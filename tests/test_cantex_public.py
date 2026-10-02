"""Offline tests for CantexPublicData: parsing and local pricing."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from canton_toolkit import CantexPublicData, Instrument
from canton_toolkit.core.venue import VenueRequestError
from canton_toolkit.venues.cantex_public import swap_output

CC = {"admin": "DSO::1220", "id": "Amulet", "symbol": "CC"}
USD = {"admin": "usdc::1220", "id": "USDCx", "symbol": "USDCx"}

STATE = {"data": {"pools": [{
    "contract_id": "00ab", "pool_id": "1", "symbol": "CC-USDCX", "fee_rate": "0.0005000000",
    "price": "0.1225276182", "reserve_a": "1613381.8277082216", "reserve_b": "197683.8326573317",
    "token_a_instrument": CC, "token_b_instrument": USD, "tvl_cc": "3226763.6554164432",
}]}}


def _adapter(routes: dict) -> CantexPublicData:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, text="nope")

    return CantexPublicData(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_pool_states_parse_decimals_and_instruments():
    states = await _adapter({"/v1/public/pools/state": STATE}).pool_states()
    s = states[0]
    assert s.symbol == "CC-USDCX" and s.symbol_a == "CC"
    assert s.token_a == Instrument("DSO::1220", "Amulet")
    assert s.reserve_b == Decimal("197683.8326573317") and s.fee == Decimal("0.0005")


async def test_quote_is_constant_product_with_fee_on_input():
    c = _adapter({"/v1/public/pools/state": STATE})
    cc, usd = Instrument("DSO::1220", "Amulet"), Instrument("usdc::1220", "USDCx")
    q = await c.quote(Decimal(1000), cc, usd)
    expected = swap_output(Decimal("1613381.8277082216"), Decimal("197683.8326573317"),
                           Decimal(1000), Decimal("0.0005"))
    assert q.returned_amount == expected
    assert q.fee_percentage == Decimal("0.0005")
    assert q.network_fee is None  # not in /pools/state; documented as excluded
    back = await c.quote(q.returned_amount, usd, cc)
    assert back.returned_amount < Decimal(1000)  # a round trip pays the fee twice


async def test_quote_rejects_unknown_pairs_and_bad_amounts():
    c = _adapter({"/v1/public/pools/state": STATE})
    cc = Instrument("DSO::1220", "Amulet")
    with pytest.raises(VenueRequestError):
        await c.quote(Decimal(1), cc, Instrument("x::1", "NOPE"))
    with pytest.raises(VenueRequestError):
        await c.quote(Decimal(0), cc, Instrument("usdc::1220", "USDCx"))


async def test_http_errors_become_venue_errors():
    with pytest.raises(VenueRequestError):
        await _adapter({}).volume()
