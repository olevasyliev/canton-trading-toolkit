"""Offline tests for the Canton Venues model: pricing, premium, scan, desk."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import model as m  # noqa: E402

from canton_toolkit.venues.cantex_public import swap_output as cx_out  # noqa: E402
from canton_toolkit.venues.tradecraft import swap_output as tc_out  # noqa: E402


def cantex_pool(token, cc, tok, fee="0.0005"):
    cc, tok, fee = Decimal(cc), Decimal(tok), Decimal(fee)

    def out(sell_cc, amount):
        return cx_out(cc, tok, amount, fee) if sell_cc else cx_out(tok, cc, amount, fee)

    return m.VenuePool("cantex", token, cc, tok, fee, Decimal("0.9"), out)


def tc_pool(token, cc, tok, fee="0.003"):
    cc, tok, fee = Decimal(cc), Decimal(tok), Decimal(fee)

    def out(sell_cc, amount):
        return tc_out(cc, tok, amount, fee) if sell_cc else tc_out(tok, cc, amount, fee)

    return m.VenuePool("tradecraft", token, cc, tok, fee, Decimal(2) / 3, out)


def test_blended_mid_weights_by_cc_depth():
    a = cantex_pool("USDCx", 1_000_000, 120_000)  # 0.12
    b = tc_pool("USDCx", 3_000_000, 372_000)      # 0.124
    assert m.blended_mid([a, b]) == Decimal("0.123")


def test_token_usd_and_cc_usd():
    usd = cantex_pool("USDCx", 1_000_000, 120_000)
    assert m.cc_usd([usd], Decimal("0.999")) == Decimal("0.11988")
    btc = cantex_pool("CBTC", 7_000_000, 10)  # 700,000 CC per BTC
    assert m.token_usd([btc], Decimal("0.12")) == pytest.approx(Decimal("84000"))


def test_premium_rejects_unit_mismatch():
    assert m.premium(Decimal("101"), Decimal("100")) == Decimal("0.01")
    assert m.premium(Decimal("31.1"), Decimal("1")) is None  # an ounce against a gram
    assert m.premium(Decimal(1), Decimal(0)) is None


def test_ladder_picks_the_venue_that_returns_more():
    pools = {"cantex": cantex_pool("USDCx", 1_600_000, 196_000),
             "tradecraft": tc_pool("USDCx", 6_000_000, 735_000)}
    rows = m.ladder(pools, Decimal("0.1225"), Decimal("0.1225"))
    small = next(r for r in rows if r["side"] == "sell" and r["size_usd"] == 100)
    big = next(r for r in rows if r["side"] == "sell" and r["size_usd"] == 50_000)
    assert small["best"] == "cantex"       # lower fee wins small
    assert big["best"] == "tradecraft"     # deeper pool wins big
    xs = m.crossover_usd(rows, "sell", "cantex", "tradecraft")
    assert len(xs) == 1 and 100 < xs[0] < 50_000


def test_best_round_trip_matches_a_brute_force_search():
    cheap = cantex_pool("X", 1_000_000, 1_000_000)   # X costs 1 CC here
    rich = tc_pool("X", 1_000_000, 960_000)         # and ~1.04 CC there
    x, g = m.best_round_trip(cheap, rich, Decimal("0.12"))
    brute = max((m.round_trip(cheap, rich, Decimal(i * 100)) - Decimal(i * 100), i * 100)
                for i in range(1, 400))
    assert g >= brute[0] - Decimal("0.01")
    assert abs(float(x) - brute[1]) < 200


def test_scan_deducts_the_network_cost_before_a_route_clears():
    flat = {"X": {"cantex": cantex_pool("X", 1_000_000, 1_000_000),
                  "tradecraft": tc_pool("X", 1_000_000, 1_000_000)}}
    routes = m.scan(flat, Decimal("0.12"))
    assert routes and not any(r["clears"] for r in routes)
    assert all(r["net_cc"] == pytest.approx(r["gross_cc"] - 3) for r in routes)

    skew = {"X": {"cantex": cantex_pool("X", 1_000_000, 1_000_000),
                  "tradecraft": tc_pool("X", 1_000_000, 900_000)}}
    best = m.scan(skew, Decimal("0.12"))[0]
    assert best["clears"] and best["buy_on"] == "cantex" and best["sell_on"] == "tradecraft"


def test_route_order_values_the_extra_in_dollars():
    pools = {"cantex": cantex_pool("USDCx", 1_600_000, 196_000),
             "tradecraft": tc_pool("USDCx", 6_000_000, 735_000)}
    fill = m.route_order(pools, "sell", 1000, Decimal("0.1225"), Decimal("0.1225"))
    # selling CC returns USDCx, so the extra is already in dollars at mid 0.1225 / 0.1225
    assert fill["extra_usd"] == pytest.approx(fill["got"] - fill["other_got"])
    assert m.route_order({"cantex": pools["cantex"]}, "sell", 1000, Decimal(1), Decimal(1)) is None


def test_fingerprint_changes_with_reserves():
    a = cantex_pool("X", 100, 100)
    b = cantex_pool("X", 101, 100)
    assert m.fingerprint(a) != m.fingerprint(b)


def test_change_needs_a_point_at_or_before_the_window():
    day = 86_400_000
    series = [(0, 1.0), (day // 2, 1.1), (day, 1.2)]
    assert m.change(series, day, day) == pytest.approx(0.2)
    assert m.change(series[1:], day, day) is None  # history starts inside the window


def test_fee_apr():
    assert m.fee_apr(100_000, 0.003, 2 / 3, 1_000_000) == pytest.approx(0.073)
    assert m.fee_apr(1, 0.003, 1, 0) is None


def _real(venue, token, cc, tok):
    fee = Decimal("0.0005") if venue == "cantex" else Decimal("0.003")
    return m.VenuePool(venue, token, Decimal(cc), Decimal(tok), fee, Decimal("0.9"),
                       m.FORMULAS[venue](Decimal(cc), Decimal(tok), fee), venue, fee)


def test_pool_survives_a_json_round_trip_and_prices_the_same():
    for venue in ("cantex", "tradecraft"):
        p = _real(venue, "CBTC", "1600000", "2.3")
        q = m.pool_from_json(m.pool_to_json(p))
        for sell_cc, amount in ((True, Decimal(5000)), (False, Decimal("0.01"))):
            assert q.out(sell_cc, amount) == p.out(sell_cc, amount)


def test_route_picks_best_venue_per_leg_and_goes_through_cc():
    books = {
        "USDCX": {"cantex": _real("cantex", "USDCx", "1600000", "196000"),
                  "tradecraft": _real("tradecraft", "USDCx", "6000000", "735000")},
        "CBTC": {"cantex": _real("cantex", "CBTC", "3000000", "4.3"),
                 "tradecraft": _real("tradecraft", "CBTC", "600000", "0.86")},
    }
    r = m.route(books, "usdcx", "cbtc", Decimal(20000))
    assert [(L["sell"], L["buy"]) for L in r["legs"]] == [("USDCX", "CC"), ("CC", "CBTC")]
    for L in r["legs"]:
        assert L["out"] == max(L["all"].values())
    assert r["legs"][1]["amount_in"] == r["legs"][0]["out"]
    assert len(m.route(books, "CC", "CBTC", Decimal(1000))["legs"]) == 1
    with pytest.raises(ValueError):
        m.route(books, "CC", "NOPE", Decimal(1))
    with pytest.raises(ValueError):
        m.route(books, "CC", "CC", Decimal(1))
