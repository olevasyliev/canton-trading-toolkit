"""Offline tests for the reference-DEX adapter.

HTTP is mocked with ``httpx.MockTransport`` fed the raw JSON shapes the
operator backend returns (captured from a live demo-mode run on 2026-07-18).
Requests are recorded so tests can assert the exact bodies sent.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from canton_toolkit import (
    DemoAllocationAuthorizer,
    DexRefAdapter,
    Instrument,
    VenueAuthError,
    VenueRequestError,
)

BTC = Instrument(admin="admin-demo", id="BTC")
USDC = Instrument(admin="admin-demo", id="USDC")

CONTEXT = {
    "operator": "operator-demo",
    "lpRegistrar": "lp-registrar-demo",
    "admin": "admin-demo",
    "network": "canton:devnet",
}
STATUS = {"network": "canton:devnet", "slot": 3, "synced": True}
POOL_ROW = {
    "contractId": "#2:0",
    "poolId": "BTC-USDC",
    "poolStateCid": "#3:0",
    "admin": "admin-demo",
    "baseInstrumentId": "BTC",
    "quoteInstrumentId": "USDC",
    "feeBps": 30,
    "status": "Active",
    "reserves": {"baseAmount": "10.0000000000", "quoteAmount": "200000.0000000000"},
}
HOLDINGS = [
    {
        "admin": "admin-demo",
        "owner": "trader-demo",
        "instrumentId": "USDC",
        "amount": "5000.0000000000",
        "locked": False,
        "contractId": "#11:0",
    },
    {
        "admin": "admin-demo",
        "owner": "trader-demo",
        "instrumentId": "BTC",
        "amount": "0.2500000000",
        "locked": False,
        "contractId": "#12:0",
    },
    {
        "admin": "admin-demo",
        "owner": "trader-demo",
        "instrumentId": "BTC",
        "amount": "0.1000000000",
        "locked": True,
        "contractId": "#13:0",
    },
]
# 0.5 BTC into 10/200000 at 30 bps, exact constant-product output.
QUOTE_OUT = "9496.5947516312"
SWAP_RESULT = {
    "poolStateCid": "#14:0",
    "inputSliceCid": "#15:0",
    "boundaryOutputSliceCid": "#16:0",
    "outputSlicesConsumed": 1,
    "amountOut": QUOTE_OUT,
    "settleResult": {"allocationSettleResults": [], "meta": {}},
}


def make_adapter(
    routes: dict[tuple[str, str], object],
    recorded: list[httpx.Request],
    **kwargs: object,
) -> DexRefAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"error": "not found", "code": "not_found"})
        payload = routes[key]
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs.setdefault("trader_party", "trader-demo")
    return DexRefAdapter(base_url="http://test", client=client, **kwargs)  # type: ignore[arg-type]


BASE_ROUTES: dict[tuple[str, str], object] = {
    ("GET", "/v1/context"): CONTEXT,
    ("GET", "/v1/status"): STATUS,
    ("GET", "/v1/pools"): [POOL_ROW],
    ("GET", "/v1/holdings"): HOLDINGS,
    ("POST", "/v1/swaps/quote"): {"outputAmount": QUOTE_OUT},
    ("POST", "/v1/pools/swap"): SWAP_RESULT,
}


async def test_pools_maps_rows() -> None:
    adapter = make_adapter(dict(BASE_ROUTES), [])
    async with adapter:
        pools = await adapter.pools()
    assert len(pools) == 1
    assert pools[0].contract_id == "#2:0"
    assert pools[0].token_a == BTC
    assert pools[0].token_b == USDC


async def test_balances_aggregates_holdings_by_instrument_and_lock() -> None:
    adapter = make_adapter(dict(BASE_ROUTES), [])
    async with adapter:
        balances = {b.instrument.id: b for b in await adapter.balances()}
    assert balances["USDC"].unlocked == Decimal("5000")
    assert balances["BTC"].unlocked == Decimal("0.25")
    assert balances["BTC"].locked == Decimal("0.1")
    assert balances["BTC"].symbol == "BTC"  # no metadata surface: symbol = id


async def test_quote_sends_contract_id_and_computes_derived_fields() -> None:
    recorded: list[httpx.Request] = []
    adapter = make_adapter(dict(BASE_ROUTES), recorded)
    async with adapter:
        quote = await adapter.quote(Decimal("0.5"), BTC, USDC)
    quote_req = next(r for r in recorded if r.url.path == "/v1/swaps/quote")
    body = json.loads(quote_req.content)
    # The request field is named poolId but must carry the pool CONTRACT id.
    assert body["poolId"] == "#2:0"
    assert body["inputAmount"] == "0.5000000000"
    assert quote.returned_amount == Decimal(QUOTE_OUT)
    assert quote.trade_price == Decimal(QUOTE_OUT) / Decimal("0.5")
    assert quote.fee_percentage == Decimal("0.003")
    # spot 20000, realized 18993.19: ~5% slippage on this oversized trade
    assert Decimal("0.05") < quote.slippage < Decimal("0.051")


async def test_swap_demo_flow_skips_request_step_and_maps_result() -> None:
    recorded: list[httpx.Request] = []
    adapter = make_adapter(
        dict(BASE_ROUTES), recorded, allocation_authorizer=DemoAllocationAuthorizer()
    )
    async with adapter:
        result = await adapter.swap(Decimal("0.5"), BTC, USDC)
    paths = [r.url.path for r in recorded]
    assert "/v1/pools/swap/request" not in paths  # demo authorizer needs no spec
    swap_req = next(r for r in recorded if r.url.path == "/v1/pools/swap")
    body = json.loads(swap_req.content)
    assert body["swapperAllocationCid"] == "#demo-alloc:0"
    assert body["swapperAccount"]["owner"] == "trader-demo"
    assert body["swapperAccount"]["provider"] == "operator-demo"
    # min output = quote * (1 - default 0.5% slippage guard)
    expected_min = Decimal(QUOTE_OUT) * Decimal("0.995")
    assert Decimal(body["minOutputAmount"]) == expected_min.quantize(Decimal("1e-10"))
    assert result.output_amount == Decimal(QUOTE_OUT)
    assert result.market == "BTC-USDC"
    assert result.admin_fee_amount == Decimal(0)
    assert result.liquidity_fee_amount == Decimal("0.5") * Decimal("0.003")


async def test_swap_without_authorizer_raises() -> None:
    adapter = make_adapter(dict(BASE_ROUTES), [])
    async with adapter:
        with pytest.raises(VenueRequestError, match="allocation_authorizer"):
            await adapter.swap(Decimal("0.1"), BTC, USDC)


async def test_transfer_is_unsupported() -> None:
    adapter = make_adapter(dict(BASE_ROUTES), [])
    async with adapter:
        with pytest.raises(VenueRequestError, match="no venue-level transfer"):
            await adapter.transfer(Decimal("1"), USDC, "someone::1220abcd")


async def test_unauthorized_maps_to_auth_error() -> None:
    routes = dict(BASE_ROUTES)
    routes[("POST", "/v1/pools/swap")] = httpx.Response(
        401, json={"error": "state-changing routes require ...", "code": "unauthorized"}
    )
    adapter = make_adapter(routes, [], allocation_authorizer=DemoAllocationAuthorizer())
    async with adapter:
        with pytest.raises(VenueAuthError, match="unauthorized"):
            await adapter.swap(Decimal("0.1"), BTC, USDC)


async def test_connect_rejects_unsynced_backend() -> None:
    routes = dict(BASE_ROUTES)
    routes[("GET", "/v1/status")] = {"network": "canton:devnet", "synced": False}
    adapter = make_adapter(routes, [])
    with pytest.raises(VenueRequestError, match="not synced"):
        await adapter.connect()
    await adapter.close()


async def test_no_pool_for_pair_raises() -> None:
    adapter = make_adapter(dict(BASE_ROUTES), [])
    eth = Instrument(admin="admin-demo", id="ETH")
    async with adapter:
        with pytest.raises(VenueRequestError, match="no active pool for ETH/USDC"):
            await adapter.quote(Decimal("1"), eth, USDC)
