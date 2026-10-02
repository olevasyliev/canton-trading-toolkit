"""Pure computations behind Canton Venues: no I/O, everything testable offline.

Every venue pool is reduced to one shape, ``VenuePool``: a CC-paired AMM that
can price a swap in either direction. Prices, premiums, the execution ladder,
the spread scan and the paper desk are all derived from those.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

CC = "CC"
SIZES_USD = (100, 1_000, 10_000, 50_000)
ROUTER_SIZE_USD = 1_000
# Round-trip network cost deducted before a spread counts. An assumption, not a
# measurement: Cantex charged 0.82-1.24 CC per swap when measured on
# 2026-10-02, Tradecraft does not publish its cost. 3 CC covers two swaps.
ROUND_TRIP_COST_CC = Decimal(3)
MIN_ROUTE_USD = Decimal("0.50")

# Assets with an independent market outside Canton, and what they track.
# CoinGecko ids. Stables are measured against the 1.00 peg instead.
REFERENCES = {
    "CC": ("canton-network", "global CC markets"),
    "CETH": ("ethereum", "ETH"),
    "CBTC": ("bitcoin", "BTC"),
    "SPYE": ("sp500-xstock", "SPYx"),
    "QQQE": ("nasdaq-xstock", "QQQx"),
    "EXAU": ("pax-gold", "gold, via PAXG"),
    "EXAG": ("kinesis-silver", "silver, via KAG"),
}
STABLES = {"USDCX", "USDC.B", "FRXUSD.B", "USX", "TF-USDT", "USDXLR", "USDM1"}
# A reference this far off is a unit mismatch (an ounce against a gram, say),
# not a premium; it is reported as unmatched rather than shown as a number.
MAX_PLAUSIBLE_PREMIUM = Decimal("0.25")


def key(symbol: str) -> str:
    return symbol.upper()


@dataclass(frozen=True)
class VenuePool:
    """A CC/token AMM pool on one venue."""

    venue: str
    token: str  # display symbol of the non-CC side
    cc_reserve: Decimal
    token_reserve: Decimal
    fee: Decimal  # what a swap pays, as a fraction (for display and APR)
    lp_share: Decimal  # part of the fee that goes to liquidity providers
    _out: Callable[[bool, Decimal], Decimal] = field(repr=False, compare=False)
    # how ``_out`` prices, so a pool can be saved and rebuilt (see pool_to_json)
    formula: str = ""        # "cantex": fee on input; "tradecraft": half the fee on each leg
    fee_param: Decimal = Decimal(0)

    @property
    def mid(self) -> Decimal:
        """Token per CC, before fees."""
        return self.token_reserve / self.cc_reserve

    def out(self, sell_cc: bool, amount: Decimal) -> Decimal:
        return self._out(sell_cc, amount)


def blended_mid(pools: list[VenuePool]) -> Decimal:
    """Token per CC across venues, weighted by each pool's CC depth."""
    depth = sum(p.cc_reserve for p in pools)
    return sum(p.mid * p.cc_reserve for p in pools) / depth


def cc_usd(stable_pools: list[VenuePool], usdcx_usd: Decimal) -> Decimal:
    """CC in dollars: the blended CC/USDCx mid times USDCx's own dollar price."""
    return blended_mid(stable_pools) * usdcx_usd


def token_usd(pools: list[VenuePool], cc_in_usd: Decimal) -> Decimal:
    return cc_in_usd / blended_mid(pools)


def premium(canton_usd: Decimal, reference_usd: Decimal) -> Decimal | None:
    """Canton price over the outside price, as a fraction. None if implausible."""
    if reference_usd <= 0:
        return None
    p = canton_usd / reference_usd - 1
    return p if abs(p) <= MAX_PLAUSIBLE_PREMIUM else None


def edge_bps(a: Decimal, b: Decimal) -> float:
    return float((a / b - 1) * 10_000) if b > 0 else 0.0


# === execution ladder ======================================================


def ladder(pools: dict[str, VenuePool], cc_in_usd: Decimal, mid: Decimal) -> list[dict]:
    """What each venue returns at each size, both directions.

    ``mid`` (token per CC) only converts dollar sizes into token amounts, so
    every venue is asked about exactly the same trade.
    """
    rows = []
    for side in ("sell", "buy"):
        for size in SIZES_USD:
            cc_amount = Decimal(size) / cc_in_usd
            amount = cc_amount if side == "sell" else cc_amount * mid
            outs = {v: p.out(side == "sell", amount) for v, p in pools.items()}
            best = max(outs, key=outs.get)
            rows.append({
                "side": side,  # sell = sell CC for the token, buy = buy CC with it
                "size_usd": size,
                "amount_in": float(amount),
                "out": {v: float(o) for v, o in outs.items()},
                "best": best,
                "edge_bps": edge_bps(max(outs.values()), min(outs.values())) if len(outs) > 1 else 0.0,
            })
    return rows


