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
    small, big = {"cantex": 1.01, "tradecraft": 1.0}, {"cantex": 1.0, "tradecraft": 1.1}
    pairs = [
        {"key": "USDCX", "kind": "cc", "symbol": "USDCx", "venues": ["cantex", "tradecraft"], "rows": [
            _row("sell", 100, dict(small), "cantex"), _row("buy", 100, dict(small), "cantex"),
            _row("sell", 50_000, dict(big), "tradecraft"), _row("buy", 50_000, dict(big), "tradecraft")]},
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
    # DefiLlama's Canton DEX list as summary.json carries it: it confirms the "on Canton" spot rankings
    llama = [{"name": "Temple", "volume_24h": 34_500_000}, {"name": "Cantex", "volume_24h": 4_500_000},
             {"name": "Rocky Exchange Spot", "volume_24h": 2_600_000}, {"name": "Pool Party", "volume_24h": 2_900}]
    return {"venues": {"t": T, "venues": venues, "spot_volume_24h_usd": 41_002_600},
            "tokens": {"t": T, "tokens": tokens}, "execution": {"t": T, "pairs": pairs},
            "lp": {"t": T, "pools": lp}, "perps": {"t": T, "markets": perps},
            "summary": {"cc_usd": 0.12, "ecosystem": {"venues": llama}}}


def test_every_live_venue_gets_a_page_and_closed_ones_do_not():
    f = vp.facts(data())
    assert set(f) == {"temple", "cantex", "tradecraft", "rocky", "ekiden", "oneswap", "pool-party"}
    # Rocky's spot and perp rows are one venue with both kinds
    assert f["rocky"]["kind"] == "Spot order book + Perpetuals"
    assert f["rocky"]["perp_volume"] == 4_600_000


def test_headline_is_the_first_thing_a_venue_leads():
    f = vp.facts(data())
    h = {s: vp.headline(x) for s, x in f.items()}
    assert h["temple"]["rule"] == "spot_volume" and "$34M" in h["temple"]["sub"]
    assert h["temple"]["title"] == "The largest spot venue on Canton" and h["temple"]["scope"] == "canton"
    assert h["temple"]["next"] == "Cantex"  # runner-up by spot volume
    assert h["cantex"]["rule"] == "kind_volume" and h["cantex"]["title"] == "The largest AMM on Canton by volume"
    assert h["rocky"]["rule"] == "perp_volume"
    # derivatives coverage is not confirmed by any outside list: scoped to what we read
    assert h["rocky"]["title"] == "The largest perps venue among the Canton venues we read"
    # Tradecraft wins both $50K CC-pair quotes, but its first lead is its pool liquidity; its card
    # stays plain by default ("lead": False), so the lead is kept but not drawn
    tc = f["tradecraft"]["leads"][0]
    assert tc["rule"] == "tvl" and tc["next"] == "Cantex"
    assert h["tradecraft"]["rule"] == "pools"
    assert h["ekiden"]["rule"] == "perp_markets" and "3 markets trading: BTC, ETH, CC" in h["ekiden"]["sub"]
    # leads nothing: a plain count, never a superlative
    assert h["oneswap"]["rule"] == "pools" and "most" not in h["oneswap"]["title"].lower()
    assert vp.lead_line(f["oneswap"], h["oneswap"]) is None


def test_no_headline_or_card_tile_ranks_venues_on_price_across_sizes():
    d = data()
    for v in d["venues"]["venues"]:  # nobody leads a volume, pool or count ranking any more
        v["volume_24h_usd"] = None
    d["lp"]["pools"], d["perps"]["markets"], d["tokens"]["tokens"] = [], [], []
    for f in vp.facts(d).values():
        h = vp.headline(f)
        assert "best execution" not in h["title"].lower() and " from $" not in h["title"]
        assert not any(p["k"].startswith("Best price") for p in vp.stats(f))
    # a single quote won clearly is still a fact, named by direction and size
    lead = vp.facts(d)["tradecraft"]["leads"][0]
    assert lead["rule"] == "best_quote" and lead["title"] == "Best price to buy USDCx with CC at $50K"


def test_a_thin_pool_is_not_a_competitor():
    d = data()
    rows = d["execution"]["pairs"][0]["rows"]
    for r in rows:  # Pool Party joins the CC/USDCx quotes and wins the $100 ones
        r["out"]["poolparty"] = 2.0
    rows[0]["best"] = rows[1]["best"] = "poolparty"
    usdcx = d["tokens"]["tokens"][0]["venues"]
    usdcx["poolparty"]["liquidity_usd"] = vp.MIN_LIQUIDITY_USD + 1
    assert vp.facts(d)["pool-party"]["exec"]["cc:100"] == {"won": 2, "of": 2}
    usdcx["poolparty"]["liquidity_usd"] = vp.MIN_LIQUIDITY_USD - 1
    f = vp.facts(d)
    assert "cc:100" not in f["pool-party"]["exec"]  # neither wins nor is counted
    assert f["cantex"]["exec"]["cc:100"] == {"won": 2, "of": 2}  # the win goes to the best real pool
    # a quote whose only alternative is thin counts for nobody
    usdcx["tradecraft"]["liquidity_usd"] = 500
    f = vp.facts(d)
    assert "cc:100" not in f["cantex"]["exec"] and "cc:100" not in f["tradecraft"]["exec"]


def test_a_tie_never_leads():
    d = data()
    lp = d["lp"]["pools"]
    lp[1]["tvl_usd"] = lp[0]["tvl_usd"]  # Cantex and Tradecraft level on pool liquidity
    assert all(x["rule"] != "tvl" for x in vp.facts(d)["tradecraft"]["leads"])


def test_method_lines_never_repeat():
    lines = vp.method_lines(["Order books from Temple's API (account key); settled volume per market. Taker fee 1 bp.",
                             "Taker fee 1 bp is Temple's published rate."])
    assert lines == ["Order books from Temple's API (account key).", "Settled volume per market.",
                     "Taker fee 1 bp is Temple's published rate."]
    for f in vp.facts(data()).values():
        assert len(f["method"]) == len(set(f["method"]))
    assert vp.method_lines(["MainNet tickers.", "MainNet tickers."]) == ["MainNet tickers."]


def test_card_ranks_only_the_top_half():
    panels = {p["k"]: p for p in vp.stats(vp.facts(data())["ekiden"])}
    # #2 of 2 is not shown; Ekiden reports quote-token turnover, not dollars
    assert panels["Perps volume, 24h"]["n"] == "quote-token turnover"
    panels = {p["k"]: p for p in vp.stats(vp.facts(data())["temple"])}
    assert panels["Spot volume, 24h"]["n"] == "#1 of 5 spot venues reporting volume"


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
    assert q["text"][0].startswith("Where Temple (@temple_ny) leads on Canton Venues: the largest spot venue")
    assert "Where Temple leads: the largest spot venue on Canton." in page and "Next: Cantex, $4M." in page and q["url"] == ["https://cantonvenues.com/venues/temple/"]
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


def test_card_colours_are_the_dashboards_own():
    css = (Path(vp.HERE) / "site" / "index.html").read_text()
    for theme, block in (("light", css.split(':root[data-theme="dark"]')[0]),
                         ("dark", css.split(':root[data-theme="dark"]')[1].split("}")[0])):
        for key, var in (("bg", "--bg"), ("bg2", "--bg-2"), ("line", "--line"), ("line2", "--line-2"),
                         ("text", "--text"), ("text2", "--text-2"), ("accent", "--accent")):
            assert f"{var}: {vp.THEMES[theme][key]};" in block, (theme, var)
    assert f"--up: {vp.UP};" in css and f"--down: {vp.DOWN};" in css


def test_a_long_headline_splits_into_two_even_lines():
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    Image = pytest.importorskip("PIL.Image")
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = vp._font("Bold", 58)
    title = "Best price to sell HANDL for CC at $10K"
    lines = vp._balanced(d, title, font, 1088)
    assert lines and " ".join(lines) == title and len(lines) == 2
    widths = [d.textlength(x, font=font) for x in lines]
    assert max(widths) <= 1088 and min(widths) > 0.6 * max(widths)  # no orphaned last word
    assert vp._balanced(d, "word " * 80, font, 1088) is None


# === venue-style cards and outward text ======================================

import venue_style as vs  # noqa: E402

BANNED = ("·", "—", "–")  # middle dot, em dash, en dash: none in anything we publish


class _Text(HTMLParser):
    """A page's visible text plus its title and meta contents; styles and scripts left out."""

    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], False

    def handle_starttag(self, tag, attrs):
        self._skip = tag in ("style", "script")
        a = dict(attrs)
        if tag == "meta" and a.get("content"):
            self.parts.append(a["content"])
        if tag == "img" and a.get("alt"):
            self.parts.append(a["alt"])

    def handle_endtag(self, tag):
        self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def test_no_middle_dot_or_dash_in_cards_pages_or_share_text(tmp_path):
    pytest.importorskip("PIL")
    d = data()
    next(v for v in d["venues"]["venues"] if v["id"] == "temple")["markets_24h"] = [
        "CBTC/USDCx", "CC/USDCx", "eXAU/USDCx"]
    vp.build(tmp_path, d, T)
    for s, f in vp.facts(d).items():
        head = vp.headline(f)
        card = [head["title"], head["sub"], f"{f['name']} / {f['kind']}", vs.strip_line(), vs.footer_note(f, head),
                vp.stamp(T)] + [x for p in vp.stats(f) for x in (p["k"], p["v"], p["n"])]
        page = _Text()
        page.feed((tmp_path / "venues" / s / "index.html").read_text())
        for text in card + page.parts + [vp.share_text(f, head)]:
            assert not any(b in text for b in BANNED), (s, text)
    index = _Text()
    index.feed((tmp_path / "venues" / "index.html").read_text())
    assert not any(b in x for x in index.parts for b in BANNED)
    assert vs.strip_line() == "Live Canton DEX data from cantonvenues.com"


