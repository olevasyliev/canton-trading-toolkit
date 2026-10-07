"""Offline tests for the Canton Venues per-venue pages and share cards."""

from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import venue_pages as vp

T = 1791380618  # 2026-10-07 13:43 UTC


def _venue(i, name, kind, vol, status="priced"):
    return {"id": i, "name": name, "kind": kind, "note": f"{name} note.", "status": status, "volume_24h_usd": vol}


def _row(side, size, out, best):
    return {"side": side, "size_usd": size, "out": out, "best": best, "edge_bps": 10.0}


def data():
    venues = [
        _venue("temple", "Temple", "Spot order book", 34_000_000),
        _venue("cantex", "Cantex", "Spot AMM", 4_000_000),
        _venue("tradecraft", "Tradecraft", "Spot AMM", 200_000),
        _venue("rocky", "Rocky", "Spot order book", 2_800_000),
        _venue("rocky_perp", "Rocky perps", "Perpetuals", 4_600_000),
        _venue("ekiden", "Ekiden", "Perpetuals", 280_000),
        _venue("poolparty", "Pool Party", "Spot AMM", 2_600),
        _venue("oneswap", "OneSwap", "Spot AMM", None),
        _venue("silvana", "Silvana", "Private order book", None, status="closed"),
    ]
    both = {"cantex": 1.0, "tradecraft": 1.1}
    pairs = [
        {"key": "USDCX", "kind": "cc", "symbol": "USDCx", "venues": ["cantex", "tradecraft"], "rows": [
            _row("sell", 100, both, "cantex"), _row("buy", 100, both, "cantex"),
            _row("sell", 50_000, both, "tradecraft"), _row("buy", 50_000, both, "tradecraft")]},
        {"key": "CBTC:USD", "kind": "usd", "token": "CBTC", "symbol": "CBTC",
         "venues": ["cantex", "temple", "rocky"], "rows": [
             _row("buy", 10_000, {"cantex": 1, "temple": 2, "rocky": 1.5}, "temple"),
             _row("sell", 10_000, {"cantex": 1, "temple": 2, "rocky": 1.5}, "temple"),
             _row("buy", 50_000, {"cantex": 1, "temple": 2, "rocky": None}, "temple"),
             _row("sell", 50_000, {"cantex": 1, "temple": 2, "rocky": None}, "temple")]},
    ]
    tokens = [
        {"symbol": "USDCx", "key": "USDCX", "venues": {
            "cantex": {"price_usd": 1.0, "liquidity_usd": 400_000, "depth_1pct_usd": 2_000},
            "tradecraft": {"price_usd": 1.0, "liquidity_usd": 1_200_000, "depth_1pct_usd": 3_000},
            "oneswap": {"price_usd": 0.99, "liquidity_usd": 36_000, "depth_1pct_usd": 180},
            "poolparty": {"price_usd": 1.0, "liquidity_usd": 12_000, "depth_1pct_usd": 60}}},
        {"symbol": "CBTC", "key": "CBTC", "venues": {
            "temple": {"price_usd": 83_000, "liquidity_usd": 490_000, "depth_1pct_usd": 490_000,
                       "market": "CBTC/USDCx", "spread_bps": 0.1},
            "rocky": {"price_usd": 83_000, "liquidity_usd": 36_000_000, "depth_1pct_usd": 36_000_000,
                      "market": "CBTC-USDCX", "spread_bps": 0.0}}},
    ]
    pool = lambda v, tvl: {"venue": v, "pair": "CC/USDCx", "tvl_usd": tvl, "volume_24h_usd": 1000.0,
                           "fee": 0.003, "fee_apr": 0.01}
    lp = [pool("tradecraft", 1_200_000), pool("cantex", 400_000), pool("oneswap", 36_000),
          pool("poolparty", 12_000)]
    perp = lambda v, base, vol, oi: {"venue": v, "symbol": base, "base": base, "quote": "USDCx",
                                     "last": 1.0, "mark": None, "index": None, "basis": -0.001,
                                     "funding_rate": None, "open_interest_usd": oi, "turnover_24h_usd": vol,
                                     "spread_bps": 1.0}
    perps = [perp("rocky_perp", "BTC", 4_000_000, None), perp("rocky_perp", "ETH", 600_000, None),
             perp("ekiden", "BTC", 200_000, 40_000), perp("ekiden", "ETH", 60_000, 10_000),
             perp("ekiden", "CC", 20_000, 2_000)]
    return {"venues": {"t": T, "venues": venues, "spot_volume_24h_usd": 41_002_600},
            "tokens": {"t": T, "tokens": tokens}, "execution": {"t": T, "pairs": pairs},
            "lp": {"t": T, "pools": lp}, "perps": {"t": T, "markets": perps}}


