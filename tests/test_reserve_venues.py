"""Offline tests for the OneSwap and Pool Party reserve readers.

Fixtures are trimmed excerpts of live MainNet responses captured on 2026-10-03:
OneSwap ``/api/rt/pools`` and Pool Party ``/tvl`` and ``/volume``, including a
Pool Party pool whose first token id is a dashed UUID and an empty pool.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from canton_toolkit import OneSwapPublicData, PoolPartyPublicData, constant_product_output

DSO = "DSO::1220b1431ef217342db44d516bb9befde802be7d8899637d290895fa58880f19accc"
UUID = "481871d4-ca56-42a8-b2d3-4b7d28742946"

OS_POOLS = [
    {"id": "rt-l4xayw", "visible": True, "swapsEnabled": True, "feeBps": 30,
     "assetX": {"id": "Amulet", "admin": DSO, "symbol": "CC"},
     "assetY": {"id": "USDCx", "admin": "decentralized-usdc-interchain-rep::12208115", "symbol": "USDCx"},
     "reserveX": 153710.31554707175, "reserveY": 18748.479226507807,
     "accounting": {"reserveX": "153710.31554707175", "reserveY": "18748.479226507808"}},
    {"id": "rt-hidden", "visible": False, "swapsEnabled": True, "feeBps": 30,
     "assetX": {"id": "Amulet", "admin": DSO, "symbol": "CC"},
     "assetY": {"id": "X", "admin": "x::1", "symbol": "X"}, "reserveX": 1, "reserveY": 1},
]
PP_TVL = {"asOf": "2026-10-03T11:31:40.954Z", "pools": {
    "Amulet-USDCx": {"Amulet": "52281.2288543148", "USDCx": "6377.7386340239"},
    f"{UUID}-Amulet": {"Amulet": "232450.7509486895", UUID: "31975.5966496274"},
    "USDC.B-cETH": {"USDC.B": "0", "cETH": "0"},
}}
PP_VOLUME = {"period": "24h", "perPool": {"Amulet-USDCx": {"volume": {"Amulet": "4828.0684", "USDCx": "610"}}}}


def _client(routes: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, body in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=body)
        return httpx.Response(404, text="not found")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_oneswap_reads_exact_reserves_and_skips_hidden_pools() -> None:
    async with OneSwapPublicData(client=_client({"/api/rt/pools": OS_POOLS})) as os_:
        (pool,) = await os_.pools()
    assert pool.token_a.admin == DSO and pool.token_a.id == "Amulet"
    assert pool.reserve_b == Decimal("18748.479226507808")  # the string, not the float
    assert pool.fee == Decimal("0.003")


async def test_pool_party_splits_dashed_ids_and_skips_empty_pools() -> None:
    async with PoolPartyPublicData(client=_client({"/tvl": PP_TVL})) as pp:
        pools = {p.pool_id: p for p in await pp.pools()}
    assert set(pools) == {"Amulet-USDCx", f"{UUID}-Amulet"}
    uuid_pool = pools[f"{UUID}-Amulet"]
    assert (uuid_pool.token_a.id, uuid_pool.token_b.id) == (UUID, "Amulet")
    assert uuid_pool.token_a.admin == ""  # Pool Party publishes no issuer


async def test_pool_party_volume_is_in_token_units() -> None:
    async with PoolPartyPublicData(client=_client({"/tvl": PP_TVL, "/volume": PP_VOLUME})) as pp:
        vol = await pp.volume()
    assert vol["Amulet-USDCx"] == {"Amulet": Decimal("4828.0684"), "USDCx": Decimal(610)}


def test_constant_product_output_takes_the_fee_on_input() -> None:
    out = constant_product_output(Decimal(1000), Decimal(1000), Decimal(10), Decimal("0.003"))
    assert out == Decimal(1000) * Decimal("9.97") / Decimal("1009.97")


def test_pool_output_both_directions() -> None:
    from canton_toolkit import ReservePool
    from canton_toolkit.core.models import Instrument

    p = ReservePool("oneswap", "p", "CC", "USDCx", Instrument(DSO, "Amulet"), Instrument("u", "USDCx"),
                    Decimal(1000), Decimal(100), Decimal(0), "test")
    assert p.output(True, Decimal(10)) == pytest.approx(Decimal(100) * 10 / 1010)
    assert p.output(False, Decimal(1)) == pytest.approx(Decimal(1000) * 1 / 101)
