"""The Telegram channel's morning post: a different kind each weekday, built from our own records.

    Mon  the weekly card (weekly.py) with its total and venue shares
    Tue  one venue's share card and headline, venues in turn
    Wed  where a CC trade got the best price over the week, net of each venue's network fee
    Thu  the next venue's card
    Fri  stablecoins and world-price premiums over the week
    Sat, Sun  nothing

Each builder returns a ``Post`` or None when its data is not there (a held weekly card, a week of
history not yet recorded); a day with nothing to say stays quiet rather than falling back to a
template. No DefiLlama figure appears in any post.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from model import REFERENCES, STABLES
from venue_pages import VENUES

SITE = "https://cantonvenues.com"
SEND_HOUR_UTC = 9
# weekday() -> kind; Saturday and Sunday are left out
SCHEDULE = {0: "weekly", 1: "venue", 2: "execution", 3: "venue", 4: "pegs"}
MIN_LIQUIDITY_USD = 100_000   # a stablecoin or token below this is not news for the channel
PEG_BAND = 0.005
HELD_SAMPLES = 6              # five-minute readings: a peg gap counts once it holds 30 minutes
WEEK_S = 7 * 86400
MIN_EXEC_HOURS = 24           # less history than this and the execution post waits
EXEC_PAIR = "USDCX"           # CC against USDCx, the pair every CC venue quotes
EXEC_SIZES = (1_000, 10_000, 50_000)
NAMES = {"cantex": "Cantex", "tradecraft": "Tradecraft", "rocky": "Rocky", "temple": "Temple",
         "oneswap": "OneSwap", "poolparty": "Pool Party", "ekiden": "Ekiden"}


@dataclass(frozen=True)
class Post:
    kind: str
    html: str                 # Telegram HTML; a photo's caption when ``photo`` is set
    photo: str | None = None  # an image URL Telegram fetches itself


def due(state: dict, now: datetime) -> str | None:
    """Today's kind, once, on the first tick at or after the send hour; None otherwise."""
    if now.hour < SEND_HOUR_UTC or state.get("date") == now.date().isoformat():
        return None
    return SCHEDULE.get(now.weekday())


def signed(f: float, d: int = 1) -> str:
    return f"{'+' if f > 0 else ''}{f * 100:.{d}f}%".replace("-", "−")


