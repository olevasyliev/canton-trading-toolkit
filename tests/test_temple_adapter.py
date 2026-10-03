"""Offline tests for the Temple adapter.

The settled-volume fixture is a live MainNet response from 2026-10-03. The
order-book fixture follows the ``OrderBook`` type in Temple's SDK 2.1.10
(``dist/api/types.d.ts``); it is not a captured response, because market data
needs a key we do not hold yet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from canton_toolkit import TempleAdapter, VenueAuthError
from canton_toolkit.venues.temple import wire_symbol

SETTLED = {
    "start_time": "2026-10-02T14:14:11Z", "end_time": "2026-10-03T14:14:11Z",
    "total_volume_usd": 17897502.339623783, "by_quote": {"USDA": 0, "USDCx": 17897502.339623783},
    "trade_count": 2425033,
    "markets": [
        {"symbol": "CBTC/USDCx", "quote": "USDCx", "quote_volume": 9111926.183983302, "trade_count": 1119821},
        {"symbol": "CC/USDCx", "quote": "USDCx", "quote_volume": 505742.69472588145, "trade_count": 40320},
    ],
}
BOOK = {"symbol": "Amulet/USDCx", "bids": [{"price": "0.1210", "quantity": "5000"}],
        "asks": [{"price": "0.1212", "quantity": "4000"}], "best_bid": "0.1210", "best_ask": "0.1212",
        "spread": "0.0002", "timestamp": "2026-10-03T14:00:00Z"}


def _adapter(seen: list, key: str | None = None) -> TempleAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/exchange/settled_volume":
            return httpx.Response(200, json=SETTLED)
        if request.url.path == "/api/v1/market/orderbook":
            if request.headers.get("X-API-Key") != "k":
                return httpx.Response(401, json={"code": "API_KEY_AUTH_FAILED"})
            return httpx.Response(200, json=BOOK)
        return httpx.Response(404, text="not found")
    return TempleAdapter(api_key=key or "", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_settled_volume_needs_no_key_and_sends_rfc3339() -> None:
    seen: list = []
    async with _adapter(seen) as temple:
        vol = await temple.settled_volume(24, end=datetime(2026, 10, 3, 14, 14, 11, tzinfo=UTC))
    assert vol["markets"][0]["symbol"] == "CBTC/USDCx"
    req = seen[-1]
    assert req.url.params["start_time"] == "2026-10-02T14:14:11Z"
    assert "X-API-Key" not in req.headers


async def test_market_data_without_a_key_refuses_before_calling() -> None:
    seen: list = []
    async with _adapter(seen) as temple:
        calls = len(seen)
        with pytest.raises(VenueAuthError):
            await temple.order_book("CC/USDCx")
    assert len(seen) == calls  # nothing was sent


async def test_order_book_with_a_key_uses_the_header_and_amulet_symbol() -> None:
    seen: list = []
    async with _adapter(seen, key="k") as temple:
        book = await temple.order_book("CC/USDCx", 20)
    assert seen[-1].url.params["symbol"] == "Amulet/USDCx"
    assert seen[-1].url.params["levels"] == "20"
    assert book.best_bid.price == Decimal("0.1210") and book.best_ask.size == Decimal(4000)


async def test_markets_come_from_settled_volume() -> None:
    async with _adapter([]) as temple:
        names = [(mk.base, mk.quote) for mk in await temple.markets()]
    assert names == [("CBTC", "USDCx"), ("CC", "USDCx")]


def test_wire_symbol_rewrites_only_the_cc_token() -> None:
    assert wire_symbol("CC/USDCx") == "Amulet/USDCx"
    assert wire_symbol("CBTC/USDCx") == "CBTC/USDCx"
