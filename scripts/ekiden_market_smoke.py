#!/usr/bin/env python3
"""Read-only smoke test of the Ekiden adapter against the live Canton gateway.

    PYTHONPATH=src python3 scripts/ekiden_market_smoke.py
    EKIDEN_BASE_URL=https://api.canton.ekiden.fi PYTHONPATH=src python3 \
        scripts/ekiden_market_smoke.py --symbol BTC-USDTx

Public market data needs no credentials. Nothing here can place an order.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from cantonvenues import EkidenAdapter


def _pct(value: Decimal) -> str:
    return f"{value * 100:+.6f}%"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="CC-USDTx")
    parser.add_argument("--depth", type=int, default=10, choices=(10, 50, 200))
    args = parser.parse_args()

    async with EkidenAdapter() as venue:
        info = venue.info or {}
        print(f"gateway      : build {info.get('build_id')} · {info.get('runtime_manifest_phase')}")
        print(f"validator    : {info.get('aptos_network')}")

        markets = await venue.markets()
        print(f"\nmarkets      : {len(markets)}")
        for m in markets:
            state = "trading" if m.is_trading else "halted"
            print(
                f"  {m.symbol:12} {state:8} tick {m.tick_size}  min {m.min_order_size} {m.base}"
                f"  max {m.max_leverage}x  funding every {m.funding_interval_minutes}m"
            )

        for t in await venue.tickers(args.symbol):
            print(
                f"\nticker {t.symbol}"
                f"\n  last / index / mark : {t.last_price} / {t.index_price} / {t.mark_price}"
                f"\n  open interest       : {t.open_interest}"
                f"\n  24h volume          : {t.volume_24h} ({t.turnover_24h} quote)"
                f"\n  funding rate        : {_pct(t.funding_rate)} next {t.next_funding_time}"
            )

        book = await venue.order_book(args.symbol, depth=args.depth)
        print(f"\norder book {book.symbol} at {book.timestamp:%H:%M:%S} UTC")
        for level in reversed(book.asks[:5]):
            print(f"  ask {level.price:>16} x {level.size}")
        if book.mid_price is not None and book.spread is not None:
            basis = (
                f"{book.spread / book.mid_price * 100:.4f}%" if book.mid_price else "n/a"
            )
            print(f"  --- mid {book.mid_price} · spread {book.spread} ({basis}) ---")
        for level in book.bids[:5]:
            print(f"  bid {level.price:>16} x {level.size}")

        trades = await venue.recent_trades(args.symbol, limit=5)
        print(f"\nrecent trades: {len(trades)}")
        for t in trades:
            print(f"  {t.timestamp:%H:%M:%S} {t.side.value:4} {t.size:>16} @ {t.price}")

        funding = await venue.funding_history(args.symbol, limit=5)
        print(f"\nfunding history: {len(funding)}")
        for f in funding:
            print(f"  {f.timestamp:%Y-%m-%d %H:%M} {_pct(f.rate)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