def test_token_tile_names_the_tokens_and_says_what_we_leave_out():
    d = data()
    f = vp.facts(d)
    tile = vp.token_tile(f["cantex"])
    assert tile["k"] == "Tokens priced" and tile["names"] == ["USDCx"] and "USDCx" in tile["v"]
    # the venue lists a market we do not price: the tile says how many of its markets we cover
    next(v for v in d["venues"]["venues"] if v["id"] == "temple")["markets_24h"] = [
        "CBTC/USDCx", "CC/USDCx", "eXAU/USDCx"]
    f = vp.facts(d)
    tile = next(p for p in vp.stats(f["temple"]) if p.get("names"))
    assert tile["k"] == "Markets we price" and tile["v"] == "CBTC"
    assert tile["n"] == "1 of 3, CC/USDCx, eXAU/USDCx excluded"
    assert any("we price 1 of them as tokens (CBTC)" in m for m in f["temple"]["method"])
    # many names: four at most, then a count of the rest
    assert vp.names_text(["A", "B", "C", "D", "E", "F"]) == "A, B, C, D +2"
    assert vp.names_text(["A", "B", "C"]) == "A, B, C"


def test_a_long_name_list_drops_names_before_it_shrinks_past_the_floor():
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    Image = pytest.importorskip("PIL.Image")
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    p = {"k": "Tokens priced", "names": ["USDC.B", "USDCx", "EDELx", "eXAU", "CBTC", "HECTO"]}
    text, font = vp.fit_value(d, p, lambda z: vp._font("Bold", z), 440, 84, 44)
    assert d.textlength(text, font=font) <= 440 and font.size >= 44
    assert text.startswith("USDC.B") and text.endswith(f"+{6 - len(text.split(', '))}")