def crossover_usd(rows: list[dict], side: str, a: str, b: str) -> list[float]:
    """Dollar sizes where the better of venues a and b flips (log-interpolated)."""
    pts = [r for r in rows if r["side"] == side and a in r["out"] and b in r["out"]]
    out = []
    for r0, r1 in zip(pts, pts[1:]):
        e0 = edge_bps(Decimal(str(r0["out"][a])), Decimal(str(r0["out"][b])))
        e1 = edge_bps(Decimal(str(r1["out"][a])), Decimal(str(r1["out"][b])))
        if e0 == 0 or e1 == 0 or (e0 > 0) == (e1 > 0):
            continue
        l0, l1 = math.log(r0["size_usd"]), math.log(r1["size_usd"])
        out.append(math.exp(l0 + (l1 - l0) * e0 / (e0 - e1)))
    return out


# === spread scan ===========================================================


def round_trip(buy_on: VenuePool, sell_on: VenuePool, cc_in: Decimal) -> Decimal:
    """CC back after buying the token on one venue and selling it on the other."""
    return sell_on.out(False, buy_on.out(True, cc_in))


def best_round_trip(buy_on: VenuePool, sell_on: VenuePool, cc_in_usd: Decimal,
                    lo_usd: float = 10, hi_usd: float = 100_000, steps: int = 60) -> tuple[Decimal, Decimal]:
    """The input (CC) that maximises the round trip's gross gain, and that gain.

    The gain is concave in the input for two constant-product pools, so a log
    grid followed by a golden-section refine finds the maximum.
    """
    def gain(x: Decimal) -> Decimal:
        return round_trip(buy_on, sell_on, x) - x

    grid = [Decimal(lo_usd * (hi_usd / lo_usd) ** (i / (steps - 1))) / cc_in_usd for i in range(steps)]
    gains = [gain(x) for x in grid]
    i = max(range(steps), key=lambda k: gains[k])
    a, b = grid[max(i - 1, 0)], grid[min(i + 1, steps - 1)]
    phi = Decimal((math.sqrt(5) - 1) / 2)
    for _ in range(40):
        c, d = b - phi * (b - a), a + phi * (b - a)
        if gain(c) > gain(d):
            b = d
        else:
            a = c
    x = (a + b) / 2
    return x, gain(x)


def scan(books: dict[str, dict[str, VenuePool]], cc_in_usd: Decimal) -> list[dict]:
    """Every token on two or more venues, both directions, at its best size."""
    found = []
    for token, pools in books.items():
        venues = sorted(pools)
        for buy in venues:
            for sell in venues:
                if buy == sell:
                    continue
                x, g = best_round_trip(pools[buy], pools[sell], cc_in_usd)
                net_cc = g - ROUND_TRIP_COST_CC
                found.append({
                    "token": token,
                    "buy_on": buy,
                    "sell_on": sell,
                    "size_cc": float(x),
                    "size_usd": float(x * cc_in_usd),
                    "gross_cc": float(g),
                    "net_cc": float(net_cc),
                    "net_usd": float(net_cc * cc_in_usd),
                    "clears": net_cc * cc_in_usd >= MIN_ROUTE_USD,
                })
    return sorted(found, key=lambda r: -r["net_usd"])


# === paper desk ============================================================


def route_order(pools: dict[str, VenuePool], side: str, size_usd: int,
                cc_in_usd: Decimal, mid: Decimal) -> dict | None:
    """One paper order, filled on whichever venue returns more."""
    if len(pools) < 2:
        return None
    cc_amount = Decimal(size_usd) / cc_in_usd
    amount = cc_amount if side == "sell" else cc_amount * mid
    outs = {v: p.out(side == "sell", amount) for v, p in pools.items()}
    best = max(outs, key=outs.get)
    worst = min(outs, key=outs.get)
    extra = outs[best] - outs[worst]
    extra_usd = extra * (cc_in_usd / mid if side == "sell" else cc_in_usd)
    return {
        "side": side,
        "amount_in": float(amount),
        "venue": best,
        "got": float(outs[best]),
        "other": worst,
        "other_got": float(outs[worst]),
        "edge_bps": edge_bps(outs[best], outs[worst]),
        "extra_usd": float(extra_usd),
    }


def fingerprint(*pools: VenuePool) -> str:
    """Identifies pool state, so one standing spread is booked once, not every tick."""
    return "|".join(f"{p.venue}:{p.cc_reserve:.6f}:{p.token_reserve:.6f}" for p in pools)


