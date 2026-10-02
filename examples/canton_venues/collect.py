"""Canton Venues collector: one loop that writes the public JSON API.

Sources, all public, no keys:
  Cantex      /v1/public REST + websocket candles   (CantexPublicData)
  Tradecraft  /v1 REST                               (TradecraftAdapter)
  DefiLlama   Canton DEX volume and chain TVL        (ecosystem context)
  CoinGecko   outside prices for the premium board

Every tick (default 300 s) prices both venues from reserves; every third tick
also refreshes candles, per-pool volumes and DefiLlama. Output goes to
``<out>/api/v1/*.json``, written atomically, which the page and anyone else
reads.

    python collect.py --out /var/www/canton-venues --interval 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx
import model as m
from model import CC, VenuePool

from canton_toolkit import CantexPublicData, TradecraftAdapter
from canton_toolkit.venues.tradecraft import swap_output as tc_swap_output

log = logging.getLogger("venues")

USDCX = "USDCX"
USDCX_GECKO = "xreserve-bridged-usdc-canton"
SLOW_EVERY = 3
HISTORY_KEEP_S = 8 * 24 * 3600
DESK_KEEP = 400
LLAMA = "https://api.llama.fi"
GECKO = "https://api.coingecko.com/api/v3/simple/price"


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=float))
    os.replace(tmp, path)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def r(x, n=6):
    return None if x is None else round(float(x), n)


class Collector:
    def __init__(self, out: Path) -> None:
        self.api = out / "api" / "v1"
        self.cantex = CantexPublicData()
        self.tradecraft = TradecraftAdapter()
        self.http = httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "canton-venues/1"})
        self.tick_no = 0
        self.slow: dict = {}
        self.history = load_json(self.api / "history.json", {"t": [], "usd": {}, "premium": {}})
        self.desk = load_json(self.api / "desk.json", {})
        self.desk.setdefault("router", {"fills": [], "n": 0, "extra_usd": 0.0, "by_venue": {}})
        self.desk.setdefault("arb", {"trades": [], "pnl_usd": 0.0, "seen": {}})
        self.desk.setdefault("pnl", [])

    async def start(self) -> None:
        await self.cantex.connect()
        await self.tradecraft.connect()

    async def stop(self) -> None:
        await self.cantex.close()
        await self.tradecraft.close()
        await self.http.aclose()

    # === sources ===========================================================

    async def _json(self, url: str, params: dict | None = None):
        resp = await self.http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _refresh_slow(self, tc_states, cantex_markets) -> None:
        """Candles, per-pool Tradecraft volume, DefiLlama. Each part fails alone."""
        try:
            symbols = [mk["symbol"] for mk in cantex_markets if mk.get("source") == "cantex"]
            self.slow["candles"] = await self.cantex.candles(symbols, 3600, timeout=40)
        except Exception as exc:  # noqa: BLE001
            log.warning("candles: %s", exc)
        try:
            vols = {}
            for st in tc_states:
                v = await self.tradecraft.pool_volume_usd(st.token_a, st.token_b)
                vols[st.amm_id] = float(v.get("1d", 0))
            self.slow["tc_volume"] = vols
        except Exception as exc:  # noqa: BLE001
            log.warning("tradecraft volume: %s", exc)
        try:
            dex = await self._json(f"{LLAMA}/overview/dexs/Canton",
                                   {"excludeTotalDataChart": "true",
                                    "excludeTotalDataChartBreakdown": "true"})
            chains = await self._json(f"{LLAMA}/v2/chains")
            self.slow["llama"] = {
                "dex_volume_24h": dex.get("total24h"),
                "dex_volume_7d": dex.get("total7d"),
                "dex_change_1d": dex.get("change_1d"),
                "venues": sorted(({"name": p["name"], "volume_24h": p.get("total24h")}
                                  for p in dex.get("protocols", [])),
                                 key=lambda p: -(p["volume_24h"] or 0)),
                "tvl": next((c["tvl"] for c in chains if c["name"] == "Canton"), None),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("defillama: %s", exc)
        try:
            self.slow["tokens_info"] = await self.cantex.tokens()
        except Exception as exc:  # noqa: BLE001
            log.warning("tokens info: %s", exc)

    async def _gecko(self) -> dict:
        ids = sorted({g for g, _ in m.REFERENCES.values()} | {USDCX_GECKO})
        try:
            return await self._json(GECKO, {"ids": ",".join(ids), "vs_currencies": "usd",
                                            "include_24hr_change": "true"})
        except Exception as exc:  # noqa: BLE001
            log.warning("coingecko: %s", exc)
            return self.slow.get("gecko_last", {})

    # === building the books ================================================

    def _books(self, cx_states, tc_states, tc_pools, lp_share_cx):
        """symbol -> venue -> VenuePool, CC-paired pools only, matched by instrument."""
        cc_inst = next(s.token_a if s.symbol_a == CC else s.token_b
                       for s in cx_states if CC in (s.symbol_a, s.symbol_b))
        names: dict = {}
        for t in self.slow.get("tokens_info", []):
            names[(t["instrument_admin"], t["instrument_id"])] = t["instrument_symbol"]
        books: dict[str, dict[str, VenuePool]] = {}
        symbol_of: dict = {}

        for s in cx_states:
            if cc_inst not in (s.token_a, s.token_b):
                continue
            cc_is_a = s.token_a == cc_inst
            tok_inst, sym = (s.token_b, s.symbol_b) if cc_is_a else (s.token_a, s.symbol_a)
            sym = names.get((tok_inst.admin, tok_inst.id), sym)
            symbol_of[tok_inst] = sym
            cc_res, tok_res = (s.reserve_a, s.reserve_b) if cc_is_a else (s.reserve_b, s.reserve_a)
            if cc_res <= 0 or tok_res <= 0:
                continue

            def out(sell_cc, amount, s=s, tok_inst=tok_inst):
                return s.output(cc_inst, tok_inst, amount) if sell_cc else s.output(tok_inst, cc_inst, amount)

            pool = VenuePool("cantex", sym, cc_res, tok_res, s.fee, lp_share_cx, out)
            prev = books.setdefault(m.key(sym), {}).get("cantex")
            if prev is None or pool.cc_reserve > prev.cc_reserve:
                books[m.key(sym)]["cantex"] = pool

        tc_inst = {p.contract_id: (p.token_a, p.token_b) for p in tc_pools}
        for st in tc_states:
            if CC not in (st.token_a, st.token_b) or st.amm_id not in tc_inst:
                continue
            ia, ib = tc_inst[st.amm_id]
            tok_sym = st.token_b if st.token_a == CC else st.token_a
            tok_inst = ib if st.token_a == CC else ia
            sym = symbol_of.get(tok_inst) or names.get((tok_inst.admin, tok_inst.id), tok_sym)
            cc_res, tok_res = st.reserves_for(CC, tok_sym)
            if cc_res <= 0 or tok_res <= 0:
                continue

            def out(sell_cc, amount, st=st, tok_sym=tok_sym):
                rin, rout = st.reserves_for(CC, tok_sym) if sell_cc else st.reserves_for(tok_sym, CC)
                return tc_swap_output(rin, rout, amount, st.total_fee)

            share = st.lp_fee / st.total_fee if st.total_fee else Decimal(0)
            books.setdefault(m.key(sym), {})["tradecraft"] = VenuePool(
                "tradecraft", sym, cc_res, tok_res, st.realized_fee, share, out)
        return books

    def _usd_series(self, now_ms: int, usdcx_usd: Decimal) -> dict[str, list]:
        """Hourly dollar series per token from Cantex candles (X-CC times CC-USDCX)."""
        candles = self.slow.get("candles") or {}
        base = next((v for k, v in candles.items() if k.upper() == "CC-USDCX"), None)
        if not base:
            return {}
        cc_at = {c.start_ms: c.close for c in base}
        series = {CC: [(t, float(p * usdcx_usd)) for t, p in sorted(cc_at.items())]}
        for market, bars in candles.items():
            b, _, q = market.upper().rpartition("-")
            if q != CC or not b:
                continue
            pts = [(c.start_ms, float(c.close * cc_at[c.start_ms] * usdcx_usd))
                   for c in bars if c.start_ms in cc_at]
            if pts:
                series[b] = pts
        return series

    # === one tick ==========================================================

    async def tick(self) -> None:
        started = time.monotonic()
        now = int(time.time())
        now_ms = now * 1000
        cx_states = await self.cantex.pool_states()
        tc_states = await self.tradecraft.pool_states()
        tc_pools = await self.tradecraft.pools()
        if self.tick_no % SLOW_EVERY == 0 or not self.slow:
            markets = await self.cantex.markets()
            await self._refresh_slow(tc_states, markets)
        cx_volume = await self.cantex.volume()
        tickers = await self.cantex.tickers()
        gecko = await self._gecko()
        if gecko:
            self.slow["gecko_last"] = gecko

        fees_cc = Decimal(cx_volume.get("fees_cc") or 0)
        lp_share_cx = Decimal(cx_volume["lp_fees_cc"]) / fees_cc if fees_cc else Decimal("0.9")
        books = self._books(cx_states, tc_states, tc_pools, lp_share_cx)
        usdcx_usd = Decimal(str(gecko.get(USDCX_GECKO, {}).get("usd", 1)))
        stable = books.get(USDCX)
        if not stable:
            raise RuntimeError("no CC/USDCx pool on any venue")
        cc_usd = m.cc_usd(list(stable.values()), usdcx_usd)

        series = self._usd_series(now_ms, usdcx_usd)
        self._record(now, cc_usd, books)
        own = self._own_series()

        tokens = self._tokens(books, cc_usd, tickers, series, own, now_ms)
        premium = self._premium(books, cc_usd, gecko, tokens)
        execution = self._execution(books, cc_usd)
        scan = m.scan({k: v for k, v in books.items() if len(v) > 1}, cc_usd)
        self._paper(books, cc_usd, scan, now)
        lp = self._lp(books, cc_usd, tickers, tc_states)
        summary = self._summary(cc_usd, cx_volume, gecko, series, now_ms, books, now,
                                time.monotonic() - started)

        write_json(self.api / "summary.json", summary)
        write_json(self.api / "tokens.json", {"t": now, "tokens": tokens})
        write_json(self.api / "premium.json", {"t": now, "assets": premium})
        write_json(self.api / "execution.json", {"t": now, "sizes_usd": list(m.SIZES_USD),
                                                  "pairs": execution})
        write_json(self.api / "scan.json", {"t": now, "round_trip_cost_cc": float(m.ROUND_TRIP_COST_CC),
                                            "min_usd": float(m.MIN_ROUTE_USD), "routes": scan})
        write_json(self.api / "desk.json", self.desk)
        write_json(self.api / "lp.json", {"t": now, "pools": lp})
        write_json(self.api / "history.json", self.history)
        self.tick_no += 1
        log.info("tick %d: %d tokens, %d on 2 venues, %d routes clear, %.1fs", self.tick_no,
                 len(tokens), sum(len(v) > 1 for v in books.values()),
                 sum(s["clears"] for s in scan), time.monotonic() - started)

    # === sections ==========================================================

    def _record(self, now: int, cc_usd: Decimal, books) -> None:
        h = self.history
        h["t"].append(now)
        n = len(h["t"])
        usd = {CC: float(cc_usd)}
        for sym, pools in books.items():
            usd[sym] = float(m.token_usd(list(pools.values()), cc_usd))
        for sym in set(h["usd"]) | set(usd):
            col = h["usd"].setdefault(sym, [None] * (n - 1))
            col.append(r(usd.get(sym), 8))
        cut = next((i for i, t in enumerate(h["t"]) if t >= now - HISTORY_KEEP_S), 0)
        if cut:
            h["t"] = h["t"][cut:]
            for k in h["usd"]:
                h["usd"][k] = h["usd"][k][cut:]
            for k in h["premium"]:
                h["premium"][k] = h["premium"][k][cut:]

    def _own_series(self) -> dict[str, list]:
        h = self.history
        return {sym: [(t * 1000, v) for t, v in zip(h["t"], col) if v is not None]
                for sym, col in h["usd"].items()}

    def _tokens(self, books, cc_usd, tickers, series, own, now_ms) -> list[dict]:
        vol_cx: dict[str, float] = {}
        for t in tickers:
            base, target = m.key(t["base_currency"]), m.key(t["target_currency"])
            if target == CC:
                vol_cx[base] = vol_cx.get(base, 0) + float(t["target_volume"]) * float(cc_usd)
            elif base == CC:
                vol_cx[target] = vol_cx.get(target, 0) + float(t["base_volume"]) * float(cc_usd)
        tc_vol = self.slow.get("tc_volume", {})
        out = []
        for sym, pools in books.items():
            price = m.token_usd(list(pools.values()), cc_usd)
            venues = {}
            for v, p in pools.items():
                venues[v] = {"price_usd": r(cc_usd / p.mid, 8),
                             "liquidity_usd": r(2 * p.cc_reserve * cc_usd, 2)}
            vol = vol_cx.get(sym, 0.0)
            if "tradecraft" in pools:
                vol += sum(v for k, v in tc_vol.items()
                           if k.upper().replace("TC ", "").replace(" LP", "") in
                           (f"CC/{sym}", f"{sym}/CC"))
            src = series.get(sym) or own.get(sym) or []
            spark = [round(v, 8) for t, v in src if t >= now_ms - 7 * 86400_000][-168:]
            out.append({
                "symbol": pools[next(iter(pools))].token,
                "key": sym,
                "price_usd": r(price, 8),
                "liquidity_usd": r(sum(x["liquidity_usd"] for x in venues.values()), 2),
                "volume_24h_usd": r(vol, 2),
                "change_24h": r(m.change(src, now_ms, 86400_000)),
                "change_7d": r(m.change(src, now_ms, 7 * 86400_000)),
                "history_source": "cantex candles" if sym in series else "own samples",
                "venues": venues,
                "spark_7d": spark,
            })
        return sorted(out, key=lambda t: -(t["liquidity_usd"] or 0))

    def _premium(self, books, cc_usd, gecko, tokens) -> list[dict]:
        rows = []
        h = self.history
        n = len(h["t"])

        def keep(sym, val):
            col = h["premium"].setdefault(sym, [None] * (n - 1))
            col.extend([None] * (n - 1 - len(col)))
            col.append(r(val, 6))

        candidates = [(CC, None)] + [(s, p) for s, p in books.items()]
        for sym, pools in candidates:
            if sym in m.REFERENCES:
                gid, label = m.REFERENCES[sym]
                ref = gecko.get(gid, {}).get("usd")
                kind = "reference"
            elif sym in m.STABLES:
                gid, label, ref, kind = None, "1.00 peg", 1.0, "peg"
            else:
                continue
            canton = cc_usd if sym == CC else m.token_usd(list(pools.values()), cc_usd)
            if ref is None:
                rows.append({"key": sym, "kind": kind, "reference": label, "status": "no reference price"})
                continue
            prem = m.premium(canton, Decimal(str(ref)))
            per_venue = {}
            if pools:
                for v, p in pools.items():
                    pv = m.premium(cc_usd / p.mid, Decimal(str(ref)))
                    per_venue[v] = r(pv, 6)
            keep(sym, prem)
            rows.append({
                "key": sym,
                "symbol": next(iter(pools.values())).token if pools else CC,
                "kind": kind,
                "reference": label,
                "reference_id": gid,
                "canton_usd": r(canton, 8),
                "reference_usd": r(ref, 8),
                "premium": r(prem, 6),
                "premium_by_venue": per_venue,
                "status": "ok" if prem is not None else "unit mismatch, excluded",
            })
        for k, col in h["premium"].items():
            col.extend([None] * (n - len(col)))
        return rows

    def _execution(self, books, cc_usd) -> list[dict]:
        out = []
        for sym, pools in books.items():
            if len(pools) < 2:
                continue
            mid = m.blended_mid(list(pools.values()))
            rows = m.ladder(pools, cc_usd, mid)
            out.append({
                "key": sym,
                "symbol": next(iter(pools.values())).token,
                "mid": {v: r(p.mid, 12) for v, p in pools.items()},
                "rows": rows,
                "crossover_usd": {s: m.crossover_usd(rows, s, "cantex", "tradecraft")
                                  for s in ("sell", "buy")},
            })
        return sorted(out, key=lambda p: (p["key"] != USDCX, p["key"]))

    def _paper(self, books, cc_usd, scan, now) -> None:
        router = self.desk["router"]
        pools = books.get(USDCX, {})
        side = "sell" if router["n"] % 2 == 0 else "buy"
        fill = m.route_order(pools, side, m.ROUTER_SIZE_USD, cc_usd,
                             m.blended_mid(list(pools.values()))) if pools else None
        if fill:
            fill["t"] = now
            router["n"] += 1
            router["extra_usd"] = round(router["extra_usd"] + fill["extra_usd"], 4)
            router["by_venue"][fill["venue"]] = router["by_venue"].get(fill["venue"], 0) + 1
            router.setdefault("since", now)
            router["fills"] = (router["fills"] + [fill])[-DESK_KEEP:]

        arb = self.desk["arb"]
        for route in scan:
            if not route["clears"]:
                continue
            pair = books[route["token"]]
            fp = m.fingerprint(pair[route["buy_on"]], pair[route["sell_on"]])
            k = f'{route["token"]}:{route["buy_on"]}>{route["sell_on"]}'
            if arb["seen"].get(k) == fp:
                continue  # the same standing spread; booked when it first appeared
            arb["seen"][k] = fp
            arb["pnl_usd"] = round(arb["pnl_usd"] + route["net_usd"], 4)
            arb["trades"] = (arb["trades"] + [{**route, "t": now}])[-DESK_KEEP:]
        arb.setdefault("since", now)
        self.desk["pnl"] = (self.desk["pnl"] + [[now, round(router["extra_usd"], 4),
                                                 round(arb["pnl_usd"], 4)]])[-2400:]

    def _lp(self, books, cc_usd, tickers, tc_states) -> list[dict]:
        cx_vol = {}
        for t in tickers:
            pair = {m.key(t["base_currency"]), m.key(t["target_currency"])}
            if CC in pair:
                other = (pair - {CC}).pop() if len(pair) > 1 else CC
                v = float(t["target_volume"] if m.key(t["target_currency"]) == CC else t["base_volume"])
                cx_vol[other] = cx_vol.get(other, 0) + v * float(cc_usd)
        tc_vol = self.slow.get("tc_volume", {})
        tc_by_sym = {}
        for st in tc_states:
            if CC in (st.token_a, st.token_b):
                tc_by_sym[m.key(st.token_b if st.token_a == CC else st.token_a)] = st.amm_id
        rows = []
        for sym, pools in books.items():
            for v, p in pools.items():
                tvl = float(2 * p.cc_reserve * cc_usd)
                vol = cx_vol.get(sym) if v == "cantex" else tc_vol.get(tc_by_sym.get(sym, ""), None)
                rows.append({
                    "venue": v, "pair": f"CC/{p.token}", "tvl_usd": round(tvl, 2),
                    "volume_24h_usd": r(vol, 2), "fee": r(p.fee, 6),
                    "fee_apr": r(m.fee_apr(vol, float(p.fee), float(p.lp_share), tvl), 6)
                    if vol is not None else None,
                })
        return sorted(rows, key=lambda x: -x["tvl_usd"])

    def _summary(self, cc_usd, cx_volume, gecko, series, now_ms, books, now, took) -> dict:
        cc_series = series.get(CC) or self._own_series().get(CC, [])
        return {
            "t": now,
            "took_s": round(took, 1),
            "status": "ok",
            "cc_usd": r(cc_usd, 8),
            "cc_change_24h": r(m.change(cc_series, now_ms, 86400_000)),
            "cc_global_usd": r(gecko.get("canton-network", {}).get("usd"), 8),
            "cantex_24h": {"volume_cc": r(cx_volume.get("volume_cc"), 2),
                           "volume_usd": r(Decimal(cx_volume.get("volume_cc", 0)) * cc_usd, 2),
                           "swaps": int(Decimal(cx_volume.get("swap_count", 0))),
                           "fees_cc": r(cx_volume.get("fees_cc"), 2)},
            "ecosystem": self.slow.get("llama"),
            "tokens": len(books),
            "on_two_venues": sum(len(v) > 1 for v in books.values()),
            "venues_priced": ["cantex", "tradecraft"],
        }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    c = Collector(args.out)
    await c.start()
    try:
        while True:
            began = time.monotonic()
            try:
                await c.tick()
            except Exception as exc:  # one bad tick must not stop the service
                log.exception("tick failed")
                s = load_json(c.api / "summary.json", {})
                s.update(status="error", error=str(exc)[:300], error_t=int(time.time()))
                write_json(c.api / "summary.json", s)
            if args.once:
                break
            await asyncio.sleep(max(5, args.interval - (time.monotonic() - began)))
    finally:
        await c.stop()


if __name__ == "__main__":
    asyncio.run(main())
