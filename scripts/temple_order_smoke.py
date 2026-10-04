"""Order-path live smoke test against Temple: place, see, cancel, confirm gone.

A dry run by default: it reports whether the account can trade (linked wallet,
delegation, fee balance), its balances, open orders and the book, and prints the
order it would place, but sends nothing. Pass ``--execute`` to place it.

The order is built never to fill: post-only (the venue rejects it rather than let
it take liquidity) and priced half way to zero below the best bid for a buy, or
double the best ask for a sell. It is cancelled straight away and the script
checks it has left the open-orders list.

Usage, from the toolkit root:

    TEMPLE_API_KEY=... python scripts/temple_order_smoke.py --testnet              # dry run
    TEMPLE_API_KEY=... python scripts/temple_order_smoke.py --testnet --execute    # place + cancel
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from canton_toolkit import Side, TempleAdapter, VenueError
from canton_toolkit.venues.temple import MAINNET_BASE_URL, TESTNET_BASE_URL


async def run(args: argparse.Namespace) -> int:
    base = TESTNET_BASE_URL if args.testnet else MAINNET_BASE_URL
    print(f"base url: {base}  ({'EXECUTE' if args.execute else 'dry run'})")
    async with TempleAdapter(base_url=base, trading=args.execute) as temple:
        status = await temple.trading_status()
        print(f"trading status: {status}")
        print(f"balances: {[(b.symbol, str(b.unlocked), str(b.locked)) for b in await temple.balances()]}")
        print(f"open orders: {len(await temple.open_orders())}")
        book = await temple.order_book(args.symbol, 5)
        if not (book.best_bid and book.best_ask):
            print(f"book for {args.symbol} is one-sided; nothing to anchor a safe price to")
            return 1
        side = Side.BUY if args.side == "buy" else Side.SELL
        price = (book.best_bid.price / 2) if side is Side.BUY else (book.best_ask.price * 2)
        print(f"book {args.symbol}: bid {book.best_bid.price} / ask {book.best_ask.price}")
        print(f"order: post-only {side.value} {args.quantity} {args.symbol} @ {price}, expires in 10 min")
        if not args.execute:
            print("dry run: nothing sent")
            return 0
        if not status["ready"]:
            print("account is not ready to trade (see status above); not sending")
            return 1
        order = await temple.place_limit_order(args.symbol, side, Decimal(args.quantity), price, post_only=True,
                                               expires_at=datetime.now(UTC) + timedelta(minutes=10))
        print(f"placed: id={order.order_id} status={order.status}")
        print(f"venue record: {order.raw}")
        seen = [o for o in await temple.open_orders(args.symbol) if o.order_id == order.order_id]
        print(f"visible in open orders: {bool(seen)}")
        ok = await temple.cancel_order(order.order_id)
        print(f"cancel confirmed: {ok}")
        gone = not [o for o in await temple.open_orders(args.symbol) if o.order_id == order.order_id]
        print(f"gone from open orders: {gone}")
        return 0 if (ok and gone) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--testnet", action="store_true", help="use Temple testnet (default: mainnet)")
    ap.add_argument("--execute", action="store_true", help="place and cancel the order for real")
    ap.add_argument("--symbol", default="CC/USDCx")
    ap.add_argument("--side", choices=("buy", "sell"), default="buy")
    ap.add_argument("--quantity", default="100")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except VenueError as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
