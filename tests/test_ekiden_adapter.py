"""Offline tests for the Ekiden market-data adapter.

HTTP is mocked with ``httpx.MockTransport``. Every fixture below is a verbatim
excerpt of a live response captured from the Canton testnet gateway
(`https://api.cnt.ekiden.fi`) on 2026-07-27, including its single-letter trade
keys and bare `[price, size]` book rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from canton_toolkit import EkidenAdapter, Side, VenueRequestError

INFO = {
    "build_id": "dev-local",
    "aptos_network": "canton-grpc.validator.cnt.ekiden.fi",
    "package_id": "b323fedaac3a5195cbe48634aba8ac47ad63f8ce8dc2202107cf1a7d67996f97",
    "runtime_manifest_generation": 16,
    "runtime_manifest_phase": "ready",
}
INSTRUMENTS = {
    "category": "linear",
    "list": [
        {
            "symbol": "CC-USDTx",
            "status": "Trading",
            "base_coin": "CC",
            "quote_coin": "USDTx",
            "price_scale": 10,
            "leverage_filter": {"min_leverage": "1", "max_leverage": "4", "leverage_step": "1"},
            "price_filter": {
                "min_price": "0.0000000001",
                "max_price": "1000000",
                "tick_size": "0.0000000001",
            },
            "lot_size_filter": {
                "max_order_qty": "184467440737.09552",
                "min_order_qty": "0.00001",
                "qty_step": "0.00000001",
                "min_notional_value": "1",
            },
            "unified_margin_trade": True,
            "funding_interval": 10,
        },
        {
            "symbol": "TSLA-USDTx",
            "status": "Trading",
            "base_coin": "TSLA",
            "quote_coin": "USDTx",
            "price_scale": 10,
            "leverage_filter": {"min_leverage": "1", "max_leverage": "15", "leverage_step": "1"},
            "price_filter": {
                "min_price": "0.0000000001",
                "max_price": "1000000",
                "tick_size": "0.0000000001",
            },
            "lot_size_filter": {
                "max_order_qty": "184467440737.09552",
                "min_order_qty": "0.00001",
                "qty_step": "0.00000001",
                "min_notional_value": "1",
            },
            "unified_margin_trade": True,
            "funding_interval": 10,
        },
    ],
}
TICKERS = {
    "list": [
        {
            "symbol": "CC-USDTx",
            "addr": "0xf7254e99b79c73c2f28c944cb0078b214d8c337fee4ddef63f237841f710e579",
            "last_price": "0.1223428511",
            "index_price": "0.12298667",
            "mark_price": "0.1231583164",
            "prev_price_24h": "0.122944",
            "high_price_24h": "0.124352",
            "low_price_24h": "0.1213557281",
            "volume_24h": "37437.4972795",
            "turnover_24h": "4621.6849060682",
            "open_interest": "405073.81523987",
            "open_interest_value": "49888.209102667",
            "funding_rate": "-0.000627767",
            "next_funding_time": 1785151200000,
            "best_ask_size": "50",
            "best_ask_price": "0.124246",
            "best_bid_size": "410.17191934",
            "best_bid_price": "0.121997625",
        }
    ]
}
ORDERBOOK = {
    "result": {
        "s": "CC-USDTx",
        "ts": 1785151137560,
        "b": [["0.121997625", "410.17191934"], ["0.1217609809", "81306.83510743"]],
        "a": [["0.124246", "50"], ["0.124251", "50"]],
    },
    "time": 1785151137560,
}
TRADES = {
    "list": [
        {
            "i": "62de99ca-d7b9-516f-bfb8-e4ece8891f4c",
            "s": "CC-USDTx",
            "S": "Sell",
            "v": "0.16393762",
            "p": "0.1223428511",
            "seq": 19198181,
            "T": 1785141995639,
        }
    ]
}
FUNDING = {
    "list": [
        {
            "symbol": "CC-USDTx",
            "funding_rate": "-0.000627767643541308202525701",
            "funding_time": 1785150600000,
        }
    ]
}

BASE_ROUTES: dict[str, object] = {
    "/api/v1/info": INFO,
    "/api/v1/market/instruments-info": INSTRUMENTS,
    "/api/v1/market/tickers": TICKERS,
    "/api/v1/market/orderbook": ORDERBOOK,
    "/api/v1/market/recent-trade": TRADES,
    "/api/v1/market/funding/history": FUNDING,
}


def make_adapter(
    routes: dict[str, object] | None = None,
    recorded: list[httpx.Request] | None = None,
) -> EkidenAdapter:
    table = dict(BASE_ROUTES) if routes is None else routes

    def handler(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(request)
        payload = table.get(request.url.path)
        if payload is None:
            return httpx.Response(404, text="not found")
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EkidenAdapter(base_url="http://test", client=client)


async def test_markets_maps_symbols_and_order_constraints() -> None:
    adapter = make_adapter()
    async with adapter:
        markets = {m.symbol: m for m in await adapter.markets()}
    cc = markets["CC-USDTx"]
    assert cc.base == "CC"
    assert cc.quote == "USDTx"
    assert cc.is_trading
    assert cc.tick_size == Decimal("0.0000000001")
    assert cc.min_order_size == Decimal("0.00001")
    assert cc.min_notional == Decimal("1")
    assert cc.max_leverage == Decimal("4")
    assert cc.funding_interval_minutes == 10
    # An equity perp sits on the same venue, with its own leverage cap.
    assert markets["TSLA-USDTx"].max_leverage == Decimal("15")


async def test_tickers_parse_exactly_and_carry_top_of_book() -> None:
    adapter = make_adapter()
    async with adapter:
        ticker = (await adapter.tickers("CC-USDTx"))[0]
    assert ticker.last_price == Decimal("0.1223428511")
    assert ticker.funding_rate == Decimal("-0.000627767")
    assert ticker.open_interest == Decimal("405073.81523987")
    assert ticker.next_funding_time == datetime(2026, 7, 27, 11, 20, tzinfo=UTC)
    assert ticker.best_bid is not None and ticker.best_bid.price == Decimal("0.121997625")
    assert ticker.best_ask is not None and ticker.best_ask.size == Decimal("50")


async def test_empty_ticker_side_reported_as_zero_is_not_a_price() -> None:
    """An empty side comes back as "0"/"0" in a price field, not as null."""
    routes = dict(BASE_ROUTES)
    routes["/api/v1/market/tickers"] = {
        "list": [{**TICKERS["list"][0], "best_ask_price": "0", "best_ask_size": "0"}]
    }
    adapter = make_adapter(routes)
    async with adapter:
        ticker = (await adapter.tickers("CC-USDTx"))[0]
    assert ticker.best_ask is None
    assert ticker.best_bid is not None


async def test_ticker_symbol_is_passed_as_a_query_param() -> None:
    recorded: list[httpx.Request] = []
    adapter = make_adapter(recorded=recorded)
    async with adapter:
        await adapter.tickers("CC-USDTx")
    req = next(r for r in recorded if r.url.path == "/api/v1/market/tickers")
    assert req.url.params["symbol"] == "CC-USDTx"


async def test_order_book_derives_mid_and_spread() -> None:
    adapter = make_adapter()
    async with adapter:
        book = await adapter.order_book("CC-USDTx")
    assert book.symbol == "CC-USDTx"
    assert book.best_bid is not None and book.best_bid.price == Decimal("0.121997625")
    assert book.best_ask is not None and book.best_ask.price == Decimal("0.124246")
    assert book.spread == Decimal("0.124246") - Decimal("0.121997625")
    assert book.mid_price == (Decimal("0.124246") + Decimal("0.121997625")) / 2
    assert book.timestamp.tzinfo is UTC


async def test_empty_book_side_yields_no_mid_or_spread() -> None:
    routes = dict(BASE_ROUTES)
    routes["/api/v1/market/orderbook"] = {
        "result": {"s": "CC-USDTx", "ts": 1785151137560, "b": [], "a": []}
    }
    adapter = make_adapter(routes)
    async with adapter:
        book = await adapter.order_book("CC-USDTx")
    assert book.mid_price is None
    assert book.spread is None
    assert book.best_bid is None


async def test_rejected_depth_is_caught_before_the_request() -> None:
    """The venue accepts only 10/50/200 and rejects the rest in plain text."""
    recorded: list[httpx.Request] = []
    adapter = make_adapter(recorded=recorded)
    async with adapter:
        with pytest.raises(VenueRequestError, match="depth must be one of"):
            await adapter.order_book("CC-USDTx", depth=3)
    assert not any(r.url.path == "/api/v1/market/orderbook" for r in recorded)


async def test_plain_text_error_body_is_surfaced_not_swallowed() -> None:
    routes = dict(BASE_ROUTES)
    routes["/api/v1/market/orderbook"] = httpx.Response(
        400,
        text="Failed to deserialize query string: depth: unknown variant `3`",
    )
    adapter = make_adapter(routes)
    async with adapter:
        with pytest.raises(VenueRequestError, match="unknown variant"):
            await adapter.order_book("CC-USDTx", depth=10)


async def test_recent_trades_decode_single_letter_keys() -> None:
    adapter = make_adapter()
    async with adapter:
        trade = (await adapter.recent_trades("CC-USDTx"))[0]
    assert trade.trade_id == "62de99ca-d7b9-516f-bfb8-e4ece8891f4c"
    assert trade.side is Side.SELL
    assert trade.price == Decimal("0.1223428511")
    assert trade.size == Decimal("0.16393762")
    assert trade.sequence == 19198181


async def test_funding_history_keeps_full_rate_precision() -> None:
    adapter = make_adapter()
    async with adapter:
        rate = (await adapter.funding_history("CC-USDTx"))[0]
    # 28 significant digits on the wire; parsing must not go through a float.
    assert rate.rate == Decimal("-0.000627767643541308202525701")
    assert rate.symbol == "CC-USDTx"


async def test_connect_rejects_a_gateway_that_is_not_ready() -> None:
    routes = dict(BASE_ROUTES)
    routes["/api/v1/info"] = {**INFO, "runtime_manifest_phase": "syncing"}
    adapter = make_adapter(routes)
    with pytest.raises(VenueRequestError, match="not ready"):
        await adapter.connect()
    await adapter.close()


async def test_calls_before_connect_are_refused() -> None:
    adapter = EkidenAdapter(base_url="http://test")
    with pytest.raises(VenueRequestError, match="not connected"):
        await adapter.markets()


async def test_unexpected_envelope_raises_rather_than_returning_empty() -> None:
    routes = dict(BASE_ROUTES)
    routes["/api/v1/market/tickers"] = {"unexpected": True}
    adapter = make_adapter(routes)
    async with adapter:
        with pytest.raises(VenueRequestError, match="unexpected"):
            await adapter.tickers()
