"""Canton Venues: one static page and one share card per venue we read live.

Hash routes cannot carry Open Graph tags, so every venue gets a real path,
``/venues/<slug>/``, with its own title, description and a 1200x630 card image
drawn from the same JSON the dashboard reads. The collector calls ``build`` after
each tick; it can also run alone against a saved API directory:

    python venue_pages.py --api /var/www/canton-venues/api/v1 --out /var/www/canton-venues

Every number comes from the collected data. The card's headline is picked by a
fixed rule (``headline``): the first ranking the venue leads, in a set order,
or a plain count when it leads none. Nothing is written about another venue
beyond its place in a ranking.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
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

    # best execution: per scope (CC pairs on pools, dollar quotes) and size, quotes won / quoted
    wins: dict[tuple, dict] = {}
    quoted: dict[tuple, dict] = {}
    won_rows: dict[str, list] = {}
    for p in pairs:
        for r in p["rows"]:
            k = (p["kind"], r["size_usd"])
            for v, out in r["out"].items():
                if out is not None:
                    quoted.setdefault(k, {}).setdefault(v, 0)
                    quoted[k][v] += 1
            if r.get("best"):
                wins.setdefault(k, {}).setdefault(r["best"], 0)
                wins[k][r["best"]] += 1
                won_rows.setdefault(r["best"], []).append(
                    {"pair": pair_label(p, r["side"]), "kind": p["kind"], "size": r["size_usd"],
                     "edge_bps": r.get("edge_bps")})
    rows_at = {k: max(quoted[k].values()) for k in quoted}

    out = {}
    for v in VENUES:
        ids = v["rows"]
        main = rows.get(ids[0])
        if main is None:
            continue
        f = {"venue": v, "id": ids[0], "name": v["name"], "status": main["status"],
             "kind": " + ".join(dict.fromkeys(rows[i]["kind"] for i in ids if i in rows)),
             "notes": list(dict.fromkeys(rows[i]["note"] for i in ids if i in rows)),
             "caveats": [c for i in ids for c in CAVEATS.get(i.replace("_perp", ""), [])]}
        f["caveats"] = list(dict.fromkeys(f["caveats"]))
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
        # sizes where this venue wins the most quotes in a scope, and at least half of them
        best = []
        for (scope, size), w in sorted(wins.items()):
            mine = w.get(i, 0)
            if mine and mine >= max(w.values()) and mine * 2 >= rows_at[(scope, size)] \
                    and sum(x == mine for x in w.values()) == 1:
                best.append({"scope": scope, "size": size, "won": mine, "of": rows_at[(scope, size)]})
        f["exec_lead"] = best
        f["exec"] = {f"{s}:{z}": {"won": wins.get((s, z), {}).get(i, 0), "of": quoted[(s, z)].get(i, 0)}
                     for (s, z) in sorted(quoted) if quoted[(s, z)].get(i)}
        f["won_rows"] = won_rows.get(i, [])
        out[v["slug"]] = f
    return out


def headline(f: dict) -> dict:
    """The card's title and subtitle: the first ranking this venue leads, in a fixed order."""
    led = lambda key: f.get(key) == 1 and (f.get(key.replace("rank", "n")) or 0) >= 2
    if led("spot_rank"):
        share = f" ({round(f['spot_share'] * 100)}% of the spot volume we see)" if f.get("spot_share") else ""
        return {"rule": "spot_volume", "title": "The largest spot venue on Canton",
                "sub": f"{short_money(f['spot_volume'])} traded in the last 24 hours{share}"}
    if led("kind_rank"):
        return {"rule": "kind_volume", "title": f"The largest {f['kind_label']} on Canton by volume",
                "sub": f"{short_money(f['spot_volume'])} traded in the last 24 hours"}
    if led("perp_rank"):
        return {"rule": "perp_volume", "title": "The largest perps venue on Canton",
                "sub": f"{short_money(f['perp_volume'])} of perpetuals traded in the last 24 hours"}
    for scope in ("usd", "cc"):
        lead = [x for x in f.get("exec_lead", []) if x["scope"] == scope]
        if lead:
            top = lead[-1]
            what = "dollar quotes" if scope == "usd" else "CC-pair quotes"
            return {"rule": f"exec_{scope}",
                    "title": f"Best execution {size_range([x['size'] for x in lead])}",
                    "sub": f"Best price in {top['won']} of {top['of']} {what} at {SIZE_LABEL[top['size']]}, "
                           "fees and price impact included"}
    if led("deep_rank"):
        d = f["deepest"]
        return {"rule": "depth", "title": "The deepest book on Canton",
                "sub": f"{short_money(d['usd'])} of {d['symbol']} within 1% of mid"}
    if led("tvl_rank"):
        return {"rule": "tvl", "title": "The most pool liquidity on Canton",
                "sub": f"{short_money(f['tvl'])} across {len(f['pools'])} CC pools"}
    if led("token_rank"):
        return {"rule": "tokens", "title": "The most tokens on Canton",
                "sub": f"{len(f['tokens'])} tokens priced live"}
    if led("perp_mkt_rank"):
        names = ", ".join(m["base"] for m in f["perp_markets"])
        return {"rule": "perp_markets", "title": "The most perp markets on Canton",
                "sub": f"{len(f['perp_markets'])} markets: {names}"}
    if f.get("pools"):
        vol = f" and {short_money(f['spot_volume'])} traded in 24h" if f.get("spot_volume") else ""
        return {"rule": "pools", "title": f"{f['name']}, priced live on Canton",
                "sub": f"{len(f['pools'])} CC pools, {short_money(f['tvl'])} in liquidity{vol}"}
    return {"rule": "read", "title": f"{f['name']}, read live", "sub": f["kind"]}


