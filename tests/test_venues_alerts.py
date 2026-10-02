"""Offline tests for Canton Venues alerts: rules, crossing, cooldown."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import alerts as al  # noqa: E402

TOKENS = [
    {"key": "USX", "symbol": "USX", "liquidity_usd": 6_000, "price_usd": 1.03},
    {"key": "USDXLR", "symbol": "USDXLR", "liquidity_usd": 170_000, "price_usd": 1.011},
    {"key": "EDELX", "symbol": "EDELx", "liquidity_usd": 500_000, "price_usd": 0.0360},
    {"key": "EXAG", "symbol": "eXAG", "liquidity_usd": 160_000, "price_usd": 61.8},
]
PREMIUM = [
    {"key": "USX", "symbol": "USX", "kind": "peg", "status": "ok", "premium": 0.03, "canton_usd": 1.03},
    {"key": "USDXLR", "symbol": "USDXLR", "kind": "peg", "status": "ok", "premium": 0.011, "canton_usd": 1.011},
    {"key": "EXAG", "symbol": "eXAG", "kind": "reference", "status": "ok", "premium": 0.004,
     "reference": "silver", "canton_usd": 61.8},
    {"key": "CC", "symbol": "CC", "kind": "reference", "status": "ok", "premium": -0.02,
     "reference": "global CC markets", "canton_usd": 0.12},
]


def keys(alerts):
    return sorted(a.key for a in alerts)


def test_rules_skip_thin_pools_and_small_gaps():
    got = al.evaluate(TOKENS, PREMIUM, [], {"EDELX": 0.0340})
    assert keys(got) == ["move:EDELX:up", "peg:USDXLR:above", "premium:CC:below"]
    # USX is 3% off but its pool is $6K; eXAG is only 0.4% off; EDELx moved +5.9%


def test_route_alert_only_when_it_clears():
    scan = [{"token": "X", "buy_on": "cantex", "sell_on": "tradecraft", "size_usd": 900,
             "net_usd": 1.2, "clears": True},
            {"token": "Y", "buy_on": "cantex", "sell_on": "tradecraft", "size_usd": 50,
             "net_usd": -0.1, "clears": False}]
    assert keys(al.evaluate([], [], scan, {})) == ["route:X:cantex"]


def test_fire_on_crossing_then_quiet_then_cooldown():
    a = al.Alert("peg:X:above", "peg", "x", "x")
    state = {"active": []}
    assert al.fire(state, [a], now=1000) == [a]
    assert al.fire(state, [a], now=1300) == []          # still true: no repeat
    assert al.fire(state, [], now=1600) == []           # cleared
    assert al.fire(state, [a], now=1900) == []          # true again, inside cooldown
    assert al.fire(state, [], now=2000) == []
    assert al.fire(state, [a], now=1000 + al.COOLDOWN_S + 1) == [a]
    assert [f["key"] for f in state["feed"]] == ["peg:X:above", "peg:X:above"]


def test_batch_windows_and_grouping():
    assert al.next_batch_time(0) == 4 * 3600
    assert al.next_batch_time(4 * 3600) == 8 * 3600
    html = al.batch_html([{"kind": "route", "html": "r1"}, {"kind": "peg", "html": "p1"},
                          {"kind": "peg", "html": "p2"}], 0)
    assert html.index("Stablecoins off peg") < html.index("Spreads that cleared")
    assert "• p1" in html and "• p2" in html and "• r1" in html and "Big moves" not in html