def money(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _day(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%-d %b %H:%M UTC")


def _span(seconds: float) -> str:
    days = round(seconds / 86400)
    return "last 7 days" if seconds >= 6.5 * 86400 else "last 24 hours" if days <= 1 else f"last {days} days"


def held_worst(pts: list[tuple[int, float]], n: int) -> tuple[int, float]:
    """The furthest from zero a reading stayed for ``n`` samples in a row (its start, its smallest
    size in that run): one odd five-minute reading is not a depeg."""
    best = (pts[0][0], 0.0)
    for i in range(len(pts) - n + 1):
        run = [v for _, v in pts[i:i + n]]
        if all(v > 0 for v in run) or all(v < 0 for v in run):
            v = min(run, key=abs)
            if abs(v) > abs(best[1]):
                best = (pts[i][0], v)
    return best


# === Monday: the weekly card ===============================================

def weekly_post(w: dict | None, now: datetime) -> Post | None:
    """The card for the seven days that closed at midnight; None when it is not that week's (held)."""
    if not w or w.get("slug") != (now.date() - timedelta(days=1)).isoformat():
        return None
    lines = [f"📅 <b>{w['label']}</b>, {w['range']}", f"<b>{money(w['total_usd'])}</b>"
             + (f" ({signed(w['change'])} on the week before)" if w.get("change") is not None else "")]
    shown = w.get("shares_shown") or []
    parts = [f"{v['name']} {s}" for v, s in zip(w["venues"], shown)][:4]
    if parts:
        lines.append(", ".join(parts))
    lines += ["", f'🔗 <a href="{SITE}/weekly/{w["slug"]}/">The week, day by day</a>']
    return Post("weekly", "\n".join(lines), f"{SITE}/weekly/{w['slug']}/card.png?v={w['t']}")


# === Tuesday, Thursday: one venue ==========================================

def venue_post(published: dict, turn: int, card_v: dict[str, int]) -> tuple[Post | None, int]:
    """The next venue, in the site's order, that has a published headline and card, and the turn after it."""
    order = [v["slug"] for v in VENUES]
    heads = (published or {}).get("venues") or {}
    for i in range(len(order)):
        slug = order[(turn + i) % len(order)]
        head = (heads.get(slug) or {}).get("head")
        if not head or slug not in card_v:
            continue
        name = next(v["name"] for v in VENUES if v["slug"] == slug)
        html = "\n".join([f"🏛 <b>{name}</b>: {head['title']}", head["sub"], "",
                          f'🔗 <a href="{SITE}/venues/{slug}/">{name} on Canton Venues</a>'])
        return (Post("venue", html, f"{SITE}/venues/{slug}/card.png?v={card_v[slug]}"),
                (turn + i + 1) % len(order))
    return None, turn


# === Wednesday: best execution over the week ===============================

def read_exec(directory: Path, now: int) -> list[dict]:
    """The hourly samples (exec_history.py) of the last seven days."""
    out = []
    for k in range(8):
        path = directory / (datetime.fromtimestamp(now - k * 86400, UTC).strftime("%Y-%m-%d") + ".jsonl")
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            s = json.loads(line)
            if s["t"] >= now - WEEK_S:
                out.append(s)
    return sorted(out, key=lambda s: s["t"])


def execution_post(samples: list[dict]) -> Post | None:
    """For a CC order at each size: which venue returned the most, net of network fees, how often."""
    if len(samples) < MIN_EXEC_HOURS:
        return None
    wins: dict[int, Counter] = defaultdict(Counter)
    edges: dict[int, list[float]] = defaultdict(list)
    for s in samples:
        for pair, _side, size, _best, _edge, best_net, edge_net in s["rows"]:
            if pair == EXEC_PAIR and size in EXEC_SIZES and best_net:
                wins[size][best_net] += 1
                edges[size].append(edge_net)
    if not wins:
        return None
    span = _span(samples[-1]["t"] - samples[0]["t"])
    lines = [f"🏁 <b>Where a CC trade got the best price</b>, {span}", ""]
    for size in EXEC_SIZES:
        c = wins.get(size)
        if not c:
            continue
        n = sum(c.values())
        ranked = c.most_common()
        top, k = ranked[0]
        line = f"<b>{money(size)}</b>: {NAMES.get(top, top)} {k * 100 // n}% of the time"
        if len(ranked) > 1:
            line += ", " + ", ".join(f"{NAMES.get(v, v)} {x * 100 // n}%" for v, x in ranked[1:3])
        line += f". The next venue returned a median {statistics.median(edges[size]) / 100:.2f}% less"
        lines.append(line)
    lines += ["", "Buys and sells of CC for USDCx, checked every hour. Pool fees, price impact and each "
              "venue's network fee included.",
              f'🔗 <a href="{SITE}/#execution">Live best execution</a>']
    return Post("execution", "\n".join(lines))


# === Friday: pegs and premiums over the week ===============================

def pegs_post(history: dict, tokens: list[dict], symbols: dict[str, str], now: int) -> Post | None:
    """Stablecoins: share of the week within 0.5% of $1 and the worst reading. Reference assets: the
    median premium to their outside price. Only tokens with ``MIN_LIQUIDITY_USD`` in pools today."""
    ts = history.get("t") or []
    lo = next((i for i, t in enumerate(ts) if t >= now - WEEK_S), None)
    if lo is None or not ts or ts[-1] - ts[lo] < 3 * 86400:
        return None
    liq = {t["key"]: t.get("liquidity_usd") or 0 for t in tokens}
    span = _span(ts[-1] - ts[lo])
    stable, ref = [], []
    for key, col in (history.get("premium") or {}).items():
        pts = [(t, v) for t, v in zip(ts[lo:], col[lo:]) if v is not None]
        if len(pts) < 100:
            continue
        name = symbols.get(key, key)
        if key in STABLES and liq.get(key, 0) >= MIN_LIQUIDITY_USD:
            inside = sum(abs(v) <= PEG_BAND for _, v in pts) / len(pts)
            worst_t, worst = held_worst(pts, HELD_SAMPLES)
            stable.append((inside, name, worst, worst_t))
        elif key in REFERENCES and (key == "CC" or liq.get(key, 0) >= MIN_LIQUIDITY_USD):
            ref.append((name, REFERENCES[key][1], statistics.median(v for _, v in pts)))
    if not stable and not ref:
        return None
    lines = [f"💵 <b>Stablecoins on Canton</b>, {span}"]
    for inside, name, worst, worst_t in sorted(stable, key=lambda x: (-x[0], x[1])):
        line = f"{name}: within 0.5% of $1 {inside * 100:.0f}% of the time"
        if abs(worst) > PEG_BAND:
            line += f", furthest {signed(worst, 2)} held 30 min ({_day(worst_t)})"
        lines.append(line)
    if ref:
        lines += ["", "🌍 <b>Against outside prices</b>, median"]
        lines += [f"{name} {signed(p, 2)} vs {label}" for name, label, p in sorted(ref, key=lambda x: -abs(x[2]))]
    lines += ["", "Read every five minutes from Canton pools and order books.",
              f'🔗 <a href="{SITE}/#premium">Live premiums</a>']
    return Post("pegs", "\n".join(lines))