def stats(f: dict) -> list[dict]:
    """Up to four card panels: header, value, small line under it."""
    out = []
    if f.get("spot_volume"):
        out.append({"k": "Spot volume, 24h", "v": short_money(f["spot_volume"]),
                    "n": ordinal_rank(f["spot_rank"], f["spot_n"], "spot venues", "as the venue reports it")})
    if f.get("perp_volume"):
        out.append({"k": "Perps volume, 24h", "v": short_money(f["perp_volume"]),
                    "n": ordinal_rank(f["perp_rank"], f["perp_n"], "perps venues", "as the venue reports it")})
    lead = f.get("exec_lead") or []
    if lead:
        top = lead[-1]
        out.append({"k": f"Best price at {SIZE_LABEL[top['size']]}", "v": f"{top['won']} of {top['of']}",
                    "n": "dollar quotes" if top["scope"] == "usd" else "CC-pair quotes"})
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
    "exec_usd": "Same dollar amount on every venue. Fees and price impact included.",
    "exec_cc": "Same amount on every pool. Pool fees and price impact included.",
    "depth": "Dollars resting within 1% of each book's own mid.",
    "tvl": "Pool liquidity from live reserves.",
    "tokens": "Tokens matched by Canton instrument.",
    "perp_markets": "Perp markets listed by each venue's public API.",
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


PINK = (255, 122, 196)
CYAN = (110, 231, 245)
SUB = (163, 236, 245)
GREY = (150, 164, 186)


