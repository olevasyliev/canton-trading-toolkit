"""Offline tests for the Temple adapter.

Fixtures are trimmed live MainNet responses: settled volume from 2026-10-03,
ticker, order book and trades from 2026-10-04 (with a key). The keyed shapes
differ from the SDK's TypeScript types, which is why they are pinned here.
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
BOOK = {"orderbook": {
    "symbol": "CC/USDCx", "timestamp": "2026-10-04T09:13:51.791592904Z", "sequence": 1791105230360438,
    "best_bid": 0.123, "best_ask": 0.12311, "spread": 0.00011,
    "bids": [{"price": 0.123, "quantity": 1021, "available_quantity": 1021, "cumulative_quantity": 1021,
              "order_count": 1, "first_created_at": "2026-10-04T09:13:47.808766Z"}],
    "asks": [{"price": 0.12311, "quantity": 4000, "available_quantity": 4000, "cumulative_quantity": 4000,
              "order_count": 2, "first_created_at": "2026-10-04T09:13:40.000000Z"}]}}
TICKER = {"count": 2, "tickers": [
    {"symbol": "CC/USDCx", "last_price": 0.12301, "volume_24h": 16216838.88, "quote_volume_24h_usd": 2004013.97,
     "trade_count_24h": 263004},
    {"symbol": "CBTC/USDCx", "last_price": 85125, "volume_24h": 193.0, "quote_volume_24h_usd": 16430419.0,
     "trade_count_24h": 2080286}]}
TRADES = {"count": 2, "trades": [
    {"id": 109870826, "trade_id": "c13b9c88", "symbol": "CBTC/USDCx", "quantity": 0.00016, "price": 85126,
     "side": "sell", "status": "pending", "created_at": "2026-10-04T09:13:52.574229Z"},
    {"id": 109870827, "trade_id": "2b7cc87f", "symbol": "CBTC/USDCx", "quantity": 0.000154, "price": 85125,
     "side": "buy", "status": "pending", "created_at": "2026-10-04T09:13:52.655223Z"}]}


def _adapter(seen: list, key: str | None = None) -> TempleAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/exchange/settled_volume":
            return httpx.Response(200, json=SETTLED)
        keyed = {"/api/v1/market/orderbook": BOOK, "/api/v1/market/ticker": TICKER, "/api/v1/market/trades": TRADES}
        if request.url.path in keyed:
            if request.headers.get("X-API-Key") != "k":
                return httpx.Response(401, json={"code": "API_KEY_AUTH_FAILED"})
            return httpx.Response(200, json=keyed[request.url.path])
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
    assert book.best_bid.price == Decimal("0.123") and book.best_ask.size == Decimal(4000)
    assert book.symbol == "CC/USDCx"  # answered as CC whatever was asked


async def test_ticker_and_trades_unwrap_the_live_envelopes() -> None:
    async with _adapter([], key="k") as temple:
        tickers = {t.symbol: t for t in await temple.tickers()}
        trades = await temple.recent_trades("CBTC/USDCx")
        markets = [mk.symbol for mk in await temple.markets()]
    assert tickers["CBTC/USDCx"].turnover_24h == Decimal("16430419.0")
    assert tickers["CC/USDCx"].best_bid is None  # Temple's ticker carries no bid/ask
    assert trades[0].trade_id == "2b7cc87f" and trades[0].side.value == "buy"
    assert markets == ["CC/USDCx", "CBTC/USDCx"]


async def test_markets_come_from_settled_volume() -> None:
    async with _adapter([]) as temple:
        names = [(mk.base, mk.quote) for mk in await temple.markets()]
    assert names == [("CBTC", "USDCx"), ("CC", "USDCx")]


def test_wire_symbol_rewrites_only_the_cc_token() -> None:
    assert wire_symbol("CC/USDCx") == "Amulet/USDCx"
    assert wire_symbol("CBTC/USDCx") == "CBTC/USDCx"
