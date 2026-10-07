"""Daily cross-check: do the figures on our venue cards match what outside sources say right now?

Reads what every card shows (``venues/published.json``, written by the publish guard), fetches the
same figures independently, and compares. Two kinds of source:

- the venue's own keyless API, read directly here rather than through the collector, priced at
  CoinGecko's CC price rather than ours. Same figure, same 24h window: ``GAP`` applies.
- DefiLlama's Canton lists (DEX volume per protocol, protocol TVL on Canton). Its windows and token
  prices are its own (not checked against its docs), so only a gap over ``LLAMA_GAP`` counts.

When any gap is over its threshold, or the cards have not been republished for ``STALE_CARDS_S``,
ONE Telegram message goes from the site's bot (TELEGRAM_BOT_TOKEN, @cantonvenuesbot) to the
owner's chat (CONTACT_CHAT_ID), both from the server's .env, the same pair contact.py uses. When
everything agrees, nothing is sent. A source that fails to answer is reported as n/a, never as 0.

    python crosscheck.py --published /var/www/canton-venues/venues/published.json
    python crosscheck.py --published ... --dry-run      # print, send nothing
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("crosscheck")

GAP = 0.15             # venue API read directly, same figure: a gap over 15% is a fault on our side or theirs
LLAMA_GAP = 0.35       # DefiLlama, its own windows and prices: only a gross gap counts
STALE_CARDS_S = 2 * 3600  # no card republished for two hours: the collector or the guard is stuck

GECKO = "https://api.coingecko.com/api/v3/simple/price"
TEMPLE = "https://api.templedigitalgroup.com/api/exchange/settled_volume"
CANTEX = "https://api.cantex.io/v1/public/volume"
POOLPARTY = "https://api-mainnet.cantonwallet.com/canton/pool-party/public/v1"
ONESWAP = "https://api.oneswap.cc/swapv2/api/rt/pools"
ROCKY_PERP = "https://api.rocky.exchange/fapi/v1/ticker/24hr"
EKIDEN = "https://api.ekiden.fi/api/v1/market/tickers"
LLAMA_DEXS = "https://api.llama.fi/overview/dexs/Canton"
LLAMA_PROTOCOLS = "https://api.llama.fi/protocols"
# our venue slug -> DefiLlama protocol name (overview/dexs/Canton and /protocols, read 2026-10-07)
LLAMA_NAMES = {"temple": "Temple", "cantex": "Cantex", "rocky": "Rocky Exchange Spot", "pool-party": "Pool Party"}
# DefiLlama TVL is comparable to our pool liquidity only where the protocol is CC pools and nothing
# else: Cantex. Pool Party's DefiLlama TVL also counts its non-CC pools; Temple's is not on a card.
LLAMA_TVL = ("cantex",)

Get = Callable[[str, dict | None], object]


def http_get(url: str, params: dict | None = None) -> object:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "canton-venues-crosscheck/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# === outside figures =======================================================
# Each returns {(slug, figure): (value, source)}; a source that fails leaves its figures out.

def _cc_usd(get: Get) -> float:
    return float(get(GECKO, {"ids": "canton-network", "vs_currencies": "usd"})["canton-network"]["usd"])


def venue_figures(get: Get, now: datetime) -> dict:
    out: dict = {}

    def attempt(name: str, fn) -> None:
        try:
            out.update(fn())
        except Exception as exc:  # noqa: BLE001 - one source failing must not stop the rest
            log.warning("%s: %s", name, type(exc).__name__)
            out[("_failed", name)] = (None, name)

    try:
        cc = _cc_usd(get)
    except Exception as exc:  # noqa: BLE001
        log.warning("coingecko: %s", type(exc).__name__)
        cc = None

    def temple():
        raw = get(TEMPLE, {"start_time": (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                           "end_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        return {("temple", "spot_volume"): (float(raw["total_volume_usd"]), "Temple settled_volume")}

    def cantex():
        vol = float(get(CANTEX, None)["data"]["volume_cc"])
        return {("cantex", "spot_volume"): (vol * cc, "Cantex /volume at CoinGecko CC")}

    def poolparty():
        vol = get(f"{POOLPARTY}/volume", {"period": "24h"})
        cc_vol = sum(float((p.get("volume") or {}).get("Amulet", 0)) for p in (vol.get("perPool") or {}).values())
        tvl = get(f"{POOLPARTY}/tvl", None)
        cc_res = sum(float(r.get("Amulet", 0)) for r in (tvl.get("pools") or {}).values())
        src = "Pool Party API at CoinGecko CC"
        return {("pool-party", "spot_volume"): (cc_vol * cc, src), ("pool-party", "tvl"): (2 * cc_res * cc, src)}

    def oneswap():
        cc_res = 0.0
        for p in get(ONESWAP, None):
            if not p.get("swapsEnabled", True) or not p.get("visible", True):
                continue
            acct = p.get("accounting") or {}
            for side, res in (("assetX", "reserveX"), ("assetY", "reserveY")):
                a = p[side]
                if a.get("id") == "Amulet" and str(a.get("admin", "")).startswith("DSO::"):
                    cc_res += float(acct.get(res, p[res]))
        return {("oneswap", "tvl"): (2 * cc_res * cc, "OneSwap pools at CoinGecko CC")}

    def rocky():
        vol = sum(float(t.get("quoteVolume") or 0) for t in get(ROCKY_PERP, None))
        return {("rocky", "perp_volume"): (vol, "Rocky /fapi ticker")}

    def ekiden():
        vol = sum(float(t.get("turnover_24h") or 0) for t in get(EKIDEN, None)["list"])
        return {("ekiden", "perp_volume"): (vol, "Ekiden tickers")}

    attempt("temple", temple)
    attempt("rocky", rocky)
    attempt("ekiden", ekiden)
    if cc:
        attempt("cantex", cantex)
        attempt("poolparty", poolparty)
        attempt("oneswap", oneswap)
    else:
        for name in ("cantex", "poolparty", "oneswap"):
            out[("_failed", name)] = (None, f"{name} (no CoinGecko CC price)")
    return out


def llama_figures(get: Get) -> dict:
    out: dict = {}
    try:
        dex = {p["name"]: p.get("total24h") for p in get(LLAMA_DEXS, {"excludeTotalDataChart": "true",
                                                                    "excludeTotalDataChartBreakdown": "true"})
               .get("protocols", [])}
        for slug, name in LLAMA_NAMES.items():
            if dex.get(name):
                out[(slug, "spot_volume")] = (float(dex[name]), "DefiLlama Canton DEX volume")
    except Exception as exc:  # noqa: BLE001
        log.warning("defillama dexs: %s", type(exc).__name__)
        out[("_failed", "defillama dexs")] = (None, "defillama dexs")
    try:
        tvl = {p["name"]: (p.get("chainTvls") or {}).get("Canton") for p in get(LLAMA_PROTOCOLS, None)}
        for slug in LLAMA_TVL:
            if tvl.get(LLAMA_NAMES[slug]):
                out[(slug, "tvl")] = (float(tvl[LLAMA_NAMES[slug]]), "DefiLlama Canton TVL")
    except Exception as exc:  # noqa: BLE001
        log.warning("defillama protocols: %s", type(exc).__name__)
        out[("_failed", "defillama protocols")] = (None, "defillama protocols")
    return out


# === compare ===============================================================

def compare(published: dict, theirs: dict, limit: float) -> list[dict]:
    """Every figure both sides have, with its gap; ``over`` when the gap is past ``limit``."""
    rows = []
    venues = published.get("venues") or {}
    for (slug, fig), (value, source) in sorted(theirs.items()):
        if slug == "_failed":
            continue
        ours = ((venues.get(slug) or {}).get("figures") or {}).get(fig)
        if ours is None or not value:
            continue
        gap = ours / value - 1
        rows.append({"venue": slug, "figure": fig, "ours": ours, "theirs": value, "source": source,
                     "gap": gap, "limit": limit, "over": abs(gap) > limit})
    return rows


def _usd(v: float) -> str:
    a = abs(v)
    return (f"${v / 1e6:.2f}M" if a >= 1e6 else f"${v / 1e3:.1f}K" if a >= 1e3 else f"${v:.0f}")


def run(published: dict, get: Get = http_get, now: datetime | None = None) -> tuple[str | None, list[dict]]:
    """The message to send (None when all is fine) and every comparison made."""
    now = now or datetime.now(UTC)
    direct, llama = venue_figures(get, now), llama_figures(get)
    rows = compare(published, direct, GAP) + compare(published, llama, LLAMA_GAP)
    failed = sorted({src for (k, _), (_, src) in {**direct, **llama}.items() if k == "_failed"})
    lines = []
    ts = [v.get("t", 0) for v in (published.get("venues") or {}).values()]
    if not ts:
        lines.append("No card figures published at all (venues/published.json is empty).")
    elif now.timestamp() - max(ts) > STALE_CARDS_S:
        lines.append(f"No card republished for {(now.timestamp() - max(ts)) / 3600:.1f} h.")
    for r in rows:
        if r["over"]:
            lines.append(f"{html.escape(r['venue'])} {r['figure']}: card {_usd(r['ours'])}, "
                         f"{html.escape(r['source'])} {_usd(r['theirs'])} ({r['gap'] * 100:+.0f}%, "
                         f"limit {r['limit'] * 100:.0f}%)")
    if not lines:
        return None, rows
    text = "<b>cantonvenues.com cross-check: figures disagree</b>\n" + "\n".join(lines)
    if failed:
        text += "\nNot checked (source did not answer): " + html.escape(", ".join(failed))
    return text, rows


def send(text: str) -> bool:
    """One message from the site's bot to the owner's chat; False when not configured or it fails."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("CONTACT_CHAT_ID")
    if not (token and chat):
        log.warning("telegram: TELEGRAM_BOT_TOKEN or CONTACT_CHAT_ID not set; message not sent")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        # the URL carries the bot token: never log it
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15) as r:
            return r.status == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram: %s", type(exc).__name__)
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--published", type=Path, required=True, help="venues/published.json from the collector")
    ap.add_argument("--dry-run", action="store_true", help="print the comparison, send nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        published = json.loads(args.published.read_text())
    except (OSError, ValueError):
        published = {}
    text, rows = run(published)
    for r in rows:
        log.info("%-10s %-12s ours %12s  %-34s %12s  %+6.1f%%%s", r["venue"], r["figure"], _usd(r["ours"]),
                 r["source"], _usd(r["theirs"]), r["gap"] * 100, "  OVER" if r["over"] else "")
    if text is None:
        log.info("all %d figures within limits; nothing sent", len(rows))
    elif args.dry_run:
        print(text)
    else:
        send(text)


if __name__ == "__main__":
    main()