def test_perp_tile_agrees_with_the_headline():
    d = data()
    d["perps"]["markets"].append(dict(d["perps"]["markets"][-1], base="XAU", symbol="XAU", turnover_24h_usd=1.0))
    f = vp.facts(d)["ekiden"]
    head = vp.headline(f)
    tile = vp.perp_tile(f)
    assert head["sub"].startswith("3 markets trading")
    assert tile["names"] == ["BTC", "ETH", "CC"] and tile["n"] == "3 trading, 4 listed"
    assert vp.perp_tile(vp.facts(data())["rocky"])["n"] == "2 listed, all trading"


def test_style_table_covers_every_venue_with_open_fonts(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    assert set(vs.STYLES) == {v["slug"] for v in vp.VENUES}
    keys = {"source", "bg", "ink", "muted", "accent", "rule", "tile", "tile_edge", "tile_label", "tile_ink",
            "tile_note", "up", "head", "label", "num", "body", "head_case", "label_case", "track", "radius",
            "border"}
    licences = {p.name.split("-")[0] for p in vp.FONTS.glob("*OFL.txt")} | {"Inter"}  # Inter's is OFL.txt
    for slug, s in vs.STYLES.items():
        assert keys <= set(s), (slug, keys - set(s))
        assert s["source"].startswith("https://"), slug
        for face in {s["head"], s["label"], s["num"], s["body"], s.get("names", s["num"])}:
            assert (vp.FONTS / f"{face}.ttf").exists(), face
            assert face.split("-")[0] in licences, face
    # every venue renders in its own style, 1200x630, with our strip at the foot
    f = vp.facts(data())
    for slug, x in f.items():
        out = tmp_path / f"{slug}.png"
        vs.render(x, vp.headline(x), T, out)
        with Image.open(out) as im:
            assert im.size == (1200, 630)
            assert im.convert("RGB").getpixel((1190, 620)) == vp._hex(vs.OURS["strip"])


def test_our_own_look_stays_available(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    vp.build(tmp_path, data(), T, look="ours")
    with Image.open(tmp_path / "venues" / "temple" / "card.png") as im:
        assert im.convert("RGB").getpixel((5, 300)) == vp._hex(vp.THEMES["light"]["bg"])


# === the verifier's fixes (2026-10-07) ======================================

def test_an_unpriced_cc_pool_still_counts_from_its_cc_side():
    d = data()
    # Pool Party's fifth pool pairs CC with a token no venue names: 2x its CC reserve, never dropped
    d["lp"]["unpriced_pools"] = [{"venue": "poolparty", "pair": "CC/481871d4", "tvl_usd": 54_000.0,
                                  "reason": "token not named by any venue we price"}]
    f = vp.facts(d)["pool-party"]
    assert f["tvl"] == 66_000 and f["pool_n"] == 2 and f["pool_priced"] == 1
    assert vp.headline({**f, "leads": []})["sub"] == "2 CC pools, $66K in liquidity, 1 priced"
    assert vp.pools_text(f) == "2 CC pools, 1 priced"
    tiles = {p["k"]: p for p in vp.stats(f)}
    assert tiles["In pools"]["v"] == "$66K" and tiles["In pools"]["n"] == "2 CC pools, 1 priced"
    assert tiles["Largest pool"]["n"] == "CC and an unnamed token"  # the unnamed pool is the largest
    assert any("counted from the CC side" in m for m in f["method"])
    # the same rule for every AMM: an unpriced pool on Cantex counts in its total and its pool count
    d["lp"]["unpriced_pools"].append({"venue": "cantex", "pair": "CC/USDCx", "tvl_usd": 5_000.0,
                                      "reason": "a second pool for the same token"})
    assert vp.facts(d)["cantex"]["tvl"] == 405_000


def test_volume_is_labelled_as_the_venue_reports_it_only_when_it_reports_dollars():
    f = vp.facts(data())
    assert vp.volume_note("temple", 0.12) == vp.volume_note("tradecraft", 0.12) == "as the venue reports it"
    assert vp.volume_note("poolparty", 0.1177) == "CC side at $0.1177 per CC"
    assert vp.volume_note("cantex", 0.1177) == "CC volume at $0.1177 per CC"
    assert vp.volume_note("rocky", 0.12) == "quote-token turnover"
    pp = {p["k"]: p for p in vp.stats(f["pool-party"])}
    assert pp["Spot volume, 24h"]["n"] == "CC side at $0.1200 per CC"
    note = vp.card_notes(f["pool-party"], vp.headline(f["pool-party"]))
    assert "the CC side of each CC pool, converted at $0.1200 per CC" in note
    assert "as Pool Party reports it" not in note


def test_a_lead_needs_a_clear_margin_over_the_runner_up():
    d = data()
    lp = d["lp"]["pools"]
    lp[1]["tvl_usd"] = lp[0]["tvl_usd"] / 1.05  # Tradecraft only 5% ahead of Cantex on pool liquidity
    f = vp.facts(d)["tradecraft"]
    assert all(x["rule"] != "tvl" for x in f["leads"])
    assert {"rule": "tvl", "next": "Cantex"} == {k: f["near"][0][k] for k in ("rule", "next")}
    # it falls to the next category the rule finds: its USDCx pool, 50% deeper than Cantex's
    assert f["leads"][0]["rule"] == "token_depth" and f["leads"][0]["margin"] == pytest.approx(0.5)
    lp[1]["tvl_usd"] = lp[0]["tvl_usd"] / (1 + vp.LEAD_MARGIN)  # exactly at the margin: a lead
    assert vp.facts(d)["tradecraft"]["leads"][0]["rule"] == "tvl"
    assert vp._lead({"a": 109, "b": 100}, "a") is None and vp._lead({"a": 111, "b": 100}, "a")[0] == "b"


def test_a_best_price_lead_must_survive_known_network_fees():
    d = data()
    d["execution"]["pairs"].append({"key": "HANDL", "kind": "cc", "symbol": "HANDL", "venues": ["cantex", "oneswap"],
                                    "rows": [_row("sell", 1000, {"oneswap": 1.0071, "cantex": 1.0}, "oneswap")]})
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "cantex": {"liquidity_usd": 50_000}, "oneswap": {"liquidity_usd": 100_000}}})
    lead = next(x for x in vp.facts(d)["oneswap"]["leads"] if x["rule"] == "best_quote")
    assert lead["title"] == "Best price to buy HANDL with CC at $1K"
    assert lead["sub"].endswith("pool fees and price impact included, network fees excluded")
    # 71 bp quoted; OneSwap charged its $2 network fee (20 bp at $1K), Cantex 0.86 CC at $0.12 (1 bp)
    assert lead["margin"] * 10_000 == pytest.approx(71 - 20 + 1, abs=1)
    # 25 bp quoted is gone once OneSwap's fee is paid: not a lead
    d["execution"]["pairs"][-1]["rows"][0]["out"]["oneswap"] = 1.0025
    assert not any(x["rule"] == "best_quote" for x in vp.facts(d)["oneswap"]["leads"])
    # a venue whose fee we do not know never wins on the fee we know for the other: 5 bp quoted is not 10
    d["execution"]["pairs"][-1]["rows"][0]["out"] = {"poolparty": 1.0005, "cantex": 1.0}
    d["tokens"]["tokens"][-1]["venues"]["poolparty"] = {"liquidity_usd": 50_000}
    assert not any(x["rule"] == "best_quote" for x in vp.facts(d)["pool-party"]["leads"])


