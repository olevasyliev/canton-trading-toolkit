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
    # one swap on each venue: Cantex at its measured 0.86 CC, Tradecraft at the 1.5 CC assumption
    assert all(r["net_cc"] == pytest.approx(r["gross_cc"] - 2.36) for r in routes)

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


def test_router_stats_is_a_median_not_a_sum():
    fills = [{"edge_bps": b, "extra_usd": b / 10} for b in (10, 20, 300, 30)]
    st = m.router_stats(fills)
    assert st == {"window": 4, "median_edge_bps": 25.0, "median_extra_usd": 2.5, "max_edge_bps": 300}
    assert m.router_stats([]) == {}


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


def test_change_ignores_a_one_bar_wick_at_the_start_of_the_window():
    hour = 3_600_000
    # steady at 100, one bar dips to 90 exactly 24h ago, steady again, now 101
    series = [(i * hour, 100.0) for i in range(20)] + [(20 * hour, 90.0)] + \
             [(i * hour, 100.0) for i in range(21, 44)] + [(44 * hour, 101.0)]
    assert m.change(series, 44 * hour, 24 * hour) == pytest.approx(0.01)


def test_book_depth_counts_only_the_band_on_both_sides():
    from canton_toolkit import BookLevel
    bids = [BookLevel(Decimal("99.5"), Decimal(2)), BookLevel(Decimal("98"), Decimal(100))]
    asks = [BookLevel(Decimal("100.5"), Decimal(1)), BookLevel(Decimal("102"), Decimal(100))]
    # mid 100, 1% band: 99.5*2 + 100.5*1, the far levels left out, in a quote worth $1.01
    assert m.book_depth_usd(bids, asks, Decimal(100), Decimal("1.01")) == Decimal("299.5") * Decimal("1.01")


def test_weighted_price_follows_the_deeper_venue():
    deep, thin = (Decimal(100), Decimal(9000)), (Decimal(110), Decimal(1000))
    assert m.weighted_usd([deep, thin]) == Decimal(101)
    assert m.weighted_usd([]) is None


def test_amm_depth_is_about_half_a_percent_of_each_side():
    pool = cantex_pool("USDCx", 1_000_000, 122_000)
    depth = m.amm_depth_usd(pool, Decimal("0.122"))
    assert float(depth) == pytest.approx(2 * 122_000 * 0.004988, rel=1e-3)


def _book(fee="0"):
    D = Decimal
    return m.BookVenue("rocky", "CBTC-USDCX", ((D(100), D(1)), (D(99), D(1))),
                       ((D(101), D(1)), (D(102), D(1))), D(1), D(fee))


def test_book_walks_levels_and_reports_a_thin_book():
    b = _book()
    assert b.sell_base(Decimal("1.5")) == Decimal(100) + Decimal("0.5") * 99
    assert b.buy_base(Decimal(152)) == 1 + Decimal(51) / 102
    assert b.sell_base(Decimal(3)) is None and b.buy_base(Decimal(1000)) is None
    assert _book("0.001").sell_base(Decimal(1)) == Decimal(100) * Decimal("0.999")


def test_book_round_trips_through_json():
    b = _book("0.00025")
    assert m.book_from_json(m.book_to_json(b, "USDCX")) == b


def test_dollar_routes_count_swaps_and_compare_like_for_like():
    stable = {"cantex": cantex_pool("USDCx", 10_000_000, 1_220_000)}
    pools = {"cantex": cantex_pool("CBTC", 10_000_000, Decimal("14.4"))}  # about $84.7K per CBTC
    routes = m.DollarRoutes(pools, stable, {"rocky": _book()}, Decimal(1))
    assert routes.venues == ["cantex", "rocky"]
    assert routes.swaps("cantex") == 2 and routes.swaps("rocky") == 1
    assert routes.buy("rocky", Decimal(101)) == 1
    assert routes.sell("rocky", Decimal(1)) == 100


def test_usd_scan_only_adds_trips_that_touch_the_book():
    stable = {"cantex": cantex_pool("USDCx", 10_000_000, 1_220_000)}
    pools = {"cantex": cantex_pool("X", 1_000_000, 1_000_000), "tradecraft": tc_pool("X", 1_000_000, 1_000_000)}
    routes = m.DollarRoutes(pools, stable, {"rocky": _book()}, Decimal(1))
    rows = m.usd_scan("X", routes, Decimal("0.122"))
    assert {(r["buy_on"], r["sell_on"]) for r in rows} == {
        ("cantex", "rocky"), ("rocky", "cantex"), ("tradecraft", "rocky"), ("rocky", "tradecraft")}
    # a pool route is two swaps (its CC/USDCx leg costed as Cantex), the book one: three a trip
    cost = {frozenset(("cantex", "rocky")): 0.86 * 2 + 1.5, frozenset(("tradecraft", "rocky")): 1.5 + 0.86 + 1.5}
    assert all(r["cost_cc"] == pytest.approx(cost[frozenset((r["buy_on"], r["sell_on"]))]) for r in rows)


def test_swap_cost_uses_a_venues_own_stated_fee():
    assert m.swap_cost_cc("cantex", Decimal("0.125")) == Decimal("0.86")  # measured
    assert m.swap_cost_cc("tradecraft", Decimal("0.125")) == Decimal("1.5")  # assumed
    assert m.swap_cost_cc("oneswap", Decimal("0.125")) == Decimal(14)  # $1.75 at $0.125


def test_router_compares_with_the_runner_up_not_the_worst():
    pools = {"cantex": cantex_pool("USDCx", 1_600_000, 196_000),
             "tradecraft": tc_pool("USDCx", 6_000_000, 735_000),
             "thin": cantex_pool("USDCx", 16_000, 1_960)}
    fill = m.route_order(pools, "sell", 1000, Decimal("0.1225"), Decimal("0.1225"))
    assert fill["other"] != "thin"


def test_spread_study_counts_one_episode_per_standing_spread():
    import spread_study as ss
    k = "X:cantex>tradecraft"
    samples = [{"t": 0, "routes": [(k, 0.2, 100, "a")]},
               {"t": 20, "routes": [(k, 0.9, 300, "a")]},
               {"t": 40, "routes": [(k, 0.7, 300, "b")]},   # still open; the pools traded
               {"t": 60, "routes": [(k, 0.1, 100, "c")]},   # closed
               {"t": 80, "routes": [("USDCX:tradecraft>cantex", 2.0, 900, "d")]}]
    run = ss.summarize(samples, 20)
    assert run["episodes"] == 2 and run["pools_changed"] == 1 and run["stable_episodes"] == 1
    assert run["capture_usd"] == 2.9  # each spread taken once, at its best
    assert sorted(t["life_s"] for t in run["top"]) == [20, 40]
