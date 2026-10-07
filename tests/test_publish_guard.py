"""The publish guard: a venue's card and page are only overwritten with plausible figures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import publish_guard as pg
import venue_pages as vp
from test_venue_pages import T, data

M = 60


def test_a_first_publish_and_normal_moves_go_through(tmp_path):
    g = pg.PublishGuard(tmp_path / "published.json")
    assert g.check("temple", {"spot_volume": 34e6}, T) is None
    g.accept("temple", {"spot_volume": 34e6}, {"rule": "spot_volume", "title": "t", "sub": "s"}, T)
    assert g.check("temple", {"spot_volume": 50e6}, T + 5 * M) is None  # 1.5x in 5 minutes: fine
    assert g.check("temple", {"spot_volume": 12e6}, T + 5 * M) is None  # a little over a third: fine


def test_an_implausible_jump_is_held_until_it_is_confirmed(tmp_path):
    g = pg.PublishGuard(tmp_path / "published.json")
    g.accept("cantex", {"tvl": 3e6, "spot_volume": 3.7e6}, {"rule": "kind_volume"}, T)
    why = g.check("cantex", {"tvl": 10e6, "spot_volume": 3.7e6}, T + 5 * M)  # more than 3x
    assert why and "tvl 3,000,000 -> 10,000,000" in why
    assert g.check("cantex", {"tvl": 0.9e6, "spot_volume": 3.7e6}, T + 5 * M)  # under a third
    # the same new level, seen three checks in a row and 15 minutes after the last good one: real
    for k in (1, 2):
        assert g.check("cantex", {"tvl": 10e6, "spot_volume": 3.7e6}, T + (5 + 5 * k) * M)
    assert g.check("cantex", {"tvl": 10.2e6, "spot_volume": 3.7e6}, T + 20 * M) is None
    # a level that keeps changing is never confirmed
    g.accept("cantex", {"tvl": 3e6}, {"rule": "kind_volume"}, T)
    for k, v in enumerate((10e6, 30e6, 10e6, 30e6, 10e6)):
        assert g.check("cantex", {"tvl": v}, T + (20 + 5 * k) * M)


def test_a_missing_figure_or_stale_data_holds_the_card(tmp_path):
    g = pg.PublishGuard(tmp_path / "published.json")
    g.accept("temple", {"spot_volume": 34e6}, {"rule": "spot_volume"}, T)
    assert "spot_volume missing" in g.check("temple", {"spot_volume": None}, T + 5 * M)
    assert "min old" in g.check("temple", {"spot_volume": 34e6}, T + 5 * M, age_s=pg.STALE_AFTER_S + 1)
    # a lead that changes rule is not a jump
    g.accept("tradecraft", {"headline:tvl": 3e6}, {"rule": "tvl"}, T)
    assert g.check("tradecraft", {"headline:token_depth": 6e3}, T + 5 * M) is None


def test_the_collector_build_keeps_the_last_good_card_and_page(tmp_path):
    pytest.importorskip("PIL")
    g = pg.PublishGuard(tmp_path / "venues" / "published.json")
    vp.build(tmp_path, data(), T, guard=g)
    page = tmp_path / "venues" / "cantex" / "index.html"
    card = page.parent / "card.png"
    before = (page.read_text(), card.read_bytes())
    d = data()
    next(v for v in d["venues"]["venues"] if v["id"] == "cantex")["volume_24h_usd"] = 40_000_000  # 10x in 5 min
    heads = vp.build(tmp_path, d, T + 300, guard=g)
    assert "cantex" not in heads and (page.read_text(), card.read_bytes()) == before
    assert "temple" in heads  # other venues still publish
    # the index keeps showing the held venue's last good headline
    assert "The largest AMM on Canton by volume" in (tmp_path / "venues" / "index.html").read_text()
    # stale data holds too: Cantex's last successful read was an hour ago
    g2 = pg.PublishGuard(tmp_path / "venues" / "published.json")
    assert "cantex" not in vp.build(tmp_path, data(), T + 3600, guard=g2, fresh={"cantex": T})
    # what the cards show is on disk for the cross-check
    saved = pg.PublishGuard(tmp_path / "venues" / "published.json").state["venues"]
    assert saved["cantex"]["figures"]["spot_volume"] == 4_000_000 and saved["temple"]["t"] == T + 3600