def test_on_canton_only_where_an_outside_list_confirms_it():
    assert vp.CATEGORIES["spot_volume"]["source"] == vp.LLAMA_DEXS
    assert all(c["scope"] == "read" for r, c in vp.CATEGORIES.items() if r not in ("spot_volume", "kind_volume"))
    d = data()
    d["summary"]["ecosystem"]["venues"] = []  # no outside list: nothing is claimed for the whole chain
    h = {s: vp.headline(x) for s, x in vp.facts(d).items()}
    assert h["temple"]["title"] == "The largest spot venue among the Canton venues we read"
    assert h["cantex"]["title"] == "The largest AMM by volume among the Canton venues we read"
    # DefiLlama lists a bigger spot venue than ours: Temple's lead stays scoped to what we read
    d["summary"]["ecosystem"]["venues"] = [{"name": "Temple", "volume_24h": 1}, {"name": "Elsewhere", "volume_24h": 9}]
    assert vp.headline(vp.facts(d)["temple"])["scope"] == "read"
    # a larger entry we cannot place by kind also blocks "the largest AMM on Canton"
    d["summary"]["ecosystem"]["venues"] = [{"name": "Cantex", "volume_24h": 4}, {"name": "Elsewhere", "volume_24h": 9}]
    assert vp.headline(vp.facts(d)["cantex"])["scope"] == "read"
    for x in vp.facts(data()).values():
        for lead in x["leads"]:
            assert ("on Canton" in lead["title"]) == (lead["scope"] == "canton"), lead["title"]


