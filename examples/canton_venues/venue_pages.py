"""Canton Venues: one static page and one share card per venue we read live.

Hash routes cannot carry Open Graph tags, so every venue gets a real path,
``/venues/<slug>/``, with its own title, description and a 1200x630 card image
drawn from the same JSON the dashboard reads. The collector calls ``build`` after
each tick; it can also run alone against a saved API directory:

    python venue_pages.py --api /var/www/canton-venues/api/v1 --out /var/www/canton-venues

Every number comes from the collected data. The card's headline is the venue's
one lead, picked by a fixed rule (``leads``): the first thing it is strictly #1
at, in a set order, after markets under ``MIN_LIQUIDITY_USD`` are set aside. A
venue that leads nothing gets a plain count instead. Nothing is written about
another venue beyond its place in a ranking.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

SITE = "https://cantonvenues.com"
HERE = Path(__file__).resolve().parent
FONTS = HERE / "fonts"

# Every venue the collector reads live. "rows" are its ids in venues.json (Rocky's spot and
# perp books are two rows but one venue). "x" is the venue's own X account, only when the
# venue's own website links it; "x_source" is where that link was read (2026-10-07).
VENUES = [
    {"slug": "temple", "name": "Temple", "rows": ["temple"], "site": "https://templedigitalgroup.com",
     "x": "temple_ny", "x_source": "https://templedigitalgroup.com"},
    {"slug": "cantex", "name": "Cantex", "rows": ["cantex"], "site": "https://www.cantex.io",
     "x": "cantex_io", "x_source": "https://www.cantex.io"},
    {"slug": "rocky", "name": "Rocky", "rows": ["rocky", "rocky_perp"], "site": "https://rocky.exchange",
     "x": "Rocky_exchange", "x_source": "https://rocky.exchange"},
    {"slug": "tradecraft", "name": "Tradecraft", "rows": ["tradecraft"], "site": "https://tradecraft.fi",
     "x": "TradecraftFi", "x_source": "https://tradecraft.fi"},
    {"slug": "ekiden", "name": "Ekiden", "rows": ["ekiden"], "site": "https://ekiden.fi",
     "x": "ekidenfi", "x_source": "https://ekiden.fi"},
    # neither site nor docs link an X account: the share text names the venue without a tag
    {"slug": "oneswap", "name": "OneSwap", "rows": ["oneswap"], "site": "https://www.oneswap.cc", "x": None},
    {"slug": "pool-party", "name": "Pool Party", "rows": ["poolparty"], "site": "https://poolparty.fun",
     "x": None},
]

# What the page states about how each venue is read, next to the numbers it qualifies.
CAVEATS = {
    "temple": ["Taker fee 1 bp is Temple's published rate."],
    "rocky": ["Taker fee 0.025% is assumed: Rocky publishes no fee schedule.",
              "Books quoted in USDC.B are valued at par ($1), not at USDC.B's pool price.",
              "Dollar routes compare Rocky's USDCx books only."],
    "oneswap": ["Volume is not published, so OneSwap has no volume figure or volume rank.",
                "Priced from reserves as a constant-product pool with a 0.30% fee."],
    "poolparty": ["Fee (0.30%) from CCTools; priced from reserves as a constant-product pool.",
                  "Pool Party publishes no issuer, so its tokens match by instrument id."],
    "ekiden": ["MainNet tickers, mark, index and funding from the public API."],
    "cantex": ["Priced from live reserves with Cantex's own pool formula."],
    "tradecraft": ["Priced from live reserves with Tradecraft's own pool formula."],
}

# A market this thin is not a competitor. A pool counts when its liquidity (both sides of its
# reserves, in USD: tokens.json ``liquidity_usd`` for a pool) is at least this much; an order book
# when the dollars resting within 1% of its mid are (``depth_1pct_usd``). Below it a pool or
# book neither wins nor is counted in a best-price tally, a count of tokens or pools, or a
# per-token ranking, and a quote left with fewer than two such venues counts for nobody.
MIN_LIQUIDITY_USD = 1_000
# A perp market counts as listed for "most perp markets" once it trades this much in 24h.
MIN_PERP_TURNOVER_USD = 1_000
# A single best price is a venue's lead only when it beats the next counted venue by this much.
CLEAR_EDGE_BPS = 10

SIZE_LABEL = {100: "$100", 1000: "$1K", 10000: "$10K", 50000: "$50K"}
KIND_SHORT = {"Spot AMM": "AMM", "Spot order book": "order book"}
LIVE = ("priced", "volume")


# === numbers ===============================================================

def money(v, digits: int = 2) -> str:
    """Dollars the way the dashboard writes them: $34.15M, $200,374, $2.65."""
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.{digits}f}B"
    if a >= 1e6:
        return f"${v / 1e6:.{digits}f}M"
    if a >= 1e3:
        return f"${round(v):,}"
    return f"${v:.2f}"


def short_money(v) -> str:
    """For the card: $34.1M, $490K, $2,649."""
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    if a >= 1e5:
        return f"${v / 1e3:.0f}K"
    if a >= 1e3:
        return f"${round(v):,}"
    return f"${v:.0f}"


def price(v) -> str:
    if v is None:
        return "–"
    d = 2 if abs(v) >= 1 else 4 if abs(v) >= 0.01 else 6
    return f"${v:,.{d}f}"


def stamp(t: int) -> str:
    d = datetime.fromtimestamp(t, UTC)
    return f"{d.day} {d:%b %Y, %H:%M} UTC"


def size_range(sizes: list[int]) -> str:
    """"at $50K", "at $10K and $50K", "from $1K to $50K"."""
    labels = [SIZE_LABEL.get(s, money(s)) for s in sizes]
    if len(labels) == 1:
        return f"at {labels[0]}"
    if len(labels) == 2:
        return f"at {labels[0]} and {labels[1]}"
    return f"from {labels[0]} to {labels[-1]}"


def ordinal_rank(rank: int, n: int, what: str, otherwise: str) -> str:
    """"#1 of 5 spot venues" while the venue sits in the top half of the ranking, else ``otherwise``."""
    return f"#{rank} of {n} {what}" if rank and rank * 2 <= n + 1 else otherwise


# === facts =================================================================

def _rank(values: dict[str, float], key: str) -> tuple[int | None, int]:
    """1-based rank of ``key`` by value, descending, and how many were ranked.

    A tie ranks below everyone it ties with, so two venues level at the top both rank 2 and
    neither can claim to lead.
    """
    vals = {k: v for k, v in values.items() if v}
    if key not in vals:
        return None, len(vals)
    return 1 + sum(v >= vals[key] for k, v in vals.items() if k != key), len(vals)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9%$.]+", " ", text.lower()).replace(". ", " ").split()).strip(" .")


def method_lines(lines: list[str]) -> list[str]:
    """Every sentence once: the venue's note and our caveats overlap ("Taker fee 1 bp." and "Taker fee
    1 bp is Temple's published rate."), so a sentence or clause already said by another is dropped,
    the shorter one giving way."""
    parts = []
    for line in lines:
        for sent in re.split(r"(?<=[.;])\s+", line.strip()):
            sent = sent.strip().rstrip(";").strip()
            if sent:
                parts.append(sent[0].upper() + sent[1:] + ("" if sent.endswith(".") else "."))
    keys = [_norm(x) for x in parts]
    out = []
    for n, (text, k) in enumerate(zip(parts, keys)):
        said = any(j != n and (f" {k} " in f" {o} " and (len(o) > len(k) or j < n))
                   for j, o in enumerate(keys))
        if not said:
            out.append(text)
    return out


def pair_label(pair: dict, side: str) -> str:
    """The direction of one quote, as the dashboard's execution view means it."""
    if pair["kind"] == "usd":
        return f"$ → {pair['symbol']}" if side == "buy" else f"{pair['symbol']} → $"
    return f"CC → {pair['symbol']}" if side == "sell" else f"{pair['symbol']} → CC"