# === LP board ==============================================================


def fee_apr(volume_usd_24h: float, fee: float, lp_share: float, tvl_usd: float) -> float | None:
    """Annualised fee income to LPs from the last 24 hours of volume."""
    if tvl_usd <= 0:
        return None
    return volume_usd_24h * fee * lp_share / tvl_usd * 365


# === history ===============================================================


def change(series: list[tuple[int, float]], now_ms: int, window_ms: int) -> float | None:
    """Relative change over ``window_ms`` from a (time_ms, value) series."""
    if not series:
        return None
    target = now_ms - window_ms
    past = [v for t, v in series if t <= target]
    if not past:
        return None
    # the median of the last three points before the window, so one wick in a thin pool (a
    # single trade that moved the price for an hour) is not taken as the starting price
    base = sorted(past[-3:])[len(past[-3:]) // 2]
    if base <= 0:
        return None
    return series[-1][1] / base - 1


# === saving and rebuilding pools ===========================================


def _cantex_out(cc: Decimal, tok: Decimal, fee: Decimal):
    from canton_toolkit.venues.cantex_public import swap_output

    def out(sell_cc: bool, amount: Decimal) -> Decimal:
        return swap_output(cc, tok, amount, fee) if sell_cc else swap_output(tok, cc, amount, fee)
    return out


def _tradecraft_out(cc: Decimal, tok: Decimal, fee: Decimal):
    from canton_toolkit.venues.tradecraft import swap_output

    def out(sell_cc: bool, amount: Decimal) -> Decimal:
        return swap_output(cc, tok, amount, fee) if sell_cc else swap_output(tok, cc, amount, fee)
    return out


FORMULAS = {"cantex": _cantex_out, "tradecraft": _tradecraft_out}


def pool_to_json(p: VenuePool) -> dict:
    return {"venue": p.venue, "token": p.token, "cc_reserve": str(p.cc_reserve),
            "token_reserve": str(p.token_reserve), "formula": p.formula,
            "fee_param": str(p.fee_param), "fee": str(p.fee), "lp_share": str(p.lp_share)}


def pool_from_json(d: dict) -> VenuePool:
    cc, tok, fee = Decimal(d["cc_reserve"]), Decimal(d["token_reserve"]), Decimal(d["fee_param"])
    return VenuePool(d["venue"], d["token"], cc, tok, Decimal(d["fee"]), Decimal(d["lp_share"]),
                     FORMULAS[d["formula"]](cc, tok, fee), d["formula"], fee)


# === routing any amount =====================================================


def best_leg(pools: dict[str, VenuePool], sell_cc: bool, amount: Decimal) -> tuple[str, Decimal, dict]:
    """The venue that returns most for one CC-paired swap, and every venue's output."""
    outs = {v: p.out(sell_cc, amount) for v, p in pools.items()}
    best = max(outs, key=outs.get)
    return best, outs[best], outs


def route(books: dict[str, dict[str, VenuePool]], sell: str, buy: str, amount: Decimal) -> dict:
    """Best route to sell ``amount`` of ``sell`` for ``buy``.

    Every pool is paired with CC, so a token-to-token trade goes through CC in
    two legs, and each leg is sent to its own best venue: the second leg's
    price depends only on the CC amount, not on where the first leg filled.
    """
    sell, buy = key(sell), key(buy)
    if sell == buy:
        raise ValueError("sell and buy are the same token")
    if amount <= 0:
        raise ValueError("amount must be positive")
    for t in (sell, buy):
        if t != CC and t not in books:
            raise ValueError(f"no Canton DEX pool for {t}")
    legs = []
    if sell == CC:
        v, got, outs = best_leg(books[buy], True, amount)
        legs.append({"sell": CC, "buy": buy, "amount_in": amount, "venue": v, "out": got, "all": outs})
    elif buy == CC:
        v, got, outs = best_leg(books[sell], False, amount)
        legs.append({"sell": sell, "buy": CC, "amount_in": amount, "venue": v, "out": got, "all": outs})
    else:
        v1, cc_mid, outs1 = best_leg(books[sell], False, amount)
        legs.append({"sell": sell, "buy": CC, "amount_in": amount, "venue": v1, "out": cc_mid, "all": outs1})
        v2, got, outs2 = best_leg(books[buy], True, cc_mid)
        legs.append({"sell": CC, "buy": buy, "amount_in": cc_mid, "venue": v2, "out": got, "all": outs2})
    return {"sell": sell, "buy": buy, "amount_in": amount, "amount_out": legs[-1]["out"], "legs": legs}