def test_a_keyed_book_never_appears_on_the_card():
    d = data()
    f = vp.facts(d)["temple"]
    tiles = {p["k"]: p for p in vp.stats(f)}
    assert "Within 1% of mid" not in tiles and tiles["Share of spot volume"]["v"] == "83%"
    assert not any(x["rule"] == "token_depth" for x in f["leads"])
    # Rocky's book is public: its depth tile stays and names the pair
    rocky = {p["k"]: p for p in vp.stats(vp.facts(d)["rocky"])}
    assert rocky["Within 1% of mid"]["n"] == "CBTC/USDCx book"


def test_one_money_style_and_tokens_lead_with_non_stablecoins():
    assert [vp.short_money(x) for x in (60_100, 2_636, 84_000, 1_200_000, 490_000, 512, 2.65, 999_960)] == [
        "$60.1K", "$2.6K", "$84K", "$1.2M", "$490K", "$512", "$2.65", "$1M"]
    assert vp.money(34_115_904) == vp.short_money(34_115_904) == "$34.1M"
    f = {"name": "X", "tokens": [{"symbol": s, "key": k, "liquidity_usd": 5_000} for s, k in (
        ("USDC.B", "USDC.B"), ("USDCx", "USDCX"), ("EDELx", "EDELX"), ("eXAU", "EXAU"))]}
    assert vp.token_tile(f)["names"] == ["EDELx", "eXAU", "USDC.B", "USDCx"]


