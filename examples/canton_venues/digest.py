"""The daily market note: one message a day built from the same data as the page."""

from __future__ import annotations

from datetime import datetime, timezone

from model import STABLES

SEND_HOUR_UTC = 9
MIN_LIQUIDITY_USD = 20_000
PEG_LIMIT = 0.005


def pct(f: float, d: int = 1) -> str:
    return f"{'+' if f > 0 else ''}{f * 100:.{d}f}%".replace("-", "−")


def money(v: float) -> str:
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.2f}"


def due(state: dict, now: datetime) -> bool:
    """True once a day, on the first tick at or after the send hour."""
    return now.hour >= SEND_HOUR_UTC and state.get("daily_date") != now.date().isoformat()


def compose(summary: dict, tokens: list[dict], premium: list[dict], desk: dict,
            now: datetime) -> str:
    """The note as Telegram HTML."""
    eco = summary.get("ecosystem") or {}
    lines = [f"☀️ <b>Canton DEX daily</b> · {now.strftime('%-d %b')}", ""]
    cc, ch = summary.get("cc_usd"), summary.get("cc_change_24h")
    if cc:
        lines.append(f"🪙 CC <b>${cc:.4f}</b>" + (f" ({pct(ch)})" if ch is not None else ""))
    if eco.get("dex_volume_24h"):
        vol = f"📊 DEX volume 24h <b>{money(eco['dex_volume_24h'])}</b>"
        if eco.get("dex_change_1d") is not None:
            vol += f" ({pct(eco['dex_change_1d'] / 100)})"
        lines.append(vol)
    swaps = (summary.get("cantex_24h") or {}).get("swaps")
    if swaps:
        lines.append(f"🔄 Cantex swaps 24h <b>{swaps:,}</b>")

    movers = [t for t in tokens if t.get("change_24h") is not None
              and (t.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD and t["key"] not in STABLES]
    movers.sort(key=lambda t: t["change_24h"], reverse=True)
    up = [t for t in movers if t["change_24h"] > 0][:3]
    down = [t for t in reversed(movers) if t["change_24h"] < 0][:3]
    if up or down:
        lines.append("")
    if up:
        lines.append("📈 " + ", ".join(f"{t['symbol']} {pct(t['change_24h'])}" for t in up))
    if down:
        lines.append("📉 " + ", ".join(f"{t['symbol']} {pct(t['change_24h'])}" for t in down))

    liq = {t["key"]: t.get("liquidity_usd") or 0 for t in tokens}
    ok = [a for a in premium if a.get("status") == "ok" and a.get("premium") is not None
          and (a["key"] == "CC" or liq.get(a["key"], 0) >= MIN_LIQUIDITY_USD)]
    refs = sorted((a for a in ok if a["kind"] == "reference"), key=lambda a: -abs(a["premium"]))[:3]
    if refs:
        lines.append("")
        lines.append("🌍 vs world: " + ", ".join(f"{a['symbol']} {pct(a['premium'], 2)}" for a in refs))
    off = [a for a in ok if a["kind"] == "peg" and abs(a["premium"]) > PEG_LIMIT]
    if off:
        lines.append("💵 Off peg: " + ", ".join(f"{a['symbol']} {pct(a['premium'], 2)}" for a in off))
    else:
        lines.append("💵 Every stablecoin within 0.5% of $1")

    router = (desk or {}).get("router") or {}
    if router.get("median_edge_bps") is not None:
        lines.append("")
        lines.append(f"🔁 Better venue beats the other by a median <b>{router['median_edge_bps'] / 100:.2f}%</b> "
                     f"on a $1K CC order (last {router['window']:,}, paper)")
    lines += ["", '🔗 <a href="https://cantonvenues.com">cantonvenues.com</a>']
    return "\n".join(lines)


def today(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)