def test_every_live_venue_gets_a_page_and_closed_ones_do_not():
    f = vp.facts(data())
    assert set(f) == {"temple", "cantex", "tradecraft", "rocky", "ekiden", "oneswap", "pool-party"}
    # Rocky's spot and perp rows are one venue with both kinds
    assert f["rocky"]["kind"] == "Spot order book + Perpetuals"
    assert f["rocky"]["perp_volume"] == 4_600_000


def test_headline_takes_the_first_ranking_a_venue_leads():
    f = vp.facts(data())
    h = {s: vp.headline(x) for s, x in f.items()}
    assert h["temple"]["rule"] == "spot_volume" and "$34.0M" in h["temple"]["sub"]
    assert h["cantex"]["rule"] == "kind_volume" and h["cantex"]["title"] == "The largest AMM on Canton by volume"
    assert h["rocky"]["rule"] == "perp_volume"
    # Tradecraft leads no volume ranking; it wins both $50K CC-pair quotes
    assert h["tradecraft"]["rule"] == "exec_cc"
    assert h["tradecraft"]["title"] == "Best execution at $50K"
    assert "2 of 2 CC-pair quotes" in h["tradecraft"]["sub"]
    assert h["ekiden"]["rule"] == "perp_markets" and "3 markets: BTC, ETH, CC" in h["ekiden"]["sub"]
    # leads nothing: a plain count, never a superlative
    assert h["oneswap"]["rule"] == "pools" and "most" not in h["oneswap"]["title"].lower()


def test_a_tie_or_a_minority_of_quotes_is_not_a_lead():
    d = data()
    d["execution"]["pairs"][0]["rows"][3]["best"] = "cantex"  # $50K: one each
    assert vp.facts(d)["tradecraft"]["exec_lead"] == []


def test_card_ranks_only_the_top_half():
    panels = {p["k"]: p for p in vp.stats(vp.facts(data())["ekiden"])}
    assert panels["Perps volume, 24h"]["n"] == "as the venue reports it"  # #2 of 2 is not shown
    panels = {p["k"]: p for p in vp.stats(vp.facts(data())["temple"])}
    assert panels["Spot volume, 24h"]["n"] == "#1 of 5 spot venues"


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.links, self.title = {}, [], None
        self._t = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            self.meta[a.get("property") or a.get("name")] = a.get("content")
        elif tag == "a":
            self.links.append(a.get("href"))
        self._t = tag == "title"

    def handle_data(self, data):
        if self._t and self.title is None:
            self.title = data


def test_page_carries_its_own_social_card_and_share_link(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    page = (tmp_path / "venues" / "temple" / "index.html").read_text()
    p = _Meta()
    p.feed(page)
    assert p.title.startswith("Temple on Canton")
    assert p.meta["twitter:card"] == "summary_large_image"
    assert p.meta["og:url"] == "https://cantonvenues.com/venues/temple/"
    img = urlparse(p.meta["og:image"])
    assert img.path == "/venues/temple/card.png" and p.meta["twitter:image"] == p.meta["og:image"]
    assert (p.meta["og:image:width"], p.meta["og:image:height"]) == ("1200", "630")
    intent = next(h for h in p.links if h.startswith("https://x.com/intent/post"))
    q = parse_qs(urlparse(intent).query)
    assert q["text"][0].startswith("Temple (@temple_ny) on Canton Venues") and q["url"] == ["https://cantonvenues.com/venues/temple/"]
    # no verified handle: the venue is named, nobody is tagged
    one = (tmp_path / "venues" / "oneswap" / "index.html").read_text()
    oq = parse_qs(urlparse(next(h for h in _links(one) if "intent/post" in h)).query)
    assert oq["text"][0].startswith("OneSwap on Canton Venues") and "@" not in oq["text"][0]
    assert 'href="temple/"' in (tmp_path / "venues" / "index.html").read_text()


def _links(page):
    p = _Meta()
    p.feed(page)
    return p.links


def test_cards_are_1200_by_630_and_kept_between_redraws(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    vp.build(tmp_path, data(), T)
    card = tmp_path / "venues" / "rocky" / "card.png"
    with Image.open(card) as im:
        assert im.size == (1200, 630) and im.format == "PNG"
    before = card.stat().st_mtime_ns
    vp.build(tmp_path, data(), T + 300, cards=False)
    assert card.stat().st_mtime_ns == before
    assert f"card.png?v={int(card.stat().st_mtime)}" in (card.parent / "index.html").read_text()


def test_an_unreachable_venue_keeps_its_last_page(tmp_path):
    pytest.importorskip("PIL")
    vp.build(tmp_path, data(), T)
    d = data()
    next(v for v in d["venues"]["venues"] if v["id"] == "ekiden")["status"] = "down"
    heads = vp.build(tmp_path, d, T + 300, cards=False)
    assert "ekiden" not in heads and (tmp_path / "venues" / "ekiden" / "index.html").exists()