def test_card_footer_names_every_kind_of_figure_on_the_card():
    for slug, f in vp.facts(data()).items():
        head = vp.headline(f)
        note = vp.card_notes(f, head)
        for p in vp.stats(f):
            assert vp.metric_note(p["m"], f) in note, (slug, p["m"])


# === second verifier pass (2026-10-07) ======================================

def test_token_count_names_and_footer_apply_the_1k_rule_on_this_venue():
    d = data()
    # HANDL has $500K on Cantex but $300 on Tradecraft: it counts for Cantex only
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "cantex": {"price_usd": 0.1, "liquidity_usd": 500_000, "depth_1pct_usd": 2_500},
        "tradecraft": {"price_usd": 0.1, "liquidity_usd": 300, "depth_1pct_usd": 2}}})
    f = vp.facts(d)
    tc, cx = vp.token_facts(f["tradecraft"]), vp.token_facts(f["cantex"])
    assert (tc["n"], tc["names"], [x["symbol"] for x in tc["thin"]]) == (1, ["USDCx"], ["HANDL"])
    assert (cx["n"], cx["names"]) == (2, ["HANDL", "USDCx"])
    for slug, t in (("tradecraft", tc), ("cantex", cx)):
        tile = vp.token_tile(f[slug])
        assert tile["names"] == t["names"] and tile["n"] == f"{t['n']} with $1K+ here"
        note = vp.card_notes(f[slug], vp.headline(f[slug]))
        assert f"Tokens: the {t['n']} with $1K or more of liquidity on {f[slug]['name']} itself" in note
    # an order book is held to its depth within 1% of mid, not its total liquidity
    rows = [{"symbol": "A", "key": "A", "market": "A/USDCx", "liquidity_usd": 9_000, "depth_1pct_usd": 400},
            {"symbol": "B", "key": "B", "market": "B/USDCx", "liquidity_usd": 9_000, "depth_1pct_usd": 4_000},
            {"symbol": "C", "key": "C", "liquidity_usd": None}]
    ok, thin = vp.token_split(rows)
    assert [x["symbol"] for x in ok] == ["B"] and [x["symbol"] for x in thin] == ["A", "C"]


