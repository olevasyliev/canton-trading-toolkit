"""Offline tests for the Canton Venues daily note."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import digest  # noqa: E402

NOW = datetime(2026, 10, 3, 9, 5, tzinfo=timezone.utc)


def test_due_once_a_day_after_the_send_hour():
    assert not digest.due({}, NOW.replace(hour=8))
    assert digest.due({}, NOW)
    assert not digest.due({"daily_date": "2026-10-03"}, NOW)
    assert digest.due({"daily_date": "2026-10-02"}, NOW)


def test_compose_ranks_movers_and_skips_thin_pools():
    tokens = [
        {"key": "EDELX", "symbol": "EDELx", "liquidity_usd": 500_000, "change_24h": 0.2},
        {"key": "TINY", "symbol": "TINY", "liquidity_usd": 900, "change_24h": 0.9},
        {"key": "HANDL", "symbol": "HANDL", "liquidity_usd": 170_000, "change_24h": -0.05},
        {"key": "USDXLR", "symbol": "USDXLR", "liquidity_usd": 170_000, "change_24h": 0.0},
    ]
    premium = [
        {"key": "USDXLR", "symbol": "USDXLR", "kind": "peg", "status": "ok", "premium": 0.011},
        {"key": "CC", "symbol": "CC", "kind": "reference", "status": "ok", "premium": -0.004},
    ]
    summary = {"cc_usd": 0.1229, "cc_change_24h": 0.0044, "cantex_24h": {"swaps": 142028},
               "ecosystem": {"dex_volume_24h": 29_200_000, "dex_change_1d": -21.4}}
    desk = {"router": {"n": 12, "extra_usd": 30.5}}
    html = digest.compose(summary, tokens, premium, desk, NOW)
    assert "3 Oct" in html and "$0.1229" in html and "$29.2M" in html and "142,028" in html
    assert "📈 EDELx +20.0%" in html and "TINY" not in html
    assert "📉 HANDL −5.0%" in html
    assert "Off peg: USDXLR +1.10%" in html
    assert "+$30.50" in html
