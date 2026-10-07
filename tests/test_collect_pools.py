"""The collector never drops a CC pool from the liquidity it reports, and counts Pool Party volume
from the CC side only."""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import collect

from cantonvenues.core.models import Instrument
from cantonvenues.venues.reserves import ReservePool

CC = Instrument(admin="DSO::1220", id="Amulet")


def _pp(name, a, ra, b, rb):
    return ReservePool(venue="poolparty", pool_id=name, symbol_a=a, symbol_b=b, token_a=Instrument("", a),
                       token_b=Instrument("", b), reserve_a=Decimal(ra), reserve_b=Decimal(rb),
                       fee=Decimal("0.003"), fee_source="test")


class FakePoolParty:
    async def pools(self):
        return [_pp("Amulet-EDELx", "Amulet", "358114", "EDELx", "1032117"),
                _pp("481871d4-x-Amulet", "481871d4-x", "32547", "Amulet", "228412")]

    async def volume(self):
        return {"Amulet-EDELx": {"Amulet": Decimal("12940"), "EDELx": Decimal("83704")},
                "481871d4-x-Amulet": {}, "HECTO-USDC.B": {"USDC.B": Decimal("101")}}


def test_unnamed_token_pool_is_kept_as_unpriced_liquidity(tmp_path):
    c = collect.Collector(tmp_path)
    c.optional = {"poolparty": FakePoolParty()}
    c.live = {"poolparty"}
    c.slow["tokens_info"] = [{"instrument_admin": "edel", "instrument_id": "EDELx", "instrument_symbol": "EDELx"}]
    cx_states = [SimpleNamespace(symbol_a="CC", symbol_b="USDCx", token_a=CC, token_b=Instrument("x", "USDCx"))]
    books: dict = {}

    class NoPools:  # OneSwap is not part of this test
        async def pools(self):
            return []

    c.optional["oneswap"] = NoPools()
    c.live.add("oneswap")

    async def go():
        await c._reserve_venues(books, cx_states)
        await c.http.aclose()

    asyncio.run(go())
    assert set(books) == {"EDELX"}  # the named pool is quoted
    rows = c._unpriced_json(Decimal("0.1177"))
    assert rows == [{"venue": "poolparty", "pair": "CC/481871d4", "reason": collect.UNNAMED,
                     "tvl_usd": round(2 * 228412 * 0.1177, 2)}]
    # volume: the CC legs only, at our CC price; non-CC pools and the EDELx leg are left out
    _, total = c._pp_volume_usd(Decimal("0.1177"))
    assert abs(total - 12940 * 0.1177) < 1e-6
