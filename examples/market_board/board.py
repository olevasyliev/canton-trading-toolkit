"""Cross-venue market board: Cantex against Tradecraft, priced live on mainnet.

One process, one loop. Every tick:

1. Pools on both venues are matched by Canton instrument (admin party + id),
   never by symbol, so a pair is only compared when it is the same two tokens
   on the ledger.
2. Each matched pair is priced at four sizes ($100, $1K, $10K, $50K) in both
   directions. Tradecraft is priced locally from its reserves with the venue's
   own formula (``swap_output`` reproduces its quotes to the digit); Cantex is
   quoted by its API. Cantex's flat network fee is taken off its output.
   Tradecraft publishes no network fee, so none is taken off its side.
3. Spread scan: sell CC for the token on one venue, sell the token back for CC
   on the other, at each size. A route is logged only when it returns at least
   $0.50 more than it started with, after every fee the venues publish.
4. Paper router: one $1,000 CC/USDCx order per tick, alternating sides, filled
   on whichever venue returns more. Nothing is ever executed or signed.

Writes ``board.json`` (latest snapshot), ``paper.json`` (router log) and
``history.json`` (mid prices) into ``--out``. Read-only: the Cantex key is the
operator key, used for its challenge-response login, and no trading key is
loaded.

    CANTEX_OPERATOR_KEY=... CANTEX_BASE_URL=https://api.cantex.io \\
        python board.py --out ./site/data --interval 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from canton_toolkit import CantexAdapter
from canton_toolkit.core.models import Instrument, Quote
from canton_toolkit.core.venue import VenueError
from canton_toolkit.venues.tradecraft import PoolState, TradecraftAdapter, swap_output

log = logging.getLogger("board")

SIZES_USD = (Decimal(100), Decimal(1_000), Decimal(10_000), Decimal(50_000))
ROUTER_SIZE_USD = Decimal(1_000)
HISTORY_SECONDS = 7 * 24 * 3600
PAPER_KEEP = 500
SCAN_KEEP = 300
CANTEX_CONCURRENCY = 4
# A round trip has to clear this in USD to be logged: Tradecraft's network cost
# is unpublished and not in our numbers, so a few cents of edge is not an edge.
MIN_ROUTE_USD = Decimal("0.50")
CC = "CC"
USD_STABLE = "USDCx"


# === pure pricing ==========================================================


@dataclass(frozen=True)
class Side:
    """One direction of a pair: what is sold and what comes back."""

    sell: str
    buy: str


@dataclass
class PairBook:
    """A matched pair and everything priced for it this tick."""

    token: str  # the non-CC symbol
    tc_state: PoolState
    cantex_sell_cc: Instrument  # CC as Cantex names it
    cantex_token: Instrument
    # (side, size_usd) -> output on each venue, net of the venue's published fees
    tc_out: dict[tuple[Side, Decimal], Decimal] = field(default_factory=dict)
    cx_out: dict[tuple[Side, Decimal], Decimal] = field(default_factory=dict)
    cx_mid: Decimal | None = None  # token per CC on Cantex, before any trade

    @property
    def tc_mid(self) -> Decimal:
        """Token per CC on Tradecraft, fees excluded."""
        return self.tc_state.price(CC, self.token)


def sell_amount(side: Side, size_usd: Decimal, cc_usd: Decimal, token_per_cc: Decimal) -> Decimal:
    """How much of ``side.sell`` is worth ``size_usd``."""
    cc_amount = size_usd / cc_usd
    return cc_amount if side.sell == CC else cc_amount * token_per_cc


def tc_output(state: PoolState, side: Side, amount: Decimal) -> Decimal:
    """Tradecraft's own price for a swap, from reserves."""
    reserve_in, reserve_out = state.reserves_for(side.sell, side.buy)
    return swap_output(reserve_in, reserve_out, amount, state.total_fee)


def cantex_net_output(quote: Quote, side: Side, token_per_cc: Decimal) -> Decimal:
    """Cantex output with its network fee (charged in CC) taken off.

    The fee is converted at the mid when the output is not CC, so both venues
    are compared on what the trader actually ends up holding.
    """
    fee_cc = quote.network_fee or Decimal(0)
    if side.buy == CC:
        return quote.returned_amount - fee_cc
    return quote.returned_amount - fee_cc * token_per_cc


