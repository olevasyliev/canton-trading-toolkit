"""Alerts: rules over one tick's data, fired on crossing, with a cooldown.

An alert fires when a condition becomes true, not on every tick it stays true,
and the same alert stays quiet for ``COOLDOWN_S`` after firing. Thin pools are
skipped: a $6K pool off its peg is noise, not news.
"""

from __future__ import annotations

from dataclasses import dataclass

PEG_LIMIT = 0.005        # a stablecoin more than 0.5% from $1
PREMIUM_LIMIT = 0.01     # an asset more than 1% from its outside price
MOVE_LIMIT = 0.05        # a token moving more than 5% in an hour
MIN_LIQUIDITY_USD = 20_000
COOLDOWN_S = 6 * 3600
KEEP = 300
EMOJI = {"peg": "💵", "premium": "🌍", "move": "⚡️", "route": "🔁"}


@dataclass(frozen=True)
class Alert:
    key: str     # identifies the condition, for crossing and cooldown
    kind: str    # peg | premium | route | move
    text: str    # one line, plain text
    html: str    # the same line for Telegram (parse_mode=HTML)


def pct(f: float) -> str:
    return f"{'+' if f > 0 else ''}{f * 100:.2f}%"


def evaluate(tokens: list[dict], premium: list[dict], scan: list[dict],
             price_hour_ago: dict[str, float]) -> list[Alert]:
    """Every condition that is true right now."""
    liq = {t["key"]: t.get("liquidity_usd") or 0 for t in tokens}
    sym = {t["key"]: t["symbol"] for t in tokens}
    out: list[Alert] = []
    for a in premium:
        p = a.get("premium")
        if a.get("status") != "ok" or p is None:
            continue
        if a["key"] != "CC" and liq.get(a["key"], 0) < MIN_LIQUIDITY_USD:
            continue
        s = a.get("symbol") or a["key"]
        if a["kind"] == "peg" and abs(p) > PEG_LIMIT:
            side = "above" if p > 0 else "below"
            out.append(Alert(f"peg:{a['key']}:{side}", "peg",
                             f"{s} is {pct(p)} off its $1 peg on Canton (${a['canton_usd']:.4f}).",
                             f"<b>{s}</b> is <b>{pct(p)}</b> off its $1 peg on Canton (${a['canton_usd']:.4f})."))
        elif a["kind"] == "reference" and abs(p) > PREMIUM_LIMIT:
            side = "above" if p > 0 else "below"
            out.append(Alert(f"premium:{a['key']}:{side}", "premium",
                             f"{s} trades {pct(p)} vs {a['reference']} on Canton.",
                             f"<b>{s}</b> trades <b>{pct(p)}</b> vs {a['reference']} on Canton."))
    for r in scan:
        if r.get("clears"):
            name = sym.get(r["token"], r["token"])
            out.append(Alert(f"route:{r['token']}:{r['buy_on']}", "route",
                             f"{name}: buy on {r['buy_on'].title()}, sell on {r['sell_on'].title()}, "
                             f"${r['size_usd']:,.0f} nets ${r['net_usd']:.2f} after costs.",
                             f"<b>{name}</b>: buy on {r['buy_on'].title()}, sell on "
                             f"{r['sell_on'].title()}, ${r['size_usd']:,.0f} nets <b>${r['net_usd']:.2f}</b> after costs."))
    for t in tokens:
        before, now = price_hour_ago.get(t["key"]), t.get("price_usd")
        if not before or not now or liq.get(t["key"], 0) < MIN_LIQUIDITY_USD:
            continue
        move = now / before - 1
        if abs(move) > MOVE_LIMIT:
            side = "up" if move > 0 else "down"
            out.append(Alert(f"move:{t['key']}:{side}", "move",
                             f"{sym[t['key']]} {pct(move)} in the last hour on Canton DEXes.",
                             f"<b>{sym[t['key']]}</b> <b>{pct(move)}</b> in the last hour on Canton DEXes."))
    return out


def fire(state: dict, current: list[Alert], now: int) -> list[Alert]:
    """The alerts to send now; updates ``state`` in place.

    ``state["active"]`` holds keys true on the previous tick, so a condition
    that stays true does not fire again; ``state["last"]`` holds when each key
    last fired, for the cooldown.
    """
    active = set(state.get("active", []))
    last = state.setdefault("last", {})
    new = []
    for a in current:
        if a.key in active:
            continue
        if a.key in last and now - last[a.key] < COOLDOWN_S:
            continue
        last[a.key] = now
        new.append(a)
    state["active"] = sorted({a.key for a in current})
    feed = state.setdefault("feed", [])
    feed.extend({"t": now, "kind": a.kind, "key": a.key, "text": a.text} for a in new)
    state["feed"] = feed[-KEEP:]
    return new
