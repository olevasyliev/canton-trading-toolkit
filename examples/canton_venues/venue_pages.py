"""Canton Venues: one static page and one share card per venue we read live.

Hash routes cannot carry Open Graph tags, so every venue gets a real path,
``/venues/<slug>/``, with its own title, description and a 1200x630 card image
drawn from the same JSON the dashboard reads. The collector calls ``build`` after
each tick; it can also run alone against a saved API directory:

    python venue_pages.py --api /var/www/canton-venues/api/v1 --out /var/www/canton-venues

Every number comes from the collected data. The card's headline is the venue's
one lead, picked by a fixed rule (``leads``): the first thing it is #1 at by at
least ``LEAD_MARGIN`` over the runner-up, in a set order, after markets under
``MIN_LIQUIDITY_USD`` are set aside. A venue that leads nothing gets a plain
count instead. Nothing is written about another venue beyond its place in a
ranking. Each category carries its scope (``CATEGORIES``): "on Canton" only
where an outside list confirms the ranking covers the chain, otherwise "among
the Canton venues we read".
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import model

log = logging.getLogger("venues.pages")

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
# Venues whose book figures (depth, spread) are read with our own account key: nobody outside can
# check them, so the card leaves them out (Temple: TEMPLE_API_KEY; its settled volume needs none).
KEYED_BOOKS = {"temple"}

# How each row's 24h volume reaches us. Only "usd" is a dollar figure the venue itself publishes;
# every other figure is converted by us, and the card says how.
#   usd    the venue reports dollars (Temple settled_volume total_volume_usd, Tradecraft /volume USD)
#   cc     the venue reports CC, converted at our CC price (Cantex /v1/public/volume volume_cc)
#   cc_leg the CC leg of each CC pool, converted at our CC price (Pool Party /volume per pool, token
#          units; its non-CC legs, e.g. EDELx, may be the other side of the same trades, unresolved)
#   quote  turnover in each book's quote token, stablecoins counted at $1 and USDCx at its price
#          (Rocky /ticker/24hr quoteVolume, Ekiden /market/tickers turnover_24h)
VOLUME_BASIS = {"temple": "usd", "tradecraft": "usd", "cantex": "cc", "poolparty": "cc_leg",
                "rocky": "quote", "rocky_perp": "quote", "ekiden": "quote"}

# What the page states about how each venue is read, next to the numbers it qualifies.
CAVEATS = {
    "temple": ["Taker fee 1 bp is Temple's published rate.",
               "Temple's CC/USDCx book feeds the CC premium board, not the token list."],
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
# A ranking is a venue's lead only when it beats the runner-up by this much (10%: $3.27M against
# $3.11M is not a lead). Two venues inside it are level for headline purposes.
LEAD_MARGIN = 0.10
# A single best price is a venue's lead only when it beats the next counted venue by this much,
# both as quoted and AFTER each venue's known per-swap network fee is taken off (``NETWORK_FEE``):
# a venue whose fee we do not know is never handed a lead by the fee we do know for another.
CLEAR_EDGE_BPS = 10
# Per-swap network fees we know, as (unit, low, high). The leader is charged the high end and the
# runner-up the low end, so a best-price lead survives the least favourable reading.
#   oneswap  docs.oneswap.cc: "typically around $1.5-2 at recent network prices" (model.SWAP_COST_USD)
#   cantex   measured: median of 96 authenticated quotes, 2026-10-03 (model.SWAP_COST_CC_MEASURED)
# Venues missing here are charged nothing: we do not know their fee, and an assumed one would
# decide leads on a guess.
NETWORK_FEE = {"oneswap": ("usd", 1.5, 2.0),
               "cantex": ("cc", float(model.SWAP_COST_CC_MEASURED["cantex"]),
                          float(model.SWAP_COST_CC_MEASURED["cantex"]))}

# DefiLlama's Canton DEX list: the outside source that says which venues trade on Canton at all.
LLAMA_DEXS = "https://api.llama.fi/overview/dexs/Canton"
# Our row id -> its DefiLlama protocol name in that list (checked 2026-10-07).
LLAMA_NAMES = {"temple": "Temple", "cantex": "Cantex", "rocky": "Rocky Exchange Spot", "poolparty": "Pool Party"}
WHERE = {"canton": "on Canton", "read": "among the Canton venues we read"}
# Every category a venue can lead, in the order the headline picks from, with its scope. "canton"
# is claimed only when ``source`` confirms it at build time (``_on_canton``); everything else is
# "read": we rank only the venues we read, and no outside list says that covers the chain.
CATEGORIES = {
    "spot_volume": {"scope": "canton", "source": LLAMA_DEXS},
    "kind_volume": {"scope": "canton", "source": LLAMA_DEXS},
    "perp_volume": {"scope": "read", "source": None},  # derivatives coverage unverified
    "tvl": {"scope": "read", "source": None},
    "tokens": {"scope": "read", "source": None},
    "pool_count": {"scope": "read", "source": None},
    "perp_markets": {"scope": "read", "source": None},  # derivatives coverage unverified
    "token_depth": {"scope": "read", "source": None},
    "pool_tvl": {"scope": "read", "source": None},
    "best_quote": {"scope": "read", "source": None},
}
STABLES = model.STABLES

SIZE_LABEL = {100: "$100", 1000: "$1K", 10000: "$10K", 50000: "$50K"}
KIND_SHORT = {"Spot AMM": "AMM", "Spot order book": "order book"}
LIVE = ("priced", "volume")
# what a missing figure shows as: a word, not a dash (no dashes in anything we publish)
NA = "n/a"


# === numbers ===============================================================

def _one(x: float) -> str:
    """One decimal, a trailing ".0" dropped: 60.1, 84, 2.6."""
    s = f"{x:.1f}"
    return s[:-2] if s.endswith(".0") else s


def short_money(v) -> str:
    """The one money style on cards and pages: $1.2M, $490K, $60.1K, $84K, $2.6K, $512, $2.65."""
    if v is None:
        return NA
    a = abs(v)
    if a >= 999_950_000:
        return f"${_one(v / 1e9)}B"
    if a >= 999_950:
        return f"${_one(v / 1e6)}M"
    if a >= 99_950:
        return f"${round(v / 1e3)}K"
    if a >= 999.5:
        return f"${_one(v / 1e3)}K"
    if a >= 10:
        return f"${v:.0f}"
    return f"${v:.2f}"


def money(v) -> str:
    """Same as ``short_money``: one style everywhere."""
    return short_money(v)


def price(v) -> str:
    if v is None:
        return NA
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


def _cc_usd(data: dict) -> float | None:
    return (data.get("summary") or {}).get("cc_usd")


def volume_note(row_id: str, cc_usd: float | None) -> str:
    """How a volume figure was reached, short enough for a card tile."""
    basis = VOLUME_BASIS.get(row_id)
    at = f" at {price(cc_usd)}" if cc_usd else ""
    if basis == "usd":
        return "as the venue reports it"
    if basis == "cc":
        return f"CC volume{at} per CC" if at else "CC volume, converted"
    if basis == "cc_leg":
        return f"CC side{at} per CC" if at else "CC side, converted"
    if basis == "quote":
        return "quote-token turnover"
    return "converted by us"


def volume_method(row_id: str, name: str, cc_usd: float | None) -> str:
    """The same, as a sentence for the card's footer and the page."""
    basis = VOLUME_BASIS.get(row_id)
    at = f"at {price(cc_usd)} per CC" if cc_usd else "at our CC price"
    if basis == "usd":
        return f"24h volume as {name} reports it, in dollars."
    if basis == "cc":
        return f"24h volume: {name}'s CC volume, converted {at}."
    if basis == "cc_leg":
        return f"24h volume: the CC side of each CC pool, converted {at}."
    if basis == "quote":
        return f"24h volume: {name}'s turnover in each book's quote token, converted to dollars."
    return f"24h volume converted by us from {name}'s figures."