def edge_bps(a: Decimal, b: Decimal) -> Decimal:
    """How much more ``a`` returns than ``b``, in basis points of ``b``."""
    if b <= 0:
        return Decimal(0)
    return (a / b - 1) * Decimal(10_000)


def route_order(cc_in: Decimal, token_out: Decimal, cc_back: Decimal) -> dict:
    """A round trip's result, rounded for display."""
    return {
        "cc_in": float(round(cc_in, 4)),
        "token_mid": float(round(token_out, 6)),
        "cc_back": float(round(cc_back, 4)),
        "net_cc": float(round(cc_back - cc_in, 4)),
        "net_bps": float(round(edge_bps(cc_back, cc_in), 2)),
    }


# === state on disk =========================================================


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """Atomic write, so the page never reads half a file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


# === one tick ==============================================================


class Board:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.tradecraft = TradecraftAdapter()
        self.paper = load_json(out / "paper.json", {"fills": [], "tick": 0})
        self.history = load_json(out / "history.json", {"t": [], "pairs": {}})
        self.scan = load_json(out / "scan.json", {"routes": [], "checked": 0})

    async def start(self) -> None:
        await self.tradecraft.connect()

    async def stop(self) -> None:
        await self.tradecraft.close()

    async def _match(self, cantex: CantexAdapter) -> list[PairBook]:
        states = {s.amm_id: s for s in await self.tradecraft.pool_states()}
        tc_pools = await self.tradecraft.pools()
        tc_by_key = {frozenset((p.token_a, p.token_b)): states[p.contract_id] for p in tc_pools}
        tc_instrument = {}
        for p in tc_pools:
            st = states[p.contract_id]
            tc_instrument[p.token_a] = st.token_a
            tc_instrument[p.token_b] = st.token_b

        books = []
        for pool in await cantex.pools():
            state = tc_by_key.get(frozenset((pool.token_a, pool.token_b)))
            if state is None or CC not in (state.token_a, state.token_b):
                continue
            cc_inst, tok_inst = (
                (pool.token_a, pool.token_b)
                if tc_instrument.get(pool.token_a) == CC
                else (pool.token_b, pool.token_a)
            )
            token = state.token_b if state.token_a == CC else state.token_a
            if state.reserve_a <= 0 or state.reserve_b <= 0:
                continue
            books.append(PairBook(token, state, cc_inst, tok_inst))
        return sorted(books, key=lambda b: (b.token != USD_STABLE, b.token))

    async def _price(self, cantex: CantexAdapter, books: list[PairBook], cc_usd: Decimal) -> None:
        gate = asyncio.Semaphore(CANTEX_CONCURRENCY)

        async def one(book: PairBook, side: Side, size: Decimal) -> None:
            mid = book.tc_mid
            amount = sell_amount(side, size, cc_usd, mid)
            book.tc_out[(side, size)] = tc_output(book.tc_state, side, amount)
            sell_i, buy_i = (
                (book.cantex_sell_cc, book.cantex_token)
                if side.sell == CC
                else (book.cantex_token, book.cantex_sell_cc)
            )
            async with gate:
                try:
                    q = await cantex.quote(amount, sell_i, buy_i)
                except VenueError as exc:
                    log.warning("cantex quote %s->%s $%s: %s", side.sell, side.buy, size, exc)
                    return
            book.cx_out[(side, size)] = cantex_net_output(q, side, mid)
            if side.sell == CC and size == SIZES_USD[0] and q.pool_price_before:
                book.cx_mid = q.pool_price_before

        jobs = []
        for book in books:
            for side in (Side(CC, book.token), Side(book.token, CC)):
                for size in SIZES_USD:
                    jobs.append(one(book, side, size))
        await asyncio.gather(*jobs)

    async def _scan(self, cantex: CantexAdapter, books: list[PairBook], cc_usd: Decimal, now: int):
        """Buy the token on one venue, sell it on the other, see what CC comes back."""
        found = []
        for book in books:
            out, back = Side(CC, book.token), Side(book.token, CC)
            for size in SIZES_USD:
                cc_in = size / cc_usd
                # Tradecraft first leg, Cantex back: the token amount is known
                # exactly, so the Cantex leg is quoted at that amount.
                tok = book.tc_out.get((out, size))
                est = book.cx_out.get((back, size))
                if tok is not None and est is not None:
                    self.scan["checked"] += 1
                    rough = est * tok / sell_amount(back, size, cc_usd, book.tc_mid)
                    if rough > cc_in * Decimal("0.999"):
                        try:
                            q = await cantex.quote(tok, book.cantex_token, book.cantex_sell_cc)
                            cc_back = cantex_net_output(q, back, book.tc_mid)
                            if (cc_back - cc_in) * cc_usd >= MIN_ROUTE_USD:
                                found.append({"t": now, "pair": f"{CC}/{book.token}",
                                              "size_usd": int(size), "buy_on": "tradecraft",
                                              "sell_on": "cantex",
                                              **route_order(cc_in, tok, cc_back)})
                        except VenueError as exc:
                            log.warning("scan quote: %s", exc)
                # Cantex first leg, Tradecraft back: priced locally, no call.
                tok = book.cx_out.get((out, size))
                if tok is not None and tok > 0:
                    self.scan["checked"] += 1
                    cc_back = tc_output(book.tc_state, back, tok)
                    if (cc_back - cc_in) * cc_usd >= MIN_ROUTE_USD:
                        found.append({"t": now, "pair": f"{CC}/{book.token}",
                                      "size_usd": int(size), "buy_on": "cantex",
                                      "sell_on": "tradecraft",
                                      **route_order(cc_in, tok, cc_back)})
        self.scan["routes"] = (self.scan["routes"] + found)[-SCAN_KEEP:]
        self.scan["last_t"] = now
        return found

    def _route(self, books: list[PairBook], cc_usd: Decimal, now: int) -> dict | None:
        """The paper order: best venue for $1,000 of CC/USDCx this tick."""
        book = next((b for b in books if b.token == USD_STABLE), None)
        if book is None:
            return None
        tick = self.paper["tick"]
        side = Side(CC, USD_STABLE) if tick % 2 == 0 else Side(USD_STABLE, CC)
        tc = book.tc_out.get((side, ROUTER_SIZE_USD))
        cx = book.cx_out.get((side, ROUTER_SIZE_USD))
        if tc is None or cx is None:
            return None
        best, other = ("cantex", "tradecraft") if cx >= tc else ("tradecraft", "cantex")
        got, alt = max(cx, tc), min(cx, tc)
        extra_usd = (got - alt) * (Decimal(1) if side.buy == USD_STABLE else cc_usd)
        fill = {
            "t": now,
            "side": "sell CC" if side.sell == CC else "buy CC",
            "sold": float(round(sell_amount(side, ROUTER_SIZE_USD, cc_usd, book.tc_mid), 4)),
            "got": float(round(got, 4)),
            "unit": side.buy,
            "venue": best,
            "other": other,
            "other_got": float(round(alt, 4)),
            "edge_bps": float(round(edge_bps(got, alt), 2)),
            "extra_usd": float(round(extra_usd, 4)),
        }
        self.paper["tick"] = tick + 1
        self.paper["fills"] = (self.paper["fills"] + [fill])[-PAPER_KEEP:]
        totals = self.paper.setdefault(
            "totals", {"fills": 0, "notional_usd": 0.0, "extra_usd": 0.0, "by_venue": {}}
        )
        totals["fills"] += 1
        totals["notional_usd"] += float(ROUTER_SIZE_USD)
        totals["extra_usd"] = round(totals["extra_usd"] + fill["extra_usd"], 4)
        totals["by_venue"][best] = totals["by_venue"].get(best, 0) + 1
        totals.setdefault("since", now)
        return fill

    def _record_history(self, books: list[PairBook], now: int) -> None:
        h = self.history
        h["t"].append(now)
        names = [b.token for b in books]
        for book in books:
            series = h["pairs"].setdefault(book.token, {"tc": [None] * (len(h["t"]) - 1),
                                                        "cx": [None] * (len(h["t"]) - 1)})
            series["tc"].append(float(book.tc_mid))
            series["cx"].append(float(book.cx_mid) if book.cx_mid else None)
        for token, series in h["pairs"].items():
            if token not in names:
                series["tc"].append(None)
                series["cx"].append(None)
        cut = 0
        while cut < len(h["t"]) and h["t"][cut] < now - HISTORY_SECONDS:
            cut += 1
        h["t"] = h["t"][cut:]
        for series in h["pairs"].values():
            series["tc"] = series["tc"][cut:]
            series["cx"] = series["cx"][cut:]

    def _snapshot(self, books: list[PairBook], cc_usd: Decimal, now: int, took: float) -> dict:
        pairs = []
        for book in books:
            rows = []
            for side in (Side(CC, book.token), Side(book.token, CC)):
                for size in SIZES_USD:
                    tc = book.tc_out.get((side, size))
                    cx = book.cx_out.get((side, size))
                    rows.append({
                        "side": "sell CC" if side.sell == CC else "buy CC",
                        "size_usd": int(size),
                        "sold": float(round(sell_amount(side, size, cc_usd, book.tc_mid), 6)),
                        "unit": side.buy,
                        "tradecraft": float(tc) if tc is not None else None,
                        "cantex": float(cx) if cx is not None else None,
                        "cantex_edge_bps": (float(round(edge_bps(cx, tc), 2))
                                            if tc is not None and cx is not None else None),
                    })
            cc_side, tok_side = book.tc_state.reserves_for(CC, book.token)
            pairs.append({
                "pair": f"{CC}/{book.token}",
                "token": book.token,
                "mid_tradecraft": float(book.tc_mid),
                "mid_cantex": float(book.cx_mid) if book.cx_mid else None,
                "mid_gap_bps": (float(round(edge_bps(book.cx_mid, book.tc_mid), 2))
                                if book.cx_mid else None),
                "tradecraft_reserves": [float(cc_side), float(tok_side)],
                "tradecraft_fee": float(book.tc_state.realized_fee),
                "rows": rows,
            })
        return {
            "t": now,
            "took_s": round(took, 1),
            "cc_usd": float(cc_usd),
            "sizes_usd": [int(s) for s in SIZES_USD],
            "pairs": pairs,
        }

    async def tick(self) -> None:
        started = time.monotonic()
        now = int(time.time())
        # A fresh Cantex session per tick: its login is a cheap challenge-
        # response, and a long-lived session would need its own expiry logic.
        async with CantexAdapter() as cantex:
            books = await self._match(cantex)
            stable = next((b for b in books if b.token == USD_STABLE), None)
            if stable is None:
                raise VenueError("no CC/USDCx pool on both venues; cannot value sizes in USD")
            cc_usd = stable.tc_mid  # USDCx per CC, Tradecraft reserves
            await self._price(cantex, books, cc_usd)
            found = await self._scan(cantex, books, cc_usd, now)
        fill = self._route(books, cc_usd, now)
        self._record_history(books, now)
        snap = self._snapshot(books, cc_usd, now, time.monotonic() - started)
        snap["status"] = "ok"
        write_json(self.out / "board.json", snap)
        write_json(self.out / "paper.json", self.paper)
        write_json(self.out / "history.json", self.history)
        write_json(self.out / "scan.json", self.scan)
        log.info("tick: %d pairs, %d routes, router %s, %.1fs", len(books), len(found),
                 fill and fill["venue"], snap["took_s"])


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.out.mkdir(parents=True, exist_ok=True)

    board = Board(args.out)
    await board.start()
    try:
        while True:
            began = time.monotonic()
            try:
                await board.tick()
            except Exception as exc:  # one bad tick must not stop the board
                log.exception("tick failed")
                status = load_json(args.out / "board.json", {})
                status.update(status="error", error=str(exc)[:300], error_t=int(time.time()))
                write_json(args.out / "board.json", status)
            if args.once:
                break
            await asyncio.sleep(max(5, args.interval - (time.monotonic() - began)))
    finally:
        await board.stop()


if __name__ == "__main__":
    asyncio.run(main())