def render_card(f: dict, head: dict, t: int, path: Path) -> None:
    """A 1200x630 PNG, drawn at 2x and scaled down so edges and text are smooth."""
    from PIL import Image, ImageDraw, ImageFilter

    S = 2
    W, H = 1200 * S, 630 * S
    # navy to teal, corner to corner
    base = Image.new("RGB", (2, 2))
    base.putdata([(8, 14, 38), (10, 34, 66), (9, 30, 60), (12, 92, 104)])
    img = base.resize((W, H), Image.BICUBIC)
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse((W * 0.55, -H * 0.4, W * 1.3, H * 0.7), fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(160 * S))
    img = Image.composite(Image.new("RGB", (W, H), (40, 120, 170)), img, glow)
    # faint stars, the same sky every time for a venue
    rnd = random.Random(f["venue"]["slug"])
    stars = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    for _ in range(170):
        x, y, r = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(0.6, 1.8) * S
        sd.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rnd.randint(25, 120)))
    img = Image.alpha_composite(img.convert("RGBA"), stars)
    d = ImageDraw.Draw(img)
    M = 64 * S

    # brand mark and name, as on the site
    for x, y, w, h, c in ((0, 0, 11, 11, (22, 199, 132)), (13, 0, 11, 7, (56, 97, 251)),
                          (13, 9, 11, 15, (235, 240, 248)), (0, 13, 11, 11, (234, 57, 67))):
        d.rounded_rectangle((M + x * S * 1.3, M + y * S * 1.3, M + (x + w) * S * 1.3, M + (y + h) * S * 1.3),
                            radius=3 * S, fill=c)
    d.text((M + 42 * S, M - 3 * S), "Canton Venues", font=_font("SemiBold", 26 * S), fill=(255, 255, 255))
    tag = f"{f['name'].upper()}  ·  {f['kind'].upper()}"
    tf = _font("SemiBold", 20 * S)
    d.text((W - M - d.textlength(tag, font=tf), M + 2 * S), tag, font=tf, fill=PINK)

    # title and subtitle
    big = _font("Bold", 74 * S)
    one = d.textlength(head["title"], font=big) <= W - 2 * M
    title_font = big if one else _font("Bold", 62 * S)
    lines = _wrap(d, head["title"], title_font, W - 2 * M, 2)
    step = (86 if one else 72) * S
    block = len(lines) * step + 50 * S
    y = 118 * S + max(0, (250 * S - block) // 2)
    for line in lines:
        d.text((M, y), line, font=title_font, fill=(255, 255, 255))
        y += step
    sub_font = _fit(d, head["sub"], "Regular", 30 * S, W - 2 * M, 22 * S)
    d.text((M, y + 4 * S), head["sub"], font=sub_font, fill=SUB)

    # glass panels with a pink-to-cyan edge
    panels = stats(f)
    if panels:
        gap, top, ph = 20 * S, 388 * S, 148 * S
        pw = (W - 2 * M - gap * (len(panels) - 1)) // len(panels)
        edge = Image.new("RGB", (2, 1))
        edge.putdata([PINK, CYAN])
        for n, p in enumerate(panels):
            x0 = M + n * (pw + gap)
            box = (x0, top, x0 + pw, top + ph)
            glass = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(glass).rounded_rectangle(box, radius=18 * S, fill=(255, 255, 255, 22))
            img = Image.alpha_composite(img, glass)
            mask = Image.new("L", (pw, ph), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=18 * S, outline=255,
                                                  width=2 * S)
            img.paste(edge.resize((pw, ph), Image.BILINEAR), (x0, top), mask)
            d = ImageDraw.Draw(img)
            pad = 22 * S
            inner = pw - 2 * pad
            d.text((x0 + pad, top + 20 * S), p["k"], font=_fit(d, p["k"], "SemiBold", 20 * S, inner, 14 * S),
                   fill=PINK)
            d.text((x0 + pad, top + 52 * S), p["v"], font=_fit(d, p["v"], "Bold", 44 * S, inner, 26 * S),
                   fill=(255, 255, 255))
            d.text((x0 + pad, top + 108 * S), p["n"], font=_fit(d, p["n"], "Regular", 18 * S, inner, 13 * S),
                   fill=GREY)

    foot = f"Data: cantonvenues.com, {stamp(t)}. {METHOD.get(head['rule'], '')}".strip()
    d = ImageDraw.Draw(img)
    d.text((M, H - M - 10 * S), foot, font=_fit(d, foot, "Regular", 19 * S, W - 2 * M, 14 * S), fill=GREY)

    out = img.convert("RGB").resize((1200, 630), Image.LANCZOS)
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
.vtop { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.vcard { display: block; width: 100%; height: auto; aspect-ratio: 1200 / 630; border-radius: 16px; border: 1px solid var(--line); background: #0a1230; }
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
.vtile img { display: block; width: 100%; height: auto; aspect-ratio: 1200 / 630; background: #0a1230; }
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
    return f"{who} on Canton Venues: {_lower_first(head['title'])}. {head['sub']}."


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
    <p class="sub">Quotes won out of quotes {e(f['name'])} could fill, at each trade size, buy and sell. Same amount on every venue; pool fees and price impact included, network fees excluded. <a href="/#execution">Compare every venue →</a></p>
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

    notes = "".join(f"<li>{e(n)}</li>" for n in [*f["notes"], *f["caveats"]])
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
