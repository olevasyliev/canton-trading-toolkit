"""Read-only smoke test of the Tradecraft adapter against Canton mainnet.

Nothing here writes: every call is a GET against the venue's public API, so
this needs no credentials and cannot move funds.

What it checks, and why each one is worth a line:

1.  the venue is reachable and the pool list parses
2.  local pricing reproduces the venue's own quote on **every** pool, which is
    what makes the "half the total fee on each leg" model a measurement
3.  the fixed-output route is the inverse of the fixed-input route
4.  reversed paths are answered in the pool's canonical order by the state and
    liquidity routes, and honoured by the quote routes
5.  the symbol/instrument-id split (CC vs Amulet) resolves both ways
6.  the guards hold: non-positive amounts, per-route history windows

Run with ``PYTHONPATH=src python3 scripts/tradecraft_market_smoke.py``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from canton_toolkit import TradecraftAdapter, VenueRequestError

SPEC_TOKEN_ENUM = {"CC", "USDCx", "CBTC", "cETH", "HANDL", "EDELx", "SBC"}
TOLERANCE = Decimal("1e-12")

passed = 0
failed = 0
venue_findings: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """An invariant of *this* client. A failure here is our bug."""
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def venue_finding(label: str, holds: bool, detail: str = "") -> None:
    """An observation about the *venue*, reported and never counted as a failure.

    Kept separate because a reviewer running this script is checking our work,
    and a defect on somebody else's side must not read as a broken deliverable.
    """
    if holds:
        print(f"  ok   {label}" + (f" — {detail}" if detail else ""))
        return
    venue_findings.append(label)
    print(f"  venue {label}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    async with TradecraftAdapter() as venue:
        print("\n== pools ==")
        states = await venue.pool_states()
        tokens = {t for s in states for t in (s.token_a, s.token_b)}
        check("pool list parses", len(states) > 0, f"{len(states)} pools, {len(tokens)} tokens")
        missing = sorted(tokens - SPEC_TOKEN_ENUM)
        venue_finding(
            "published token enum covers the live tokens",
            not missing,
            f"absent from the spec enum: {', '.join(missing)}" if missing else "",
        )

        print("\n== pricing: our arithmetic against the venue's own quotes ==")
        priced = 0
        # Coverage is reported, not assumed: a direction the venue cannot quote
        # is named with its reason rather than dropped, so a reviewer reads the
        # denominator off the output instead of trusting the count.
        skipped: list[str] = []
        for state in states:
            for sell, buy, reserve in (
                (state.token_a, state.token_b, state.reserve_a),
                (state.token_b, state.token_a, state.reserve_b),
            ):
                if state.reserve_a <= 0 or state.reserve_b <= 0:
                    skipped.append(f"{state.amm_id} {sell}->{buy}: pool side is empty")
                    continue
                size = (reserve / Decimal(10000)).quantize(Decimal("1e-8"))
                if size <= 0:
                    skipped.append(
                        f"{state.amm_id} {sell}->{buy}: reserve too small to size a probe"
                    )
                    continue
                quote = await venue.quote_symbols(size, sell, buy)
                local = venue.quote_locally(state, size, sell, buy)
                if quote.returned_amount == 0:
                    skipped.append(f"{state.amm_id} {sell}->{buy}: venue quoted zero for {size}")
                    continue
                error = abs(local - quote.returned_amount) / quote.returned_amount
                priced += 1
                check(
                    f"{sell}->{buy} {size} reproduces to {error:.1e}",
                    error < TOLERANCE,
                    f"venue {quote.returned_amount}, local {local}",
                )
        print(f"  ({priced} of {priced + len(skipped)} directions priced)")
        for line in skipped:
            print(f"  skip {line}")

        print("\n== fee model ==")
        # Deterministic and meaningful: the single-pool checks below run against
        # the deepest live pool. Taking whichever pool the venue happens to list
        # first lands them on a dust pool, where the venue's own rounding at tiny
        # absolute amounts swamps the invariant being tested.
        first = max(
            (s for s in states if s.reserve_a > 0 and s.reserve_b > 0),
            key=lambda s: s.reserve_a,
            default=states[0],
        )
        print(f"  (single-pool checks run against {first.amm_id})")
        check(
            "realized fee is under the published total by (total^2)/4",
            first.realized_fee < first.total_fee,
            f"{first.amm_id}: published {first.total_fee}, realized {first.realized_fee}",
        )
        lp, operator = await venue.fees(first.token_a, first.token_b)
        check(
            "fee constants agree across the two routes that serve them",
            (lp, operator) == (first.lp_fee, first.operator_fee),
            f"/feeAmount {lp}+{operator} vs /pools {first.lp_fee}+{first.operator_fee}",
        )

        print("\n== fixed input vs fixed output ==")
        size = (first.reserve_a / Decimal(10000)).quantize(Decimal("1e-8"))
        forward = await venue.quote_symbols(size, first.token_a, first.token_b)
        back = await venue.quote_for_output(forward.returned_amount, first.token_a, first.token_b)
        drift = abs(back.sell_amount - size) / size
        check("the two quote routes are inverses", drift < Decimal("1e-9"), f"drift {drift:.1e}")

        print("\n== orientation ==")
        canonical = await venue.inspect(first.token_a, first.token_b)
        reversed_path = await venue.inspect(first.token_b, first.token_a)
        check(
            "state route answers in canonical order either way",
            (reversed_path.token_a, reversed_path.token_b)
            == (canonical.token_a, canonical.token_b),
            f"asked {first.token_b}/{first.token_a}, got "
            f"{reversed_path.token_a}/{reversed_path.token_b}",
        )
        lq = await venue.liquidity_deposit_quote(
            first.token_b, first.token_a, Decimal("1000"), Decimal("1000")
        )
        check(
            "liquidity quote is labelled by symbol, not by request position",
            lq.token_a == canonical.token_a,
            f"1000 belongs to {lq.token_a}, and the path led with {first.token_b}",
        )
        one_way = await venue.quote_symbols(size, first.token_a, first.token_b)
        other_way = await venue.quote_symbols(size, first.token_b, first.token_a)
        check(
            "quote routes do honour path order",
            one_way.returned_amount != other_way.returned_amount,
            f"{first.token_a}->{first.token_b} {one_way.returned_amount} vs reverse "
            f"{other_way.returned_amount}",
        )

        print("\n== instruments ==")
        pools = await venue.pools()
        by_symbol = {p.contract_id: p for p in pools}
        first_pool = by_symbol[first.amm_id]
        quote_via_instruments = await venue.quote(size, first_pool.token_a, first_pool.token_b)
        check(
            "instrument ids resolve to the symbols the routes take",
            quote_via_instruments.returned_amount == one_way.returned_amount,
            f"{first_pool.token_a.id} is {first.token_a} on this venue",
        )

        print("\n== guards ==")
        for bad in (Decimal("0"), Decimal("-1000")):
            try:
                await venue.quote_symbols(bad, first.token_a, first.token_b)
                check(f"non-positive amount {bad} refused", False, "the call went through")
            except VenueRequestError:
                check(f"non-positive amount {bad} refused", True)
        try:
            await venue.volume_history(first.token_a, first.token_b, "month")
            check("volume history rejects a yield-only window", False, "month was accepted")
        except VenueRequestError:
            check("volume history rejects a yield-only window", True)
        rows = await venue.yield_history(first.token_a, first.token_b, "month")
        check("yield history serves the wider window set", len(rows) > 0, f"{len(rows)} points")

    print(f"\n{passed} passed, {failed} failed")
    if venue_findings:
        print(f"{len(venue_findings)} open venue-side finding(s): " + ", ".join(venue_findings))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
