"""Offline tests for the market-board example: pricing, routing, history."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "market_board"))

import board  # noqa: E402
from board import (  # noqa: E402
    CC,
    ROUTER_SIZE_USD,
    Board,
    PairBook,
    Side,
    cantex_net_output,
    edge_bps,
    sell_amount,
    tc_output,
)

from canton_toolkit.core.models import Instrument, Quote  # noqa: E402
from canton_toolkit.venues.tradecraft import PoolState, swap_output  # noqa: E402

CC_I = Instrument(admin="DSO::1220", id="Amulet")
USD_I = Instrument(admin="usdc::1220", id="USDCx")


def _state(token_a="CC", token_b="USDCx", ra="6000000", rb="720000") -> PoolState:
    return PoolState(
        amm_id=f"TC {token_a}/{token_b} LP",
        token_a=token_a,
        token_b=token_b,
        reserve_a=Decimal(ra),
        reserve_b=Decimal(rb),
        lp_supply=Decimal(1),
        lp_token_name=f"TC {token_a}/{token_b} LP",
        lp_fee=Decimal("0.002"),
        operator_fee=Decimal("0.001"),
    )


def _quote(returned: str, fee: str | None = "1.2375") -> Quote:
    return Quote(
        sell_amount=Decimal(1),
        sell_instrument=CC_I,
        buy_instrument=USD_I,
        returned_amount=Decimal(returned),
        returned_instrument=USD_I,
        trade_price=Decimal(1),
        slippage=Decimal(0),
        fee_percentage=Decimal("0.0005"),
        estimated_time_seconds=Decimal(5),
        network_fee=Decimal(fee) if fee else None,
    )


def test_sell_amount_values_both_sides_at_the_same_usd():
    cc_usd, usd_per_cc = Decimal("0.12"), Decimal("0.12")
    assert sell_amount(Side(CC, "USDCx"), Decimal(120), cc_usd, usd_per_cc) == Decimal(1000)
    assert sell_amount(Side("USDCx", CC), Decimal(120), cc_usd, usd_per_cc) == Decimal(120)


def test_tc_output_orients_by_symbol_not_by_pool_order():
    # HANDL/CC is listed token-first on the venue; selling CC must use CC's reserve as input
    state = _state("HANDL", "CC", ra="19800000", rb="373000")
    got = tc_output(state, Side(CC, "HANDL"), Decimal(1000))
    assert got == swap_output(Decimal("373000"), Decimal("19800000"), Decimal(1000), Decimal("0.003"))


def test_cantex_network_fee_is_taken_in_cc_when_buying_cc():
    q = _quote("825.4926663143")
    assert cantex_net_output(q, Side("USDCx", CC), Decimal("0.12")) == Decimal("824.2551663143")


def test_cantex_network_fee_is_converted_when_output_is_not_cc():
    q = _quote("120.88")
    # 1.2375 CC at 0.12 USDCx per CC = 0.1485 USDCx
    assert cantex_net_output(q, Side(CC, "USDCx"), Decimal("0.12")) == Decimal("120.7315")


def test_missing_network_fee_takes_nothing_off():
    assert cantex_net_output(_quote("10", fee=None), Side(CC, "USDCx"), Decimal(1)) == Decimal(10)


def test_edge_bps():
    assert edge_bps(Decimal("1.002"), Decimal(1)) == Decimal("20.000")
    assert edge_bps(Decimal(1), Decimal(0)) == Decimal(0)


def _book_with(tc_sell, cx_sell, tc_buy, cx_buy) -> PairBook:
    book = PairBook("USDCx", _state(), CC_I, USD_I)
    sell, buy = Side(CC, "USDCx"), Side("USDCx", CC)
    book.tc_out[(sell, ROUTER_SIZE_USD)] = Decimal(tc_sell)
    book.cx_out[(sell, ROUTER_SIZE_USD)] = Decimal(cx_sell)
    book.tc_out[(buy, ROUTER_SIZE_USD)] = Decimal(tc_buy)
    book.cx_out[(buy, ROUTER_SIZE_USD)] = Decimal(cx_buy)
    return book


def test_router_alternates_sides_and_fills_on_the_better_venue(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "TradecraftAdapter", lambda: None)
    b = Board(tmp_path)
    book = _book_with(tc_sell="995", cx_sell="997", tc_buy="8300", cx_buy="8200")

    first = b._route([book], Decimal("0.12"), now=100)
    second = b._route([book], Decimal("0.12"), now=400)

    assert (first["side"], first["venue"], first["got"]) == ("sell CC", "cantex", 997.0)
    assert first["extra_usd"] == 2.0  # 2 USDCx more
    assert (second["side"], second["venue"], second["got"]) == ("buy CC", "tradecraft", 8300.0)
    assert second["extra_usd"] == pytest.approx(12.0)  # 100 CC more at 0.12
    assert b.paper["totals"]["fills"] == 2
    assert b.paper["totals"]["by_venue"] == {"cantex": 1, "tradecraft": 1}


def test_router_skips_a_tick_without_both_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "TradecraftAdapter", lambda: None)
    b = Board(tmp_path)
    book = PairBook("USDCx", _state(), CC_I, USD_I)
    assert b._route([book], Decimal("0.12"), now=1) is None
    assert b.paper["tick"] == 0


def test_history_keeps_series_aligned_and_trims_old_points(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "TradecraftAdapter", lambda: None)
    b = Board(tmp_path)
    usd = PairBook("USDCx", _state(), CC_I, USD_I)
    usd.cx_mid = Decimal("0.121")
    b._record_history([usd], now=0)
    # a pair that appears later is back-filled; one that vanishes gets a gap
    handl = PairBook("HANDL", _state("HANDL", "CC", ra="19800000", rb="373000"), CC_I, USD_I)
    b._record_history([handl], now=board.HISTORY_SECONDS + 10)

    h = b.history
    assert h["t"] == [board.HISTORY_SECONDS + 10]
    assert h["pairs"]["USDCx"] == {"tc": [None], "cx": [None]}
    assert len(h["pairs"]["HANDL"]["tc"]) == 1
    assert h["pairs"]["HANDL"]["cx"] == [None]