def _on_canton(rule: str, row_id: str, rows: dict, llama: list | None) -> bool:
    """True when DefiLlama's Canton DEX list confirms ``row_id`` leads the same ranking chain-wide.

    For spot volume, ours must be the largest entry in the list; for volume by kind, the largest
    among entries of our kind, and an entry we cannot place by kind that is larger blocks it.
    """
    if not llama or row_id not in LLAMA_NAMES:
        return False
    vol = {x["name"]: x.get("volume_24h") or 0 for x in llama}
    ours = vol.get(LLAMA_NAMES[row_id])
    if not ours:
        return False
    kind_of = {LLAMA_NAMES[i]: rows[i]["kind"] for i in LLAMA_NAMES if i in rows}
    for name, v in vol.items():
        if name == LLAMA_NAMES[row_id] or v < ours:
            continue
        if rule == "spot_volume" or kind_of.get(name) in (None, rows[row_id]["kind"]):
            return False
    return True


def facts(data: dict) -> dict[str, dict]:
    """Per venue slug: every figure its page and card use, with its rankings."""
    rows = {r["id"]: r for r in data["venues"]["venues"]}
    tokens = data["tokens"]["tokens"]
    pairs = data["execution"]["pairs"]
    pools = data["lp"]["pools"]
    # CC pools the collector reads but cannot quote (a token no venue names, a second pool for a
    # token): their liquidity is still 2x the CC reserve, so they count in every pool total
    unpriced = [{**p, "priced": False} for p in data["lp"].get("unpriced_pools") or []]
    perps = data["perps"]["markets"]
    spot_total = data["venues"].get("spot_volume_24h_usd")
    cc_usd = _cc_usd(data)
    llama = ((data.get("summary") or {}).get("ecosystem") or {}).get("venues")

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
    for p in [*pools, *unpriced]:
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
        swaps = p.get("swaps") or {}
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
                 "next_out": outs[second], "edge_bps": (outs[best] / outs[second] - 1) * 10_000,
                 "net_edge_bps": net_edge_bps(best, second, outs[best], outs[second], r["size_usd"],
                                              swaps.get(best, 1), swaps.get(second, 1), cc_usd)})

    # what each venue can lead, measured the same way for everyone
    tok_count = {v: sum(1 for x in xs if (x.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD)
                 for v, xs in tok_rows.items()}
    pool_count: dict[str, int] = {}
    for p in [*pools, *unpriced]:
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
        f = {"venue": v, "id": ids[0], "name": v["name"], "status": main["status"], "cc_usd": cc_usd,
             "kind": " + ".join(dict.fromkeys(rows[i]["kind"] for i in ids if i in rows)),
             "keyed": ids[0] in KEYED_BOOKS,
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
            if main["volume_24h_usd"]:
                f["method"].append(volume_method(i, v["name"], cc_usd))
        perp_id = next((x for x in ids if rows.get(x, {}).get("kind") == "Perpetuals"), None)
        if perp_id:
            f["perp_id"] = perp_id
            f["perp_volume"] = perp_vol.get(perp_id)
            f["perp_rank"], f["perp_n"] = _rank(perp_vol, perp_id)
            f["perp_markets"] = perp_mkts.get(perp_id, [])
            counts = {k: len(x) for k, x in perp_mkts.items()}
            f["perp_mkt_rank"], f["perp_mkt_n"] = _rank(counts, perp_id)
            oi = [m["open_interest_usd"] for m in f["perp_markets"] if m["open_interest_usd"] is not None]
            f["open_interest"] = sum(oi) if oi else None
            if f["perp_volume"]:
                f["method"].append(f"Perps volume: 24h turnover in each market's quote token "
                                   f"({', '.join(sorted({m['quote'] for m in f['perp_markets']}))}), counted at $1.")
        f["tokens"] = sorted(tok_rows.get(i, []), key=lambda x: -(x.get("liquidity_usd") or 0))
        # the venue's own market list, when the collector has one (Temple: every market that settled
        # in the last 24h), so a card can say how many of them we price
        listed = [s.partition("/")[0] for s in main.get("markets_24h") or []]
        if listed:
            f["listed"] = list(dict.fromkeys(listed))
            f["listed_pairs"] = list(dict.fromkeys(main.get("markets_24h") or []))
            ours = [x["symbol"] for x in f["tokens"] if x["symbol"] in f["listed"]]
            if len(ours) < len(f["listed"]):
                f["method"].append(f"{v['name']} had {len(f['listed'])} markets trading in the last 24h "
                                   f"({', '.join(f['listed'])}); we price {len(ours)} of them as tokens "
                                   f"({', '.join(ours)}).")
        if f["tokens"]:
            f["token_rank"], f["token_n"] = _rank({k: len(x) for k, x in tok_rows.items()}, i)
            top = max(f["tokens"], key=lambda x: x.get("depth_1pct_usd") or 0)
            f["deepest"] = {"symbol": top["symbol"], "usd": top.get("depth_1pct_usd"),
                            "market": top.get("market")}
            f["deep_rank"], f["deep_n"] = _rank(deepest, i)
        if i in pool_rows:
            f["pools"] = sorted(pool_rows[i], key=lambda x: -x["tvl_usd"])
            f["pool_n"] = len(f["pools"])
            f["pool_priced"] = sum(1 for p in f["pools"] if p.get("priced", True))
            f["tvl"] = tvl[i]
            f["tvl_rank"], f["tvl_n"] = _rank(tvl, i)
            if f["pool_priced"] < f["pool_n"]:
                f["method"].append(
                    f"{f['pool_n'] - f['pool_priced']} of its {f['pool_n']} CC pools pair CC with a token we cannot "
                    "price; their liquidity is counted from the CC side (twice the CC reserve), like every pool's.")
        f["exec"] = {f"{s}:{z}": {"won": wins.get((s, z), {}).get(i, 0), "of": quoted[(s, z)].get(i, 0)}
                     for (s, z) in sorted(quoted) if quoted[(s, z)].get(i)}
        f["won_rows"] = won_rows.get(i, [])
        f["leads"] = _leads(f, i, perp_id, name_of, rows, llama, spot_vol, kind_vol.get(main["kind"], {}),
                            perp_vol, tvl, tok_count, pool_count, live_mkts, tokens, pools)
        f["near"] = _near(f, i, perp_id, spot_vol, kind_vol.get(main["kind"], {}), perp_vol, tvl,
                          tok_count, pool_count, live_mkts, name_of)
        out[v["slug"]] = f
    return out


def _lead(values: dict[str, float], key: str, margin: float = LEAD_MARGIN):
    """(runner-up id, its value, margin over it) when ``key`` is first of two or more by at least
    ``margin`` (a share: 0.10 is 10%); else None. A tie never leads."""
    vals = {k: v for k, v in values.items() if v}
    if key not in vals or len(vals) < 2:
        return None
    rest = sorted(((v, k) for k, v in vals.items() if k != key), reverse=True)
    top = rest[0][0]
    if vals[key] > top and vals[key] >= top * (1 + margin):
        return rest[0][1], top, vals[key] / top - 1
    return None


def _near(f, i, perp_id, spot_vol, kind_vol, perp_vol, tvl, tok_count, pool_count, live_mkts, name_of):
    """Rankings this venue tops but by less than ``LEAD_MARGIN``: not leads, kept for the log."""
    out = []
    for rule, vals, key in (("spot_volume", spot_vol, i), ("kind_volume", kind_vol, i),
                            ("perp_volume", perp_vol, perp_id), ("tvl", tvl, i), ("tokens", tok_count, i),
                            ("pool_count", pool_count, i),
                            ("perp_markets", {k: len(x) for k, x in live_mkts.items()}, perp_id)):
        if key is None:
            continue
        r = _lead(vals, key, margin=0)
        if r and r[2] < LEAD_MARGIN:
            out.append({"rule": rule, "next": name_of.get(r[0], r[0]), "margin": r[2]})
    return out


def net_edge_bps(best: str, nxt: str, out_best: float, out_next: float, size: float, swaps_best: int,
                 swaps_next: int, cc_usd: float | None) -> float | None:
    """The best venue's edge over the next after each one's known network fee; the leader pays the
    high end, the runner-up the low end. None when a known fee cannot be converted to dollars."""
    def fee(v: str, swaps: int, high: bool) -> float | None:
        if v not in NETWORK_FEE:
            return 0.0
        unit, lo, hi = NETWORK_FEE[v]
        amount = hi if high else lo
        if unit == "cc":
            if not cc_usd:
                return None
            amount *= cc_usd
        return swaps * amount / size
    fb, fn = fee(best, swaps_best, True), fee(nxt, swaps_next, False)
    if fb is None or fn is None or fb >= 1:
        return None
    return (out_best * (1 - fb) / (out_next * (1 - fn)) - 1) * 10_000


def _direction(row: dict) -> str:
    """"buy CBTC with CC", "sell CBTC for dollars": one quote, in words."""
    sym = row["symbol"]
    if row["kind"] == "usd":
        return f"buy {sym} with dollars" if row["side"] == "buy" else f"sell {sym} for dollars"
    return f"buy {sym} with CC" if row["side"] == "sell" else f"sell {sym} for CC"


def _leads(f, i, perp_id, name_of, rows, llama, spot_vol, kind_vol, perp_vol, tvl, tok_count, pool_count,
           live_mkts, tokens, pools) -> list[dict]:
    """Every ranking this venue is #1 at by ``LEAD_MARGIN`` or more, in the order the headline picks from.

    Each lead carries ``title`` and ``sub`` for the card, ``fact`` for the page and the post,
    ``next``/``next_value`` (the runner-up), ``margin`` over it, ``value`` (the figure the card
    leads with) and ``scope`` with its ``source``. Ties never lead.
    """
    out = []

    def where(rule: str, row_id: str | None = None) -> str:
        cat = CATEGORIES[rule]
        if cat["scope"] == "canton" and row_id and _on_canton(rule, row_id, rows, llama):
            return "canton"
        return "read"

    def add(rule, title, sub, r, value, fmt=short_money, scope="read"):
        nxt, nval, margin = r
        out.append({"rule": rule, "title": title, "sub": sub, "next": name_of.get(nxt, nxt),
                     "next_value": nval, "next_text": fmt(nval), "margin": margin, "value": value,
                     "scope": scope, "source": CATEGORIES[rule]["source"] if scope == "canton" else None})

    if f.get("spot_volume") and (r := _lead(spot_vol, i)):
        sc = where("spot_volume", i)
        share = f" ({round(f['spot_share'] * 100)}% of the spot volume we read)" if f.get("spot_share") else ""
        add("spot_volume", f"The largest spot venue {WHERE[sc]}",
            f"{short_money(f['spot_volume'])} traded in the last 24 hours{share}", r, f["spot_volume"], scope=sc)
    if f.get("spot_volume") and (r := _lead(kind_vol, i)):
        sc = where("kind_volume", i)
        title = (f"The largest {f['kind_label']} on Canton by volume" if sc == "canton"
                 else f"The largest {f['kind_label']} by volume {WHERE[sc]}")
        add("kind_volume", title, f"{short_money(f['spot_volume'])} traded in the last 24 hours", r,
            f["spot_volume"], scope=sc)
    if perp_id and (r := _lead(perp_vol, perp_id)):
        add("perp_volume", f"The largest perps venue {WHERE['read']}",
            f"{short_money(f['perp_volume'])} of perpetuals traded in the last 24 hours", r, f["perp_volume"])
    if r := _lead(tvl, i):
        add("tvl", f"The most pool liquidity {WHERE['read']}", f"{short_money(tvl[i])} across {pools_text(f)}", r,
            tvl[i])
    if r := _lead(tok_count, i):
        add("tokens", f"The most tokens {WHERE['read']}", f"{tok_count[i]} tokens priced live", r, tok_count[i],
            fmt=lambda n: f"{n} tokens")
    if r := _lead(pool_count, i):
        add("pool_count", f"The most CC pools {WHERE['read']}", f"{pool_count[i]} pools with $1K or more in them",
            r, pool_count[i], fmt=lambda n: f"{n} pools")
    if perp_id and (r := _lead({k: len(x) for k, x in live_mkts.items()}, perp_id)):
        ms = live_mkts[perp_id]
        add("perp_markets", f"The most perp markets {WHERE['read']}",
            f"{len(ms)} markets trading: {', '.join(m['base'] for m in ms)}", r, len(ms),
            fmt=lambda n: f"{n} markets")
    # one token: the deepest market within 1% of mid, among markets that are not thin. A book read
    # with our own key is not something anyone else can check, so it never leads.
    best_tok = None
    for t in tokens:
        here = t["venues"].get(i)
        depth = {v: x.get("depth_1pct_usd") for v, x in t["venues"].items()
                 if (x.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD}
        if f.get("keyed") and here and here.get("market"):
            continue
        if here and i in depth and (r := _lead(depth, i)) and (best_tok is None or depth[i] > best_tok[1]):
            best_tok = (t["symbol"], depth[i], "book" if here.get("market") else "pool", r)
    if best_tok:
        sym, d, what, r = best_tok
        add("token_depth", f"The deepest {sym} {what} {WHERE['read']}", f"{short_money(d)} within 1% of mid", r, d)
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
        add("pool_tvl", f"The largest {pair} pool {WHERE['read']}", f"{short_money(v)} in liquidity", r, v)
    # one quote: a single direction and size it still wins clearly once each venue's known network
    # fee is taken off, the largest size first
    clear = [w for w in f.get("won_rows", [])
             if w["edge_bps"] >= CLEAR_EDGE_BPS and (w.get("net_edge_bps") or 0) >= CLEAR_EDGE_BPS]
    if clear:
        w = max(clear, key=lambda x: (x["size"], x["net_edge_bps"]))
        add("best_quote", f"Best price to {_direction(w)} at {SIZE_LABEL.get(w['size'], money(w['size']))}",
            f"{w['edge_bps'] / 100:.2f}% more than the next venue we read, pool fees and price impact "
            "included, network fees excluded",
            (w["next"], w["net_edge_bps"], w["net_edge_bps"] / 10_000), w["edge_bps"], fmt=lambda b: "")
    return out


def pools_text(f: dict) -> str:
    """"5 CC pools, 4 priced" when some pair CC with a token we cannot price, else "24 CC pools"."""
    n, priced = f.get("pool_n", 0), f.get("pool_priced", 0)
    return f"{n} CC pools, {priced} priced" if priced < n else f"{n} CC pools"


def headline(f: dict) -> dict:
    """The card's title and subtitle: the venue's first lead, or a plain count when it has none."""
    if f.get("leads"):
        return f["leads"][0]
    if f.get("pools"):
        n, priced = f["pool_n"], f["pool_priced"]
        tail = f", {priced} priced" if priced < n else ""
        return {"rule": "pools", "title": f"{f['name']}, priced live on Canton",
                "sub": f"{n} CC pools, {short_money(f['tvl'])} in liquidity{tail}", "value": f["tvl"]}
    if f.get("tokens"):
        return {"rule": "read", "title": f"{f['name']}, priced live on Canton",
                "sub": f"{len(f['tokens'])} tokens priced live", "value": None}
    return {"rule": "read", "title": f"{f['name']}, read live", "sub": f["kind"], "value": None}


def lead_line(f: dict, head: dict) -> str | None:
    """"Where Temple leads: the largest spot venue on Canton, $34.1M ... (next: Rocky, $2.8M)"."""
    if head["rule"] in PLAIN:
        return None
    nxt = f"{head['next']}, {head['next_text']}" if head["next_text"] else head["next"]
    return f"Where {f['name']} leads: {_lower_first(head['title'])}. {head['sub']}. Next: {nxt}."


def names_text(names: list[str], shown: int = 4) -> str:
    """"CBTC, eXAU, eXAG", or the first ``shown`` and a count of the rest: "USDC.B, USDCx +21"."""
    rest = len(names) - shown
    return ", ".join(names[:shown]) + (f" +{rest}" if rest > 0 else "")


def fit_value(d, p: dict, font_at, width: float, size: int, floor: int):
    """A tile's value and its font: the largest size down to ``floor`` at which it fits. A list of
    names drops names (four at most, then "+N") before it would go under the floor."""
    names = p.get("names")
    tries = [names_text(names, k) for k in range(min(4, len(names)), 0, -1)] if names else [p["v"]]
    for text in tries:
        z = size
        while z > floor and d.textlength(text, font=font_at(z)) > width:
            z -= 2
        if d.textlength(text, font=font_at(z)) <= width:
            return text, font_at(z)
    return tries[-1], font_at(floor)


QUOTE_NAMES = {"USDCX": "USDCx", "USDCB": "USDC.B", "USDC.B": "USDC.B", "USDT": "USDT", "CC": "CC"}


def market_pair(symbol: str, market: str | None) -> str:
    """A book's pair the way people write it: "CBTC-USDCB" -> "CBTC/USDC.B"."""
    if not market:
        return symbol
    for sep in ("/", "-"):
        if sep in market:
            q = market.rsplit(sep, 1)[1]
            return f"{symbol}/{QUOTE_NAMES.get(q.upper(), q)}"
    return market


def _stable(x: dict) -> bool:
    return (x.get("key") or x["symbol"]).upper() in STABLES


def token_tile(f: dict) -> dict:
    """What we price there, by name, the largest non-stablecoin tokens first. When the venue
    publishes its own market list and we price only part of it, the tile names what is left out
    ("CC/USDCx excluded")."""
    toks = [x for x in f["tokens"] if not _stable(x)] + [x for x in f["tokens"] if _stable(x)]
    names = [x["symbol"] for x in toks]
    listed = f.get("listed") or []
    if listed and set(listed) - set(names):
        n = len(set(names) & set(listed))
        left = [p for p in f.get("listed_pairs") or listed if p.partition("/")[0] not in names]
        return {"k": "Markets we price", "v": names_text(names), "names": names, "m": "tokens",
                "n": f"{n} of {len(listed)}, {', '.join(left)} excluded"}
    return {"k": "Tokens priced", "v": names_text(names), "names": names, "m": "tokens",
            "n": f"{len(names)} priced live"}


def perp_tile(f: dict) -> dict:
    """Perp markets by name, the ones trading first; trading means $1K or more in 24h, the same line
    the "most perp markets" headline draws."""
    ms = f["perp_markets"]
    live = [m["base"] for m in ms if (m["turnover_24h_usd"] or 0) >= MIN_PERP_TURNOVER_USD]
    if not live:
        names = [m["base"] for m in ms]
        return {"k": "Perp markets", "v": names_text(names), "names": names, "m": "perp_markets",
                "n": f"{len(ms)} listed, none trading"}
    n = f"{len(ms)} listed, all trading" if len(live) == len(ms) else f"{len(live)} trading, {len(ms)} listed"
    return {"k": "Perp markets trading", "v": names_text(live), "names": live, "n": n, "m": "perp_markets"}


def stats(f: dict) -> list[dict]:
    """Up to four card panels: header, value, small line under it, and ``m``, the kind of figure,
    which the card's footer explains. A tile of names carries them in ``names`` so the card can fit
    as many as the width allows."""
    out = []
    if f.get("spot_volume"):
        out.append({"k": "Spot volume, 24h", "v": short_money(f["spot_volume"]), "m": "spot_volume",
                    "n": ordinal_rank(f["spot_rank"], f["spot_n"], "spot venues reporting volume",
                                      volume_note(f["id"], f.get("cc_usd")))})
    if f.get("perp_volume"):
        out.append({"k": "Perps volume, 24h", "v": short_money(f["perp_volume"]), "m": "perp_volume",
                    "n": ordinal_rank(f["perp_rank"], f["perp_n"], "perps venues we read",
                                      volume_note(f.get("perp_id", ""), f.get("cc_usd")))})
    if f.get("pools"):
        out.append({"k": "In pools", "v": short_money(f["tvl"]), "n": pools_text(f), "m": "tvl"})
    elif f.get("keyed"):
        # a keyed book's depth cannot be checked from outside: the venue's share of the volume
        # we read takes its place
        if f.get("spot_share"):
            out.append({"k": "Share of spot volume", "v": f"{f['spot_share'] * 100:.0f}%", "m": "share",
                        "n": f"of {f['spot_n']} venues reporting it"})
    elif f.get("deepest") and f["deepest"]["usd"]:
        out.append({"k": "Within 1% of mid", "v": short_money(f["deepest"]["usd"]), "m": "depth",
                    "n": f"{market_pair(f['deepest']['symbol'], f['deepest']['market'])} book"})
    if f.get("open_interest"):
        out.append({"k": "Open interest", "v": short_money(f["open_interest"]), "n": "all markets", "m": "oi"})
    if f.get("perp_markets") and len(out) < 4:
        out.append(perp_tile(f))
    if f.get("tokens") and len(out) < 4:
        out.append(token_tile(f))
    if f.get("pools") and len(out) < 4:
        p = f["pools"][0]
        out.append({"k": "Largest pool", "v": short_money(p["tvl_usd"]), "m": "tvl",
                    "n": p["pair"] if p.get("priced", True) else "CC and an unnamed token"})
    if f.get("pools") and len(out) < 4:
        fees = sorted({p["fee"] for p in f["pools"] if p.get("fee") is not None})
        if fees:
            out.append({"k": "Pool fee", "v": f"{fees[0] * 100:.2f}%" if len(fees) == 1
                        else f"{fees[0] * 100:.2f}% to {fees[-1] * 100:.2f}%", "n": "per swap", "m": "fee"})
    return out[:4]


METHOD = {
    "spot_volume": "Spot volume ranked among the venues we read.",
    "kind_volume": "Volume ranked among the venues we read.",
    "perp_volume": "Perps turnover ranked among the venues we read.",
    "tvl": "Pool liquidity ranked among the venues we read.",
    "tokens": "Tokens with $1K or more of liquidity, matched by Canton instrument.",
    "pool_count": "Pools with $1K or more of liquidity, from live reserves.",
    "perp_markets": "Perp markets with $1K or more traded in 24h, from each venue's public API.",
    "token_depth": "Dollars within 1% of mid; markets under $1K set aside.",
    "pool_tvl": "Pool liquidity from live reserves; pools under $1K set aside.",
    "best_quote": ("Best price: same amount on every venue, pool fees and price impact included, network fees "
                   "excluded; the lead also holds after known network fees."),
    "pools": "",
    "read": "Read from the venue's API.",
}
# the "on Canton" version of a ranking line: the outside list it was checked against
METHOD_CANTON = "Ranked {what} among the venues we read and checked against DefiLlama's Canton DEX list."


def metric_note(m: str, f: dict) -> str:
    """What one kind of card figure is, in a sentence: the footer names every kind the card shows."""
    if m == "spot_volume":
        return volume_method(f["id"], f["name"], f.get("cc_usd"))
    if m == "perp_volume":
        quotes = ", ".join(sorted({x["quote"] for x in f.get("perp_markets") or []})) or "the quote token"
        return f"Perps volume: 24h turnover in {quotes}, counted at $1."
    return {
        "tvl": "Pool liquidity: twice each CC pool's CC reserve, at our CC price.",
        "share": "Share: of the 24h spot volume across the venues we read.",
        "depth": "Depth: dollars resting within 1% of mid, from the venue's public book.",
        "oi": "Open interest: open contracts at mark price.",
        "perp_markets": "Perp markets trading: $1K or more in 24h.",
        "tokens": "Tokens: $1K or more of liquidity, matched by Canton instrument.",
        "fee": "Pool fee: per swap, before network fees.",
    }.get(m, "")


def card_notes(f: dict, head: dict) -> str:
    """The card footer: who we are not, how the headline was ranked, and every figure kind shown."""
    rule = head["rule"]
    if head.get("scope") == "canton":
        lead = METHOD_CANTON.format(what={"spot_volume": "by spot volume"}.get(rule, "by volume"))
    else:
        lead = METHOD.get(rule, "")
    parts = [f"Independent data, not affiliated with {f['name']}.", lead]
    for p in stats(f):
        note = metric_note(p.get("m", ""), f)
        if note and note not in parts:
            parts.append(note)
    return " ".join(x for x in parts if x)


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


def note_lines(draw, text: str, font_at, width: int, size: int, two: int, floor: int):
    """The footer note on one line while it fits at ``size`` or up to 4px under, else two even
    lines at the largest size from ``two`` down to ``floor`` that fits."""
    for z in range(size, size - 5, -1):
        if draw.textlength(text, font=font_at(z)) <= width:
            return [text], font_at(z)
    for z in range(two, floor - 1, -1):
        lines = _balanced(draw, text, font_at(z), width)
        if lines:
            return lines, font_at(z)
    return _wrap(draw, text, font_at(floor), width, 2), font_at(floor)


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


# which card ships: "venue" draws each venue's card in its own visual style (venue_style.py);
# "ours" draws every card in the dashboard's own look (``theme`` light or dark)
LOOKS = ("venue", "ours")


def render_card(f: dict, head: dict, t: int, path: Path, theme: str = "light", look: str = "venue") -> None:
    """The venue's 1200x630 share card, in its own style (``look="venue"``) or ours."""
    if look == "venue":
        import venue_style
        venue_style.render(f, head, t, path)
    else:
        render_card_ours(f, head, t, path, theme)


def render_card_ours(f: dict, head: dict, t: int, path: Path, theme: str = "light") -> None:
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
            v, vf = fit_value(d, p, lambda z: _font("Bold", z), inner, 42 * S, 24 * S)
            d.text((b[0] + pad, top + 46 * S), v, font=vf, fill=c["text"], anchor="lt")
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
    for part, col in (("Live Canton DEX data from ", c["strip_text"]), ("cantonvenues.com", c["strip_accent"])):
        d.text((x, y0), part, font=sf, fill=col, anchor="lm")
        x += d.textlength(part, font=sf)
    when = stamp(t)
    wf = _font("Regular", 20 * S)
    d.text((W - M, y0), when, font=wf, fill=c["strip_2"], anchor="rm")
    lines, nf = note_lines(d, card_notes(f, head), lambda z: _font("Regular", z), width, 18 * S, 15 * S, 11 * S)
    for n, line in enumerate(lines):
        d.text((M, st + (58 + 20 * n if len(lines) > 1 else 66) * S), line, font=nf, fill=c["strip_2"], anchor="lm")

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
    <div><h4>Canton Venues</h4><p>Built by <a href="https://github.com/olevasyliev">Oleksii</a> on <a href="https://github.com/olevasyliev/canton-venues-sdk">Canton Venues SDK</a>, open source. Read-only: nothing here is signed or executed.</p><p><a href="https://t.me/cantonvenues">Telegram channel</a>, <a href="/#contact">contact</a></p><p>Updated {e(stamp(t))}.</p></div>
    <div><h4>Method</h4><p>Pools are priced from live reserves with each venue's own formula, order books from their books. Best execution compares what each venue returns for the same amount, pool fees and price impact included, network fees excluded. Volumes are each venue's 24h figures; where a venue reports token amounts rather than dollars we convert them, and each venue's page says how.</p></div>
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
    title = (f"{f['name']} on Canton: live liquidity and prices | Canton Venues" if head["rule"] in PLAIN
             else f"{f['name']} on Canton: {_lower_first(head['title'])} | Canton Venues")
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
        st.append(("Rank by spot volume", f"#{f['spot_rank']} of {f['spot_n']} reporting volume"))
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
        st.append(("CC pools", f"{f['pool_n']}, {f['pool_priced']} priced" if f["pool_priced"] < f["pool_n"]
                   else str(f["pool_n"])))
    if f.get("tokens"):
        listed = f.get("listed") or []
        ours = [x for x in f["tokens"] if x["symbol"] in listed]
        st.append(("Markets we price", f"{len(ours)} of {len(listed)}") if len(ours) < len(listed)
                  else ("Tokens priced", str(len(f["tokens"]))))
    if f.get("deepest") and not f.get("pools") and not f.get("keyed"):
        st.append((f"{market_pair(f['deepest']['symbol'], f['deepest']['market'])} within 1% of mid",
                   money(f["deepest"]["usd"])))
    if f.get("perp_markets"):
        live = sum(1 for m in f["perp_markets"] if (m["turnover_24h_usd"] or 0) >= MIN_PERP_TURNOVER_USD)
        st.append(("Perp markets trading", f"{live} of {len(f['perp_markets'])}"))
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
                if c else f'<span class="muted">{NA}</span>' for c in cells])
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
                             NA if x.get("spread_bps") is None else f"{x['spread_bps']:.2f} bp"])
            else:
                rows.append([link, price(x.get("price_usd")), money(x.get("liquidity_usd")),
                             money(x.get("depth_1pct_usd"))])
        keyed_note = (f" Prices, depth and spread here are from {e(f['name'])}'s order book via our account key, "
                      "so they cannot be checked without one." if f.get("keyed") else "")
        parts.append(f"""  <section class="block">
    <h2>Tokens</h2>
    <p class="sub">Every token we price on {e(f['name'])}, largest first. Click one for every venue's price.{keyed_note}</p>
    <div class="panel">{_table(hdr, rows)}</div>
  </section>""")

    if f.get("pools"):
        rows = [[f"<b>{e(p['pair'])}</b>" if p.get("priced", True)
                 else f'<b>{e(p["pair"])}</b><span class="muted"> (not priced)</span>',
                 money(p["tvl_usd"]), money(p.get("volume_24h_usd")),
                 NA if p.get("fee") is None else f"{p['fee'] * 100:.2f}%",
                 NA if p.get("fee_apr") is None else f"{p['fee_apr'] * 100:.1f}%"]
                for p in f["pools"] if p["tvl_usd"] >= 100][:20]
        parts.append(f"""  <section class="block">
    <h2>Pools</h2>
    <p class="sub">CC pools by liquidity: twice the CC reserve, at our CC price, so a pool counts even when we cannot price its other token. Fee APR is the last 24h of LP fees over liquidity, annualised.</p>
    <div class="panel">{_table([("Pool", "l"), ("Liquidity", ""), ("Volume (24h)", ""), ("Fee", ""), ("Fee APR", "")], rows)}</div>
  </section>""")

    if f.get("perp_markets"):
        rows = [[f"<b>{e(m['base'])}</b><span class=\"muted\"> / {e(m['quote'])}</span>",
                 price(m["mark"] or m["last"]),
                 NA if m["basis"] is None else f"{m['basis'] * 100:+.2f}%",
                 NA if m["funding_rate"] is None else f"{m['funding_rate'] * 100:+.4f}%",
                 NA if m["spread_bps"] is None else f"{m['spread_bps']:.2f} bp",
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
    return _shell("Canton venues: a live page for every DEX we read | Canton Venues",
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


def build(out: Path, data: dict, t: int, cards: bool = True, look: str = "venue", guard=None,
          fresh: dict[str, int] | None = None) -> dict:
    """Write ``out/venues/<slug>/index.html`` (+ ``card.png``) and ``out/venues/index.html``.

    Pages are cheap and follow every tick; cards are drawn when ``cards`` is set (the collector
    does it every few ticks) or when a venue has none yet. A venue that is unreachable this tick
    keeps its last page and card. ``look`` picks the card's style (``LOOKS``).

    ``guard`` (publish_guard.PublishGuard) holds a venue on its last good card and page while its
    figures jump implausibly or its data is stale; ``fresh`` is each row id's last successful read
    (unix time), from the collector. Returns the headlines written this time.
    """
    from publish_guard import card_figures

    all_facts = {s: f for s, f in facts(data).items() if f["status"] in LIVE}
    heads = {s: headline(f) for s, f in all_facts.items()}
    shown = dict(heads)
    card_v = {}
    for s, f in list(all_facts.items()):
        d = out / "venues" / s
        card = d / "card.png"
        if guard is not None:
            figs = card_figures(f, heads[s], stats(f))
            seen = [fresh[i] for i in f["venue"]["rows"] if fresh and i in fresh]
            age = t - min(seen) if seen else None
            why = guard.check(s, figs, t, age)
            if why and (d / "index.html").exists():
                log.warning("venue %s held on its last good card: %s", s, why)
                del heads[s]
                shown[s] = guard.last_head(s) or shown[s]
                card_v[s] = int(card.stat().st_mtime) if card.exists() else t
                continue
        if cards or not card.exists():
            render_card(f, heads[s], t, card, look=look)
            card_v[s] = t
        else:
            card_v[s] = int(card.stat().st_mtime)
        _write(d / "index.html", venue_page(f, heads[s], t, card_v[s]))
        if guard is not None:
            guard.accept(s, figs, heads[s], t)
    if all_facts:
        _write(out / "venues" / "index.html", index_page(all_facts, shown, t, card_v))
    if guard is not None:
        guard.save()
    return heads


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", type=Path, required=True, help="directory holding venues.json and the rest")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--look", choices=LOOKS, default="venue",
                    help="venue: each card in its venue's own style; ours: every card in the dashboard's look")
    args = ap.parse_args()
    data = {n: json.loads((args.api / f"{n}.json").read_text())
            for n in ("venues", "tokens", "execution", "lp", "perps")}
    if (args.api / "summary.json").exists():  # CC price and DefiLlama's Canton list
        data["summary"] = json.loads((args.api / "summary.json").read_text())
    heads = build(args.out, data, data["venues"]["t"], look=args.look)
    near = {s: f["near"] for s, f in facts(data).items()}
    for s, h in heads.items():
        margin = ("no lead" if h.get("margin") is None else
                  f"net +{h['margin'] * 10_000:.0f} bp over {h['next']} after network fees"
                  if h["rule"] == "best_quote" else f"+{h['margin'] * 100:.0f}% over {h['next']}")
        print(f"{s:12} {h['rule']:13} {h['title']} | {h['sub']} | {margin} | scope {h.get('scope', '-')}")
        for x in near.get(s, []):
            print(f"{'':12} not a lead: {x['rule']} only +{x['margin'] * 100:.1f}% over {x['next']}")


if __name__ == "__main__":
    main()
