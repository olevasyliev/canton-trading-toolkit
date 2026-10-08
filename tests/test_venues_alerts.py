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


def test_only_loud_alerts_reach_the_channel():
    scan = [{"token": "X", "buy_on": "cantex", "sell_on": "tradecraft", "size_usd": 900,
             "net_usd": 1.2, "clears": True},
            {"token": "Y", "buy_on": "cantex", "sell_on": "tradecraft", "size_usd": 5000,
             "net_usd": 40.0, "clears": True}]
    got = {a.key: a.loud for a in al.evaluate(TOKENS, PREMIUM, scan, {"EDELX": 0.0340})}
    # USDXLR 1.1% off in a $170K pool, CC 2% vs the world, a $40 spread: loud. A $1.20 spread is site-only.
    assert got == {"peg:USDXLR:above": True, "premium:CC:below": True, "route:Y:cantex": True,
                   "route:X:cantex": False, "move:EDELX:up": True}


def test_moves_of_tokens_with_an_outside_price_stay_off_the_channel():
    tokens = [{"key": "EXAU", "symbol": "eXAU", "liquidity_usd": 800_000, "price_usd": 5000}]
    [a] = al.evaluate(tokens, [], [], {"EXAU": 4000})
    assert a.kind == "move" and not a.loud


def test_channel_caps_a_day_and_keeps_its_own_crossing():
    a, b, c = (al.Alert(f"route:{k}:x", "route", k, k, loud=True) for k in "ABC")
    quiet = al.Alert("route:Q:x", "route", "q", "q")
    state = {}
    day = 10 * 86400
    assert al.channel(state, [a, quiet], day + 100) == [a]
    assert al.channel(state, [a, b, c], day + 400) == [b]       # a still true; c past the cap of 2
    assert al.channel(state, [], day + 700) == []
    assert al.channel(state, [c], day + 3600) == []             # c inside its cooldown from the drop
    assert al.channel(state, [], day + 3700) == []
    d = al.Alert("peg:D:above", "peg", "d", "<b>D</b>", loud=True)
    assert al.channel(state, [c, d], day + 86400 + 30) == [c, d]  # a new day: the cap resets
    assert "💵 <b>D</b>" in al.channel_html([d])