def test_page_lists_thin_tokens_apart_and_counts_only_the_rest(tmp_path):
    pytest.importorskip("PIL")
    d = data()
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "tradecraft": {"price_usd": 0.1, "liquidity_usd": 300, "depth_1pct_usd": 2},
        "cantex": {"price_usd": 0.1, "liquidity_usd": 500_000, "depth_1pct_usd": 2_500}}})
    vp.build(tmp_path, d, T)
    page = (tmp_path / "venues" / "tradecraft" / "index.html").read_text()
    head, _, thin = page.partition("Thin, under $1K of liquidity on Tradecraft")
    assert thin and "HANDL" in thin and "HANDL" not in head.split("<h2>Tokens</h2>")[1]
    assert "The 1 token with $1K or more of liquidity on Tradecraft itself" in page
    assert "Thin, under" not in (tmp_path / "venues" / "cantex" / "index.html").read_text()


def test_best_price_sub_names_what_you_receive():
    row = {"symbol": "HANDL", "kind": "cc", "side": "sell", "edge_bps": 87.0}
    assert vp.best_quote_sub(row) == ("0.87% more HANDL than the next venue we read, pool fees and price "
                                      "impact included, network fees excluded")
    assert vp.best_quote_sub({**row, "side": "buy"}).startswith("0.87% more CC than the next venue")
    assert vp.best_quote_sub({**row, "kind": "usd", "side": "sell"}).startswith("0.87% more dollars ")
    d = data()
    d["execution"]["pairs"].append({"key": "HANDL", "kind": "cc", "symbol": "HANDL", "venues": ["cantex", "oneswap"],
                                    "rows": [_row("sell", 1000, {"oneswap": 1.0087, "cantex": 1.0}, "oneswap")]})
    d["tokens"]["tokens"].append({"symbol": "HANDL", "key": "HANDL", "venues": {
        "cantex": {"liquidity_usd": 50_000}, "oneswap": {"liquidity_usd": 100_000}}})
    lead = next(x for x in vp.facts(d)["oneswap"]["leads"] if x["rule"] == "best_quote")
    assert lead["sub"].startswith("0.87% more HANDL than the next venue we read")


def test_tradecraft_card_is_plain_unless_its_lead_is_switched_on():
    tc = next(v for v in vp.VENUES if v["slug"] == "tradecraft")
    assert tc["lead"] is False
    f = vp.facts(data())["tradecraft"]
    h = vp.headline(f)
    assert h["rule"] == "pools" and h["title"] == "Tradecraft, priced live on Canton"
    assert vp.lead_line(f, h) is None and "most" not in vp.share_text(f, h).lower()
    tiles = [p["k"] for p in vp.stats(f)]
    assert not any("1% of mid" in k for k in tiles) and "Largest pool" in tiles
    assert "Within 1% of mid" not in vp.card_notes(f, h) and "Depth:" not in vp.card_notes(f, h)
    # the override restores the lead
    assert vp.headline({**f, "venue": {**f["venue"], "lead": True}})["rule"] == f["leads"][0]["rule"]
