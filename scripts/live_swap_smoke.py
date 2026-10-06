"""Write-path live smoke test against the real Cantex API.

Unlike ``live_smoke.py`` (read-only), this script can execute a REAL swap with
REAL funds on Cantex mainnet. It is a dry run by default: it authenticates,
reports balances, and prices the swap, but submits nothing. Pass ``--execute``
to actually submit.

Purpose: close the last unvalidated claim in the venue adapter, that it can
execute and not only read, and record the fee figures Cantex does not document.

Usage (from the toolkit root, with .env present):

    python scripts/live_swap_smoke.py                 # dry run, submits nothing
    python scripts/live_swap_smoke.py --execute       # submits a real swap
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

from cantonvenues import VenueAuthError, VenueError
from cantonvenues.venues.cantex import BASE_URL_ENV, DEFAULT_BASE_URL, CantexAdapter

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = TOOLKIT_ROOT / ".env"

# Instrument ids, not display symbols. Canton Coin is the one token where the
# two differ: it prices and pools as "Amulet" but reports as symbol "CC".
SELL_SYMBOL = "Amulet"
BUY_SYMBOL = "USDCx"
SELL_AMOUNT = Decimal("10")
# Denominated in CC. Measured 2026-07-17 from the quote endpoint: the network
# fee is a flat 0.95 CC on any size below 500 CC, and exactly 0 from 500 CC up.
# So sub-500 CC trades are structurally lossy, and Cantex's own SDK example cap
# of 0.5 is below the going rate. 1.0 leaves headroom over 0.95 without letting
# a runaway fee through.
MAX_NETWORK_FEE = Decimal("1.0")
# Refuse to submit if the venue prices the trade more than this far from the
# quote we just read back, i.e. the pool moved under us.
MAX_SLIPPAGE_PCT = Decimal("5")


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


async def _run(execute: bool) -> int:
    base_url = os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL
    print(f"base url: {base_url}")
    print(f"mode: {'EXECUTE (real funds)' if execute else 'dry run (submits nothing)'}")

    try:
        async with CantexAdapter() as venue:
            print("auth OK")

            balances = {b.instrument.id: b for b in await venue.balances()}
            for symbol in (SELL_SYMBOL, BUY_SYMBOL):
                bal = balances.get(symbol)
                if bal is not None:
                    print(f"balance {symbol}: unlocked={bal.unlocked} locked={bal.locked}")
                else:
                    print(f"balance {symbol}: absent")

            sell_balance = balances.get(SELL_SYMBOL)
            if sell_balance is None:
                print(f"ABORT: no {SELL_SYMBOL} balance on the account")
                return 1
            if sell_balance.unlocked < SELL_AMOUNT:
                print(
                    f"ABORT: unlocked {sell_balance.unlocked} {SELL_SYMBOL} "
                    f"is below the {SELL_AMOUNT} needed"
                )
                return 1

            pools = await venue.pools()
            pool = next(
                (
                    p
                    for p in pools
                    if p.token_a.id == SELL_SYMBOL and p.token_b.id == BUY_SYMBOL
                ),
                None,
            )
            if pool is None:
                print(f"ABORT: no {SELL_SYMBOL}/{BUY_SYMBOL} pool among {len(pools)} pools")
                return 1

            quote = await venue.quote(SELL_AMOUNT, pool.token_a, pool.token_b)
            print(
                f"quote: sell {SELL_AMOUNT} {SELL_SYMBOL} -> "
                f"{quote.returned_amount} {BUY_SYMBOL} "
                f"(price {quote.trade_price}, slippage {quote.slippage}, "
                # fee_percentage is a fraction, not a percent: 0.0005 = 0.05%.
                f"fee {quote.fee_percentage * 100}%)"
            )

            if quote.slippage is not None and abs(quote.slippage) > MAX_SLIPPAGE_PCT:
                print(f"ABORT: slippage {quote.slippage} exceeds {MAX_SLIPPAGE_PCT}")
                return 1

            if not execute:
                print("dry run complete, nothing submitted. Pass --execute to submit.")
                return 0

            print(f"submitting swap, network fee capped at {MAX_NETWORK_FEE} ...")
            result = await venue.swap(
                SELL_AMOUNT,
                pool.token_a,
                pool.token_b,
                max_network_fee=MAX_NETWORK_FEE,
            )
            print("SWAP CONFIRMED ON LEDGER")
            print(f"  sold:           {result.input_amount} {result.input_instrument.id}")
            print(f"  received:       {result.output_amount} {result.output_instrument.id}")
            print(f"  price:          {result.price}")
            print(f"  admin fee:      {result.admin_fee_amount}")
            print(f"  liquidity fee:  {result.liquidity_fee_amount}")
            print(f"  market:         {result.market}")

            after = {b.instrument.id: b for b in await venue.balances()}
            for symbol in (SELL_SYMBOL, BUY_SYMBOL):
                bal = after.get(symbol)
                if bal is not None:
                    print(f"balance after {symbol}: unlocked={bal.unlocked}")

    except VenueAuthError as exc:
        print(f"AUTH FAILED: {exc}")
        return 1
    except VenueError as exc:
        print(f"REQUEST FAILED: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="submit a real swap with real funds (default: dry run)",
    )
    args = parser.parse_args()
    _load_env_file(ENV_FILE)
    sys.exit(asyncio.run(_run(args.execute)))