def facts(data: dict) -> dict[str, dict]:
    """Per venue slug: every figure its page and card use, with its rankings."""
    rows = {r["id"]: r for r in data["venues"]["venues"]}
    tokens = data["tokens"]["tokens"]
    pairs = data["execution"]["pairs"]
    pools = data["lp"]["pools"]
    perps = data["perps"]["markets"]
    spot_total = data["venues"].get("spot_volume_24h_usd")

    spot_vol = {i: r["volume_24h_usd"] for i, r in rows.items() if r["kind"] != "Perpetuals"}
    kind_vol: dict[str, dict] = {}
    for i, r in rows.items():
        kind_vol.setdefault(r["kind"], {})[i] = r["volume_24h_usd"]
    perp_vol: dict[str, float] = {}
    perp_mkts: dict[str, list] = {}
    for p in perps:
        perp_vol[p["venue"]] = perp_vol.get(p["venue"], 0) + (p["turnover_24h_usd"] or 0)
        perp_mkts.setdefault(p["venue"], []).append(p)
    tvl: dict[str, float] = {}
    pool_rows: dict[str, list] = {}
    for p in pools:
        tvl[p["venue"]] = tvl.get(p["venue"], 0) + p["tvl_usd"]
        pool_rows.setdefault(p["venue"], []).append(p)
    tok_rows: dict[str, list] = {}
    for t in tokens:
        for v, info in t["venues"].items():
            tok_rows.setdefault(v, []).append({"symbol": t["symbol"], "key": t["key"], **info})
    deepest = {v: max((x.get("depth_1pct_usd") or 0) for x in xs) for v, xs in tok_rows.items()}

    by_key = {t["key"]: t for t in tokens}
    name_of = {i: v["name"] for v in VENUES for i in v["rows"]}

    def liquidity(key: str, venue: str, market: str | None = None) -> float | None:
        """Pool liquidity, or a book's dollars within 1% of mid; None when the data has none.

        ``market`` names the book a dollar route uses. tokens.json carries the depth of each
        venue's deepest dollar book only, so a route through another book is unmeasured.
        """
        info = by_key.get(key, {}).get("venues", {}).get(venue)
        if not info:
            return None
        if info.get("market"):
            return info.get("depth_1pct_usd") if market in (None, info["market"]) else None
        return info.get("liquidity_usd")

    def counted(key: str, venue: str, market: str | None = None) -> bool:
        """Unmeasured is not thin: only a market measured under the floor is set aside."""
        liq = liquidity(key, venue, market)
        return liq is None or liq >= MIN_LIQUIDITY_USD

    # best execution: per scope (CC pairs on pools, dollar quotes) and size, quotes won / quoted,
    # among the venues that are not thin
    wins: dict[tuple, dict] = {}
    quoted: dict[tuple, dict] = {}
    won_rows: dict[str, list] = {}
    for p in pairs:
        key = p.get("token") or p["key"]
        markets = {v: b["market"] for v, b in (p.get("books") or {}).items()}
        for r in p["rows"]:
            k = (p["kind"], r["size_usd"])
            outs = {v: o for v, o in r["out"].items() if o is not None and counted(key, v, markets.get(v))}
            if len(outs) < 2:
                continue
            for v in outs:
                quoted.setdefault(k, {}).setdefault(v, 0)
                quoted[k][v] += 1
            ranked = sorted(outs, key=outs.get, reverse=True)
            best, second = ranked[0], ranked[1]
            if outs[best] == outs[second]:
                continue
            wins.setdefault(k, {}).setdefault(best, 0)
            wins[k][best] += 1
            won_rows.setdefault(best, []).append(
                {"pair": pair_label(p, r["side"]), "kind": p["kind"], "symbol": p["symbol"],
                 "side": r["side"], "size": r["size_usd"], "out": outs[best], "next": second,
                 "next_out": outs[second], "edge_bps": (outs[best] / outs[second] - 1) * 10_000})

    # what each venue can lead, measured the same way for everyone
    tok_count = {v: sum(1 for x in xs if (x.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD)
                 for v, xs in tok_rows.items()}
    pool_count: dict[str, int] = {}
    for p in pools:
        if p["tvl_usd"] >= MIN_LIQUIDITY_USD:
            pool_count[p["venue"]] = pool_count.get(p["venue"], 0) + 1
    live_mkts = {v: [m for m in ms if (m["turnover_24h_usd"] or 0) >= MIN_PERP_TURNOVER_USD]
                 for v, ms in perp_mkts.items()}

    out = {}
    for v in VENUES:
        ids = v["rows"]
        main = rows.get(ids[0])
        if main is None:
            continue
        f = {"venue": v, "id": ids[0], "name": v["name"], "status": main["status"],
             "kind": " + ".join(dict.fromkeys(rows[i]["kind"] for i in ids if i in rows)),
             "method": method_lines([rows[i]["note"] for i in ids if i in rows]
                                    + [c for i in ids for c in CAVEATS.get(i.replace("_perp", ""), [])])}
        i = ids[0]
        if main["kind"] != "Perpetuals":
            f["spot_volume"] = main["volume_24h_usd"]
            f["spot_rank"], f["spot_n"] = _rank(spot_vol, i)
            f["spot_share"] = (main["volume_24h_usd"] / spot_total
                               if main["volume_24h_usd"] and spot_total else None)
            f["kind_label"] = KIND_SHORT.get(main["kind"], main["kind"].lower())
            f["kind_rank"], f["kind_n"] = _rank(kind_vol.get(main["kind"], {}), i)
        perp_id = next((x for x in ids if rows.get(x, {}).get("kind") == "Perpetuals"), None)
        if perp_id:
            f["perp_volume"] = perp_vol.get(perp_id)
            f["perp_rank"], f["perp_n"] = _rank(perp_vol, perp_id)
            f["perp_markets"] = perp_mkts.get(perp_id, [])
            counts = {k: len(x) for k, x in perp_mkts.items()}
            f["perp_mkt_rank"], f["perp_mkt_n"] = _rank(counts, perp_id)
            oi = [m["open_interest_usd"] for m in f["perp_markets"] if m["open_interest_usd"] is not None]
            f["open_interest"] = sum(oi) if oi else None
        f["tokens"] = sorted(tok_rows.get(i, []), key=lambda x: -(x.get("liquidity_usd") or 0))
        if f["tokens"]:
            f["token_rank"], f["token_n"] = _rank({k: len(x) for k, x in tok_rows.items()}, i)
            top = max(f["tokens"], key=lambda x: x.get("depth_1pct_usd") or 0)
            f["deepest"] = {"symbol": top["symbol"], "usd": top.get("depth_1pct_usd"),
                            "market": top.get("market")}
            f["deep_rank"], f["deep_n"] = _rank(deepest, i)
        if i in pool_rows:
            f["pools"] = sorted(pool_rows[i], key=lambda x: -x["tvl_usd"])
            f["tvl"] = tvl[i]
            f["tvl_rank"], f["tvl_n"] = _rank(tvl, i)
        f["exec"] = {f"{s}:{z}": {"won": wins.get((s, z), {}).get(i, 0), "of": quoted[(s, z)].get(i, 0)}
                     for (s, z) in sorted(quoted) if quoted[(s, z)].get(i)}
        f["won_rows"] = won_rows.get(i, [])
        f["leads"] = _leads(f, i, perp_id, name_of, spot_vol, kind_vol.get(main["kind"], {}), perp_vol, tvl,
                            tok_count, pool_count, live_mkts, tokens, pools)
        out[v["slug"]] = f
    return out


def _lead(values: dict[str, float], key: str):
    """(runner-up id, its value) when ``key`` is strictly first of two or more, else None."""
    vals = {k: v for k, v in values.items() if v}
    if key not in vals or len(vals) < 2:
        return None
    rest = sorted(((v, k) for k, v in vals.items() if k != key), reverse=True)
    return (rest[0][1], rest[0][0]) if vals[key] > rest[0][0] else None


def _direction(row: dict) -> str:
    """"buy CBTC with CC", "sell CBTC for dollars": one quote, in words."""
    sym = row["symbol"]
    if row["kind"] == "usd":
        return f"buy {sym} with dollars" if row["side"] == "buy" else f"sell {sym} for dollars"
    return f"buy {sym} with CC" if row["side"] == "sell" else f"sell {sym} for CC"


def _leads(f, i, perp_id, name_of, spot_vol, kind_vol, perp_vol, tvl, tok_count, pool_count,
           live_mkts, tokens, pools) -> list[dict]:
    """Every ranking this venue is strictly #1 at, in the order the headline picks from.

    Each lead carries ``title`` and ``sub`` for the card, ``fact`` for the page and the post,
    and ``next``/``next_value`` (the runner-up). Ties never lead.
    """
    out = []

    def add(rule, title, sub, nxt, nval, fmt=short_money):
        out.append({"rule": rule, "title": title, "sub": sub, "next": name_of.get(nxt, nxt),
                    "next_value": nval, "next_text": fmt(nval)})

    if f.get("spot_volume") and (r := _lead(spot_vol, i)):
        share = f" ({round(f['spot_share'] * 100)}% of the spot volume we see)" if f.get("spot_share") else ""
        add("spot_volume", "The largest spot venue on Canton",
            f"{short_money(f['spot_volume'])} traded in the last 24 hours{share}", *r)
    if f.get("spot_volume") and (r := _lead(kind_vol, i)):
        add("kind_volume", f"The largest {f['kind_label']} on Canton by volume",
            f"{short_money(f['spot_volume'])} traded in the last 24 hours", *r)
    if perp_id and (r := _lead(perp_vol, perp_id)):
        add("perp_volume", "The largest perps venue on Canton",
            f"{short_money(f['perp_volume'])} of perpetuals traded in the last 24 hours", *r)
    if r := _lead(tvl, i):
        add("tvl", "The most pool liquidity on Canton",
            f"{short_money(tvl[i])} across {len(f['pools'])} CC pools", *r)
    if r := _lead(tok_count, i):
        add("tokens", "The most tokens on Canton", f"{tok_count[i]} tokens priced live", *r,
            fmt=lambda n: f"{n} tokens")
    if r := _lead(pool_count, i):
        add("pool_count", "The most CC pools on Canton", f"{pool_count[i]} pools with $1K or more in them",
            *r, fmt=lambda n: f"{n} pools")
    if perp_id and (r := _lead({k: len(x) for k, x in live_mkts.items()}, perp_id)):
        ms = live_mkts[perp_id]
        add("perp_markets", "The most perp markets on Canton",
            f"{len(ms)} markets trading: {', '.join(m['base'] for m in ms)}", *r,
            fmt=lambda n: f"{n} markets")
    # one token: the deepest market within 1% of mid, among markets that are not thin
    best_tok = None
    for t in tokens:
        here = t["venues"].get(i)
        depth = {v: x.get("depth_1pct_usd") for v, x in t["venues"].items()
                 if (x.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD}
        if here and i in depth and (r := _lead(depth, i)) and (best_tok is None or depth[i] > best_tok[1]):
            best_tok = (t["symbol"], depth[i], "book" if here.get("market") else "pool", r)
    if best_tok:
        sym, d, what, r = best_tok
        add("token_depth", f"The deepest {sym} {what} on Canton", f"{short_money(d)} within 1% of mid", *r)
    # one pool pair: the largest pool for it
    best_pool = None
    by_pair: dict[str, dict] = {}
    for p in pools:
        if p["tvl_usd"] >= MIN_LIQUIDITY_USD:
            by_pair.setdefault(p["pair"], {})[p["venue"]] = p["tvl_usd"]
    for pair, vals in by_pair.items():
        if (r := _lead(vals, i)) and (best_pool is None or vals[i] > best_pool[1]):
            best_pool = (pair, vals[i], r)
    if best_pool:
        pair, v, r = best_pool
        add("pool_tvl", f"The largest {pair} pool on Canton", f"{short_money(v)} in liquidity", *r)
    # one quote: a single direction and size it wins clearly, the largest size first
    clear = [w for w in f.get("won_rows", []) if w["edge_bps"] >= CLEAR_EDGE_BPS]
    if clear:
        w = max(clear, key=lambda x: (x["size"], x["edge_bps"]))
        add("best_quote", f"Best price to {_direction(w)} at {SIZE_LABEL.get(w['size'], money(w['size']))}",
            f"{w['edge_bps'] / 100:.2f}% more than the next venue, fees and price impact included",
            w["next"], w["edge_bps"], fmt=lambda b: "")
    return out


def headline(f: dict) -> dict:
    """The card's title and subtitle: the venue's first lead, or a plain count when it has none."""
    if f.get("leads"):
        return f["leads"][0]
    if f.get("pools"):
        vol = f" and {short_money(f['spot_volume'])} traded in 24h" if f.get("spot_volume") else ""
        return {"rule": "pools", "title": f"{f['name']}, priced live on Canton",
                "sub": f"{len(f['pools'])} CC pools, {short_money(f['tvl'])} in liquidity{vol}"}
    if f.get("tokens"):
        return {"rule": "read", "title": f"{f['name']}, priced live on Canton",
                "sub": f"{len(f['tokens'])} tokens priced live"}
    return {"rule": "read", "title": f"{f['name']}, read live", "sub": f["kind"]}


def lead_line(f: dict, head: dict) -> str | None:
    """"Where Temple leads: the largest spot venue on Canton, $34.1M ... (next: Rocky, $2.8M)"."""
    if head["rule"] in PLAIN:
        return None
    nxt = f"{head['next']}, {head['next_text']}" if head["next_text"] else head["next"]
    return f"Where {f['name']} leads: {_lower_first(head['title'])}. {head['sub']}. Next: {nxt}."


def stats(f: dict) -> list[dict]:
    """Up to four card panels: header, value, small line under it."""
    out = []
    if f.get("spot_volume"):
        out.append({"k": "Spot volume, 24h", "v": short_money(f["spot_volume"]),
                    "n": ordinal_rank(f["spot_rank"], f["spot_n"], "spot venues", "as the venue reports it")})
    if f.get("perp_volume"):
        out.append({"k": "Perps volume, 24h", "v": short_money(f["perp_volume"]),
                    "n": ordinal_rank(f["perp_rank"], f["perp_n"], "perps venues", "as the venue reports it")})
    if f.get("pools"):
        out.append({"k": "In pools", "v": short_money(f["tvl"]), "n": f"{len(f['pools'])} CC pools"})
    elif f.get("deepest") and f["deepest"]["usd"]:
        out.append({"k": "Within 1% of mid", "v": short_money(f["deepest"]["usd"]),
                    "n": f"{f['deepest']['symbol']} book"})
    if f.get("open_interest"):
        out.append({"k": "Open interest", "v": short_money(f["open_interest"]), "n": "all markets"})
    if f.get("perp_markets") and len(out) < 4:
        out.append({"k": "Perp markets", "v": str(len(f["perp_markets"])),
                    "n": ", ".join(m["base"] for m in f["perp_markets"][:4])})
    if f.get("tokens") and len(out) < 4:
        out.append({"k": "Tokens", "v": str(len(f["tokens"])), "n": "priced live"})
    if f.get("pools") and len(out) < 4:
        p = f["pools"][0]
        out.append({"k": "Largest pool", "v": short_money(p["tvl_usd"]), "n": p["pair"]})
    if f.get("pools") and len(out) < 4:
        fees = sorted({p["fee"] for p in f["pools"]})
        out.append({"k": "Pool fee", "v": f"{fees[0] * 100:.2f}%" if len(fees) == 1
                    else f"{fees[0] * 100:.2f}–{fees[-1] * 100:.2f}%", "n": "per swap"})
    return out[:4]


METHOD = {
    "spot_volume": "24h spot volume as each venue reports it.",
    "kind_volume": "24h spot volume as each venue reports it.",
    "perp_volume": "24h perpetuals turnover as each venue reports it.",
    "tvl": "Pool liquidity from live reserves.",
    "tokens": "Tokens with $1K or more of liquidity, matched by Canton instrument.",
    "pool_count": "Pools with $1K or more of liquidity, from live reserves.",
    "perp_markets": "Perp markets with $1K or more traded in 24h, from each venue's public API.",
    "token_depth": "Dollars within 1% of mid; markets under $1K set aside.",
    "pool_tvl": "Pool liquidity from live reserves; pools under $1K set aside.",
    "best_quote": "Same amount on every venue, fees and price impact included; markets under $1K set aside.",
    "pools": "Pool liquidity from live reserves.",
    "read": "Read from the venue's API.",
}


# === card ==================================================================

def _font(weight: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONTS / f"Inter-{weight}.ttf"), size)


def _wrap(draw, text: str, font, width: int, lines: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        nxt = f"{cur} {w}".strip()
        if draw.textlength(nxt, font=font) <= width or not cur:
            cur = nxt
        else:
            out.append(cur)
            cur = w
    out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        while draw.textlength(out[-1] + "…", font=font) > width and " " in out[-1]:
            out[-1] = out[-1].rsplit(" ", 1)[0]
        out[-1] += "…"
    return out


def _fit(draw, text: str, weight: str, size: int, width: int, floor: int):
    """The largest font size, down to ``floor``, at which ``text`` fits on one line."""
    while size > floor and draw.textlength(text, font=_font(weight, size)) > width:
        size -= 2
    return _font(weight, size)


def _balanced(draw, text: str, font, width: int) -> list[str] | None:
    """Two lines of near-equal width, so a long headline never ends on one orphaned word; None
    when no split fits."""
    words = text.split()
    best = None
    for n in range(1, len(words)):
        a, b = " ".join(words[:n]), " ".join(words[n:])
        w = max(draw.textlength(a, font=font), draw.textlength(b, font=font))
        if w <= width and (best is None or w < best[0]):
            best = (w, [a, b])
    return best[1] if best else None


def _hex(c: str) -> tuple[int, int, int]:
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


# The dashboard's own tokens (site/index.html :root and [data-theme="dark"]), so a card reads as a
# piece of cantonvenues.com. Light is the default: it is the site's default theme, and on X's dark
# timeline a white card stands apart where a navy one would sink into the page.
THEMES = {
    "light": {"bg": "#ffffff", "bg2": "#f8fafd", "line": "#eff2f5", "line2": "#e3e7ee", "text": "#0d1421",
              "text2": "#58667e", "text3": "#a1a7bb", "accent": "#3861fb", "shadow": True,
              "strip": "#0d1421", "strip_text": "#ffffff", "strip_2": "#a1a7bb", "strip_accent": "#6188ff"},
    "dark": {"bg": "#0d1421", "bg2": "#171924", "line": "#222531", "line2": "#323546", "text": "#ffffff",
             "text2": "#a1a7bb", "text3": "#646b80", "accent": "#6188ff", "shadow": False,
             "strip": "#171924", "strip_text": "#ffffff", "strip_2": "#a1a7bb", "strip_accent": "#6188ff"},
}
UP = "#16c784"
DOWN = "#ea3943"
# the four-square mark from the site's header svg (viewBox 26): x, y, w, h, fill
LOGO = ((1, 1, 11, 11, UP), (14, 1, 11, 7, "#3861fb"), (14, 10, 11, 15, None), (1, 14, 11, 11, DOWN))


def _mix(a, b, k: float):
    return tuple(round(x * k + y * (1 - k)) for x, y in zip(a, b))


def draw_logo(d, x: float, y: float, size: float, text_rgb, bg_rgb) -> None:
    """The site's mark at ``size`` px; its dark square is the text colour at 85%, as in the svg."""
    k = size / 26
    for rx, ry, w, h, c in LOGO:
        fill = _hex(c) if c else _mix(text_rgb, bg_rgb, 0.85)
        d.rounded_rectangle((x + rx * k, y + ry * k, x + (rx + w) * k, y + (ry + h) * k), radius=3 * k, fill=fill)


def render_card(f: dict, head: dict, t: int, path: Path, theme: str = "light") -> None:
    """A 1200x630 PNG in the dashboard's own look, drawn at 2x and scaled down for clean edges.

    Header as on the site (mark, name, a live chip), the venue and its type, the lead as the
    headline, up to four stat tiles shaped like the dashboard's, and a dark source strip at the
    foot naming cantonvenues.com, the time and how the headline was measured.
    """
    from PIL import Image, ImageDraw, ImageFilter

    c = {k: (_hex(v) if isinstance(v, str) else v) for k, v in THEMES[theme].items()}
    S = 2
    W, H = 1200 * S, 630 * S
    M = 56 * S
    img = Image.new("RGB", (W, H), c["bg"])
    d = ImageDraw.Draw(img)

    # header: the site's mark and name, a hairline under it like the site header's border
    draw_logo(d, M, 38 * S, 40 * S, c["text"], c["bg"])
    d.text((M + 54 * S, 58 * S), "Canton Venues", font=_font("Bold", 30 * S), fill=c["text"], anchor="lm")
    live = "Live data"
    lf = _font("SemiBold", 20 * S)
    lw = d.textlength(live, font=lf)
    x1 = W - M
    x0 = x1 - lw - 50 * S
    d.rounded_rectangle((x0, 40 * S, x1, 76 * S), radius=8 * S, fill=_mix(_hex(UP), c["bg"], 0.12))
    d.ellipse((x0 + 16 * S, 53 * S, x0 + 26 * S, 63 * S), fill=_hex(UP))
    d.text((x0 + 34 * S, 58 * S), live, font=lf, fill=_mix(_hex(UP), (0, 0, 0), 0.8) if theme == "light"
           else _hex(UP), anchor="lm")
    d.line((0, 104 * S, W, 104 * S), fill=c["line2"], width=S)

    # the venue, the lead as the headline, its numbers under it
    width = W - 2 * M
    title = head["title"]
    # one line while it fits at 56px or more, else two even lines; never an orphaned last word
    tfont, lines = None, None
    for size in (64, 60, 56):
        if d.textlength(title, font=_font("Bold", size * S)) <= width:
            tfont, lines = _font("Bold", size * S), [title]
            break
    if not lines:
        for size in (58, 54, 50, 46):
            tfont = _font("Bold", size * S)
            lines = _balanced(d, title, tfont, width)
            if lines:
                break
        if not lines:
            lines = _wrap(d, title, tfont, width, 2)
    step = round(tfont.size * 1.12)
    sub_font = _fit(d, head["sub"], "Regular", 28 * S, width, 20 * S)
    block = 40 * S + 18 * S + len(lines) * step + 12 * S + 36 * S
    y = 104 * S + max(26 * S, (284 * S - block) // 2)

    nf = _font("SemiBold", 30 * S)
    d.text((M, y + 20 * S), f["name"], font=nf, fill=c["text"], anchor="lm")
    kx = M + d.textlength(f["name"], font=nf) + 16 * S
    kf = _font("SemiBold", 19 * S)
    kind = f["kind"]
    d.rounded_rectangle((kx, y + 3 * S, kx + d.textlength(kind, font=kf) + 24 * S, y + 37 * S), radius=8 * S,
                        fill=c["line"])
    d.text((kx + 12 * S, y + 20 * S), kind, font=kf, fill=c["text2"], anchor="lm")
    y += 40 * S + 18 * S
    for line in lines:
        d.text((M, y), line, font=tfont, fill=c["text"], anchor="lt")
        y += step
    d.text((M, y + 12 * S), head["sub"], font=sub_font, fill=c["text2"], anchor="lt")

    # stat tiles: the dashboard's .stat (hairline border, card fill, grey label over a bold value)
    panels = stats(f)
    if panels:
        gap, top, ph = 16 * S, 392 * S, 118 * S
        pw = (width - gap * (len(panels) - 1)) // len(panels)
        boxes = [(M + n * (pw + gap), top, M + n * (pw + gap) + pw, top + ph) for n in range(len(panels))]
        if c["shadow"]:  # the site's --shadow: 0 1px 2px and 0 4px 24px of #58667e
            for dy, blur, alpha in ((1, 2, 31), (4, 24, 20)):
                m = Image.new("L", (W, H), 0)
                md = ImageDraw.Draw(m)
                for b in boxes:
                    md.rounded_rectangle((b[0], b[1] + dy * S, b[2], b[3] + dy * S), radius=16 * S, fill=alpha)
                m = m.filter(ImageFilter.GaussianBlur(blur * S / 2))
                img = Image.composite(Image.new("RGB", (W, H), (88, 102, 126)), img, m)
            d = ImageDraw.Draw(img)
        pad = 22 * S
        for p, b in zip(panels, boxes):
            d.rounded_rectangle(b, radius=16 * S, fill=c["bg"], outline=c["line2"], width=2 * S)
            inner = pw - 2 * pad
            d.text((b[0] + pad, top + 18 * S), p["k"], font=_fit(d, p["k"], "Regular", 21 * S, inner, 15 * S),
                   fill=c["text2"], anchor="lt")
            d.text((b[0] + pad, top + 46 * S), p["v"], font=_fit(d, p["v"], "Bold", 42 * S, inner, 26 * S),
                   fill=c["text"], anchor="lt")
            first = p["n"].startswith("#1 ")
            d.text((b[0] + pad, top + 96 * S), p["n"],
                   font=_fit(d, p["n"], "SemiBold" if first else "Regular", 18 * S, inner, 13 * S),
                   fill=_hex(UP) if first else c["text2"], anchor="lt")

    # the source strip: where this came from, when, and how the headline was measured
    st = 538 * S
    d.rectangle((0, st, W, H), fill=c["strip"])
    sf = _font("SemiBold", 24 * S)
    y0 = st + 30 * S
    x = M
    for part, col in (("Source: ", c["strip_text"]), ("cantonvenues.com", c["strip_accent"]),
                      (" · live Canton DEX data", c["strip_text"])):
        d.text((x, y0), part, font=sf, fill=col, anchor="lm")
        x += d.textlength(part, font=sf)
    when = stamp(t)
    wf = _font("Regular", 20 * S)
    d.text((W - M, y0), when, font=wf, fill=c["strip_2"], anchor="rm")
    note = METHOD.get(head["rule"], "")
    if note:
        d.text((M, st + 66 * S), note, font=_fit(d, note, "Regular", 18 * S, width, 13 * S), fill=c["strip_2"],
               anchor="lm")

    out = img.resize((1200, 630), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    out.save(tmp, "PNG", optimize=True)
    os.replace(tmp, path)


# === pages =================================================================

def _site_parts() -> tuple[str, str, str]:
    """The dashboard's own stylesheet, logo mark and theme icons, so a venue page looks like the site."""
    src = (HERE / "site" / "index.html").read_text()
    style = re.search(r"<style>(.*?)</style>", src, re.DOTALL).group(1)
    logo = re.search(r'<a class="logo" href="#">\s*(<svg.*?</svg>)', src, re.DOTALL).group(1)
    icons = "\n".join(re.findall(r"^const (?:SUN|MOON) = .*$", src, re.MULTILINE))
    return style, logo, icons


PAGE_CSS = """
.crumbs { margin: 20px 0 6px; font-size: 13px; color: var(--text-2); font-weight: 500; }
.crumbs a { font-weight: 600; }
.vhead { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 4px 0 4px; }
.vhead h1 { font-size: 28px; margin: 0; letter-spacing: -0.01em; }
.vlead { color: var(--text-2); margin: 4px 0 18px; font-size: 15px; max-width: 80ch; }
.vlead b { color: var(--text); }
.vleads { display: inline-block; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--accent); background: var(--bg-2); color: var(--text); font-weight: 600; }
.vtop { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.vcard { display: block; width: 100%; height: auto; aspect-ratio: 1200 / 630; border-radius: 16px; border: 1px solid var(--line-2); background: var(--bg); box-shadow: var(--shadow); }
.acts { display: grid; gap: 10px; }
.btn { display: flex; align-items: center; justify-content: center; gap: 8px; border-radius: 10px; padding: 11px 16px; font-weight: 600; font-size: 14px; border: 1px solid var(--line-2); background: var(--bg-2); color: var(--text); }
.btn:hover { text-decoration: none; border-color: var(--accent); color: var(--accent); }
.btn.x { background: var(--text); color: var(--bg); border-color: var(--text); }
.btn.x:hover { color: var(--bg); filter: brightness(1.15); }
.btn svg { width: 16px; height: 16px; }
.acts .muted { line-height: 1.5; }
.won { display: inline-block; min-width: 2.2em; font-weight: 700; color: var(--up); }
.vgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.vtile { display: block; border: 1px solid var(--line); border-radius: 16px; overflow: hidden; background: var(--card); box-shadow: var(--shadow); color: var(--text); }
.vtile:hover { text-decoration: none; border-color: var(--accent); }
.vtile img { display: block; width: 100%; height: auto; aspect-ratio: 1200 / 630; background: var(--bg); border-bottom: 1px solid var(--line); }
.vtile div { padding: 12px 16px 14px; }
.vtile b { font-size: 16px; }
.vtile p { margin: 2px 0 0; color: var(--text-2); font-size: 13px; }
ul.notes { margin: 6px 0 0; padding-left: 18px; color: var(--text-2); font-size: 13px; }
ul.notes li { margin: 3px 0; }
@media (max-width: 900px) { .vtop { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
"""

X_ICON = ('<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17.8 3h3.1l-6.8 7.8'
          ' 8 10.2h-6.3l-4.9-6.4L5.3 21H2.2l7.3-8.3L1.8 3h6.4l4.4 5.9L17.8 3zm-1.1 16.2h1.7L7.4 4.7H5.6l11.1'
          ' 14.5z"/></svg>')

e = html.escape


def _shell(title: str, desc: str, canonical: str, image: str | None, body: str, t: int) -> str:
    style, logo, icons = _site_parts()
    og_img = (f'<meta property="og:image" content="{e(image)}">\n'
              '<meta property="og:image:width" content="1200">\n<meta property="og:image:height" content="630">\n'
              f'<meta name="twitter:image" content="{e(image)}">\n') if image else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{e(canonical)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Canton Venues">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(canonical)}">
{og_img}<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(desc)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<script>
  (function () {{
    var t = null;
    try {{ t = localStorage.getItem("venues-theme"); }} catch (e) {{}}
    if (!t) t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
  }})();
</script>
<link rel="icon" type="image/png" href="/icon.png">
<link rel="apple-touch-icon" href="/icon-180.png">
<script defer src="https://cloud.umami.is/script.js" data-website-id="da1b8603-0104-4e1f-8368-dbd9adbeafcb"></script>
<style>{style}{PAGE_CSS}</style>
</head>
<body>
<header class="head">
  <div class="wrap">
    <a class="logo" href="/">{logo} Canton Venues</a>
    <nav class="nav" id="nav" aria-label="Sections">
      <a href="/">Dashboard</a><a href="/venues/">Venues</a><a href="/#tokens">Tokens</a><a href="/#execution">Execution</a><a href="/#perps">Perps</a><a href="/#api">API &amp; MCP</a><a href="/#contact">Contact</a>
    </nav>
    <a class="tg" href="https://t.me/cantonvenues" target="_blank" rel="noopener" aria-label="Telegram channel"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M21.9 4.3 18.7 19.4c-.2 1-.9 1.3-1.7.8l-4.8-3.5-2.3 2.2c-.3.3-.5.5-1 .5l.3-4.9 8.9-8c.4-.3-.1-.5-.6-.2L6.5 13.2 1.8 11.7c-1-.3-1-1 .2-1.5L20.5 3c.9-.3 1.6.2 1.4 1.3z"/></svg><span>Telegram</span></a>
    <button class="theme" id="theme" type="button" aria-label="Switch theme"></button>
    <button class="burger" id="burger" type="button" aria-label="Menu" aria-expanded="false" aria-controls="nav"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
  </div>
</header>
<main class="wrap">
{body}
  <footer class="foot">
    <div><h4>Canton Venues</h4><p>Built by <a href="https://github.com/olevasyliev">Oleksii</a> on <a href="https://github.com/olevasyliev/canton-venues-sdk">Canton Venues SDK</a>, open source. Read-only: nothing here is signed or executed.</p><p><a href="https://t.me/cantonvenues">Telegram channel</a> · <a href="/#contact">Contact</a></p><p>Updated {e(stamp(t))}.</p></div>
    <div><h4>Method</h4><p>Pools are priced from live reserves with each venue's own formula, order books from their books. Best execution compares what each venue returns for the same amount, pool fees and price impact included, network fees excluded. Volumes are each venue's own 24h figures.</p></div>
    <div><h4>Data</h4><p>Every number on this page is in the open JSON API: <a href="/api/v1/venues.json">venues</a>, <a href="/api/v1/execution.json">execution</a>, <a href="/api/v1/tokens.json">tokens</a>, <a href="/api/v1/lp.json">pools</a>, <a href="/api/v1/perps.json">perps</a>.</p></div>
  </footer>
</main>
<script>
{icons}
(function () {{
  var b = document.getElementById("theme"), h = document.querySelector(".head"), m = document.getElementById("burger");
  function label() {{ var d = document.documentElement.getAttribute("data-theme") === "dark"; b.innerHTML = (d ? SUN : MOON) + (d ? "<span>Light</span>" : "<span>Dark</span>"); }}
  label();
  b.addEventListener("click", function () {{
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {{ localStorage.setItem("venues-theme", next); }} catch (e) {{}}
    label();
  }});
  m.addEventListener("click", function () {{ var o = h.classList.toggle("open"); m.setAttribute("aria-expanded", o ? "true" : "false"); }});
}})();
</script>
</body>
</html>
"""


PLAIN = ("pools", "read")  # headlines that rank nothing and open with the venue's own name


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def share_text(f: dict, head: dict) -> str:
    # a post that opens with an @handle reads as a reply: the name goes first
    who = f"{f['name']} (@{f['venue']['x']})" if f["venue"].get("x") else f["name"]
    if head["rule"] in PLAIN:  # the title only repeats the name: say the numbers
        return f"{who} on Canton Venues: {head['sub']}."
    return f"Where {who} leads on Canton Venues: {_lower_first(head['title'])}. {head['sub']}."


def intent_url(f: dict, head: dict) -> str:
    url = f"{SITE}/venues/{f['venue']['slug']}/"
    return f"https://x.com/intent/post?text={quote(share_text(f, head))}&url={quote(url, safe='')}"


def _table(headers: list[tuple[str, str]], rows: list[list[str]]) -> str:
    th = "".join(f'<th class="{c}">{e(h)}</th>' for h, c in headers)
    body = "".join("<tr>" + "".join(f'<td class="{headers[i][1]}">{cell}</td>' for i, cell in enumerate(r))
                   + "</tr>" for r in rows)
    return f'<div class="tablewrap"><table class="mini"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def venue_page(f: dict, head: dict, t: int, card_v: int | None = None) -> str:
    """The page. ``card_v`` versions the card URL so a re-share fetches the current image."""
    v = f["venue"]
    cv = card_v or t
    url = f"{SITE}/venues/{v['slug']}/"
    image = f"{url}card.png?v={cv}"
    title = (f"{f['name']} on Canton: live liquidity and prices · Canton Venues" if head["rule"] in PLAIN
             else f"{f['name']} on Canton: {_lower_first(head['title'])} · Canton Venues")
    desc = (f"{head['title']}. {head['sub']}. Live {f['name']} data on Canton Network: volume, "
            "liquidity and best execution, refreshed every five minutes.")
    parts = []
    lead = f"<b>{e(head['title'])}.</b> {e(head['sub'])}."
    led = lead_line(f, head)
    if led:
        lead = f'<span class="vleads">{e(led)}</span>'

    parts.append(f"""  <p class="crumbs"><a href="/">Canton Venues</a> / <a href="/venues/">Venues</a> / {e(f['name'])}</p>
  <div class="vhead"><h1>{e(f['name'])}</h1><span class="vchip">{e(f['kind'])}</span><span class="vst {'priced' if f['status'] == 'priced' else ''}">{'Read live' if f['status'] in LIVE else 'Unreachable'}</span></div>
  <p class="vlead">{lead}</p>""")
    handle = (f'<a class="btn" href="https://x.com/{e(v["x"])}" target="_blank" rel="noopener">{X_ICON}@{e(v["x"])}</a>'
              if v.get("x") else "")
    parts.append(f"""  <div class="vtop">
    <img class="vcard" src="card.png?v={cv}" width="1200" height="630" alt="{e(f['name'])}: {e(head['title'])}. {e(head['sub'])}.">
    <div class="panel acts">
      <h3>Share this venue</h3>
      <p class="muted" style="margin:0">The card updates with the data. Shared on X, it shows as the preview.</p>
      <a class="btn x" href="{e(intent_url(f, head))}" target="_blank" rel="noopener">{X_ICON}Share on X</a>
      <a class="btn" href="{e(v['site'])}" target="_blank" rel="noopener">{e(v['site'].split('//')[1])} ↗</a>
      {handle}
      <a class="btn" href="card.png?v={cv}" download="canton-venues-{e(v['slug'])}.png">Download card</a>
      <a class="btn" href="/#venues">All venues on the dashboard</a>
    </div>
  </div>""")

    st = []
    if f.get("spot_volume") is not None:
        st.append(("Spot volume, 24h", money(f["spot_volume"])))
        st.append(("Rank by spot volume", f"#{f['spot_rank']} of {f['spot_n']}"))
    elif "spot_rank" in f:
        st.append(("Spot volume, 24h", "Not published"))
    if f.get("spot_share"):
        st.append(("Share of spot volume", f"{f['spot_share'] * 100:.1f}%"))
    if f.get("perp_volume") is not None:
        st.append(("Perps volume, 24h", money(f["perp_volume"])))
        st.append(("Rank by perps volume", f"#{f['perp_rank']} of {f['perp_n']}"))
    if f.get("open_interest") is not None:
        st.append(("Open interest", money(f["open_interest"])))
    if f.get("tvl") is not None:
        st.append(("In pools", money(f["tvl"])))
    if f.get("tokens"):
        st.append(("Tokens priced", str(len(f["tokens"]))))
    if f.get("deepest") and not f.get("pools"):
        st.append((f"{f['deepest']['symbol']} within 1% of mid", money(f["deepest"]["usd"])))
    if f.get("perp_markets"):
        st.append(("Perp markets", str(len(f["perp_markets"]))))
    parts.append('  <div class="stats" style="margin-top:16px">' + "".join(
        f'<div class="stat"><div class="k">{e(k)}</div><div class="v num">{e(val)}</div></div>' for k, val in st)
        + "</div>")

    if f.get("exec"):
        sizes = sorted({int(k.split(":")[1]) for k in f["exec"]})
        rows = []
        for scope, label in (("cc", "CC pairs on pools"), ("usd", "Dollar quotes (order-book tokens)")):
            cells = [f["exec"].get(f"{scope}:{s}") for s in sizes]
            if not any(cells):
                continue
            rows.append([f"<b>{label}</b>"] + [
                f'<span class="{"won" if c and c["won"] else "muted"}">{c["won"]}</span><span class="muted"> of {c["of"]}</span>'
                if c else '<span class="muted">–</span>' for c in cells])
        won = {}
        for r in f["won_rows"]:
            won.setdefault(r["pair"], []).append(SIZE_LABEL.get(r["size"], str(r["size"])))
        won_list = "".join(f"<li><b>{e(p)}</b>: best at {e(', '.join(s))}</li>" for p, s in won.items())
        won_list = f'<ul class="notes">{won_list}</ul>' if won_list else ""
        if len(won) > 8:  # a long list folds, so the tables below stay in reach on a phone
            won_list = (f'<details><summary class="sub" style="cursor:pointer">Show all {len(won)}</summary>'
                        f"{won_list}</details>")
        parts.append(f"""  <section class="block" style="margin-top:28px">
    <h2>Where {e(f['name'])} is the best price</h2>
    <p class="sub">Quotes won out of quotes {e(f['name'])} could fill, at each trade size, buy and sell. Same amount on every venue; pool fees and price impact included, network fees excluded. A pool with under {e(money(MIN_LIQUIDITY_USD))} of liquidity, or a book with under {e(money(MIN_LIQUIDITY_USD))} within 1% of mid, is not counted, and a quote left with one venue counts for nobody. <a href="/#execution">Compare every venue →</a></p>
    <div class="panel">{_table([("Quotes", "l")] + [(SIZE_LABEL.get(s, str(s)), "") for s in sizes], rows)}
    {f'<p class="sub" style="margin:14px 0 0">Best price right now, by direction</p>{won_list}' if won_list else '<p class="sub" style="margin:14px 0 0">Not the best price on any quote right now.</p>'}</div>
  </section>""")

    if f.get("tokens"):
        ob = any(x.get("market") for x in f["tokens"])
        hdr = [("Token", "l"), ("Price here", ""), ("Liquidity", ""), ("Within 1% of mid", "")]
        if ob:
            hdr = [("Token", "l"), ("Market", "l"), ("Price here", ""), ("Within 1% of mid", ""), ("Spread", "")]
        rows = []
        for x in f["tokens"][:25]:
            link = f'<a href="/#t/{quote(x["key"])}"><b>{e(x["symbol"])}</b></a>'
            if ob:
                rows.append([link, f'<span class="muted">{e(x.get("market") or "")}</span>', price(x.get("price_usd")),
                             money(x.get("depth_1pct_usd")),
                             "–" if x.get("spread_bps") is None else f"{x['spread_bps']:.2f} bp"])
            else:
                rows.append([link, price(x.get("price_usd")), money(x.get("liquidity_usd")),
                             money(x.get("depth_1pct_usd"))])
        parts.append(f"""  <section class="block">
    <h2>Tokens</h2>
    <p class="sub">Every token we price on {e(f['name'])}, largest first. Click one for every venue's price.</p>
    <div class="panel">{_table(hdr, rows)}</div>
  </section>""")

    if f.get("pools"):
        rows = [[f"<b>{e(p['pair'])}</b>", money(p["tvl_usd"]), money(p["volume_24h_usd"]),
                 f"{p['fee'] * 100:.2f}%", "–" if p["fee_apr"] is None else f"{p['fee_apr'] * 100:.1f}%"]
                for p in f["pools"] if p["tvl_usd"] >= 100][:20]
        parts.append(f"""  <section class="block">
    <h2>Pools</h2>
    <p class="sub">CC pools by liquidity, from live reserves. Fee APR is the last 24h of LP fees over liquidity, annualised.</p>
    <div class="panel">{_table([("Pool", "l"), ("Liquidity", ""), ("Volume (24h)", ""), ("Fee", ""), ("Fee APR", "")], rows)}</div>
  </section>""")

    if f.get("perp_markets"):
        rows = [[f"<b>{e(m['base'])}</b><span class=\"muted\"> / {e(m['quote'])}</span>",
                 price(m["mark"] or m["last"]),
                 "–" if m["basis"] is None else f"{m['basis'] * 100:+.2f}%",
                 "–" if m["funding_rate"] is None else f"{m['funding_rate'] * 100:+.4f}%",
                 "–" if m["spread_bps"] is None else f"{m['spread_bps']:.2f} bp",
                 money(m["open_interest_usd"]), money(m["turnover_24h_usd"])] for m in f["perp_markets"]]
        parts.append(f"""  <section class="block">
    <h2>Perpetuals</h2>
    <p class="sub">Basis is the perp price against the outside spot price.</p>
    <div class="panel">{_table([("Market", "l"), ("Price", ""), ("Basis", ""), ("Funding", ""), ("Spread", ""), ("Open interest", ""), ("Volume (24h)", "")], rows)}</div>
  </section>""")

    notes = "".join(f"<li>{e(n)}</li>" for n in f["method"])
    parts.append(f"""  <section class="block">
    <h2>How we read {e(f['name'])}</h2>
    <ul class="notes">{notes}</ul>
    <p class="sub" style="margin-top:12px">Something wrong or missing? <a href="/#contact">Write to us</a>.</p>
  </section>""")
    return _shell(title, desc, url, image, "\n".join(parts), t)


def index_page(all_facts: dict, heads: dict, t: int, card_v: dict | None = None) -> str:
    card_v = card_v or {}
    tiles = "".join(
        f'<a class="vtile" href="{e(s)}/"><img src="{e(s)}/card.png?v={card_v.get(s, t)}" width="1200" height="630" loading="lazy" alt=""><div>'
        f'<b>{e(f["name"])}</b><p>{e(heads[s]["title"])}. {e(heads[s]["sub"])}.</p></div></a>'
        for s, f in all_facts.items())
    body = f"""  <p class="crumbs"><a href="/">Canton Venues</a> / Venues</p>
  <div class="title" style="padding-top:4px"><h1>Every venue we read</h1><p>A page for each Canton venue Canton Venues reads live: volume, liquidity, where it is the best price, and a card to share. Refreshed every five minutes.</p></div>
  <div class="vgrid">{tiles}</div>"""
    first = next(iter(all_facts))
    return _shell("Canton venues: a live page for every DEX we read · Canton Venues",
                  f"Live pages for {len(all_facts)} Canton Network venues: "
                  + ", ".join(f["name"] for f in all_facts.values())
                  + ". Volume, liquidity and best execution, refreshed every five minutes.",
                  f"{SITE}/venues/", f"{SITE}/venues/{first}/card.png?v={card_v.get(first, t)}", body, t)


# === build =================================================================

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def build(out: Path, data: dict, t: int, cards: bool = True) -> dict:
    """Write ``out/venues/<slug>/index.html`` (+ ``card.png``) and ``out/venues/index.html``.

    Pages are cheap and follow every tick; cards are drawn when ``cards`` is set (the collector
    does it every few ticks) or when a venue has none yet. A venue that is unreachable this tick
    keeps its last page and card.
    """
    all_facts = {s: f for s, f in facts(data).items() if f["status"] in LIVE}
    heads = {s: headline(f) for s, f in all_facts.items()}
    card_v = {}
    for s, f in all_facts.items():
        d = out / "venues" / s
        card = d / "card.png"
        if cards or not card.exists():
            render_card(f, heads[s], t, card)
            card_v[s] = t
        else:
            card_v[s] = int(card.stat().st_mtime)
        _write(d / "index.html", venue_page(f, heads[s], t, card_v[s]))
    if all_facts:
        _write(out / "venues" / "index.html", index_page(all_facts, heads, t, card_v))
    return heads


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True, help="directory holding venues.json and the rest")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    data = {n: json.loads((args.api / f"{n}.json").read_text())
            for n in ("venues", "tokens", "execution", "lp", "perps")}
    heads = build(args.out, data, data["venues"]["t"])
    for s, h in heads.items():
        print(f"{s:12} {h['rule']:13} {h['title']} | {h['sub']}")


if __name__ == "__main__":
    main()
