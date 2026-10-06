"""Offline tests for the Tradecraft spot-AMM adapter.

HTTP is mocked with ``httpx.MockTransport``. Every fixture below is a verbatim
excerpt of a live response captured from ``https://api.tradecraft.fi/v1`` on
2026-08-11, including the JSON numbers as they arrive on the wire and the
canonical-order answers the venue gives to reversed paths.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from cantonvenues import Instrument, TradecraftAdapter, VenueRequestError, swap_output

HEALTH = {"status": "ok"}

POOLS = {
    "pools": [
        {
            "token1": "CC",
            "token2": "USDCx",
            "lp_token_name": "TC CC/USDCx LP",
            "token1_holdings": 6967855.125699786,
            "token2_holdings": 673730.7556480888,
            "total_lp_tokens": 2025781.7962135246,
            "lp_fee_percent": 0.2,
            "operator_fee_percent": 0.1,
            "yield24h": 0.005265372375770516,
        },
        {
            "token1": "CC",
            "token2": "CBTC",
            "lp_token_name": "TC CC/CBTC LP",
            "token1_holdings": 782901.7608761458,
            "token2_holdings": 1.1427830326,
            "total_lp_tokens": 25406.9535561622,
            "lp_fee_percent": 0.2,
            "operator_fee_percent": 0.1,
            "yield24h": 0.0,
        },
        {
            "token1": "SBC",
            "token2": "cETH",
            "lp_token_name": "TC SBC/cETH LP",
            "token1_holdings": 100.0,
            "token2_holdings": 10.0,
            "total_lp_tokens": 31.6227766017,
            "lp_fee_percent": 0.0,
            "operator_fee_percent": 0.2,
            "yield24h": 0.0,
        },
    ]
}

INSPECT_CC_USDCX = {
    "total_lp_token_supply": 2025781.7962135246,
    "token_a_id": "CC",
    "token_a_holdings": 6967855.125699786,
    "token_b_id": "USDCx",
    "token_b_holdings": 673730.7556480888,
    "k": 4694458299084.125,
    "unclaimed_operator_fees": 9141.9196361846,
    "updated_at": "2026-08-10T21:08:24Z",
}

TOKEN_A = {
    "instrument_id": {
        "admin": "DSO::1220b1431ef217342db44d516bb9befde802be7d8899637d290895fa58880f19accc",
        "id": "Amulet",
    }
}
TOKEN_B = {
    "instrument_id": {
        "admin": (
            "decentralized-usdc-interchain-rep::"
            "12208115f1e168dd7e792320be9c4ca720c751a02a3053c7606e1c1cd3dad9bf60ef"
        ),
        "id": "USDCx",
    }
}

# 1,000 CC into the CC/USDCx pool, measured 2026-08-11
QUOTE_FIXED_INPUT = {"user_gets": 96.38760050838262}
QUOTE_FIXED_OUTPUT = {"user_gives": 10388.734818762192}
FEE_AMOUNT = {"fee_amount": 0.002, "operator_fee_amount": 0.001}
# asked as /quoteLPDeposit/USDCx/CC, answered in the pool's own order: the
# 1000 belongs to CC, not to the USDCx that led the path
LP_DEPOSIT_REVERSED_PATH = {
    "lp_tokens_to_mint": 290.7324793165923,
    "instrument_1_to_deposit": 1000,
    "instrument_2_to_deposit": 96.69126919174077,
}
YIELD_HISTORY = {
    "pool_id": "TC CC/USDCx LP",
    "window": "month",
    "history": [
        {"timestamp": "2026-07-12T12:00:00Z", "apy": 0.0275211526},
        {"timestamp": "2026-07-13T12:00:00Z", "apy": 0.0},
    ],
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    routes = {
        "/v1/health": HEALTH,
        "/v1/pools": POOLS,
        "/v1/inspect/CC/USDCx": INSPECT_CC_USDCX,
        "/v1/inspect/USDCx/CC": INSPECT_CC_USDCX,  # canonical order either way
        "/v1/tokenA/CC/USDCx": TOKEN_A,
        "/v1/tokenB/CC/USDCx": TOKEN_B,
        "/v1/tokenA/CC/CBTC": TOKEN_A,
        "/v1/tokenB/CC/CBTC": {"instrument_id": {"admin": "cbtc-admin::12", "id": "CBTC"}},
        "/v1/tokenA/SBC/cETH": {"instrument_id": {"admin": "sbc-admin::12", "id": "SBC"}},
        "/v1/tokenB/SBC/cETH": {"instrument_id": {"admin": "ceth-admin::12", "id": "cETH"}},
        "/v1/quoteForFixedInput/CC/USDCx": QUOTE_FIXED_INPUT,
        "/v1/quoteForFixedOutput/CC/USDCx": QUOTE_FIXED_OUTPUT,
        "/v1/feeAmount/CC/USDCx": FEE_AMOUNT,
        "/v1/quoteLPDeposit/USDCx/CC": LP_DEPOSIT_REVERSED_PATH,
        "/v1/yield_history/CC/USDCx": YIELD_HISTORY,
    }
    if path in routes:
        return httpx.Response(200, json=routes[path])
    if path == "/v1/nope":
        return httpx.Response(404, text="404 page not found", headers={"content-type": "text/plain"})
    return httpx.Response(400, json={"error": f"No AMM with id {path}"})


@pytest.fixture
def adapter() -> TradecraftAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return TradecraftAdapter(base_url="https://api.tradecraft.fi/v1", client=client)


@pytest.mark.asyncio
async def test_connect_requires_healthy_venue(adapter: TradecraftAdapter) -> None:
    async with adapter:
        states = await adapter.pool_states()
    assert [s.amm_id for s in states] == ["TC CC/USDCx LP", "TC CC/CBTC LP", "TC SBC/cETH LP"]


@pytest.mark.asyncio
async def test_fee_constants_are_normalised_to_fractions(adapter: TradecraftAdapter) -> None:
    """/pools ships percents, /feeAmount ships fractions, for the same numbers."""
    async with adapter:
        state = (await adapter.pool_states())[0]
        lp, operator = await adapter.fees("CC", "USDCx")
    assert state.lp_fee == Decimal("0.002")
    assert state.operator_fee == Decimal("0.001")
    assert (lp, operator) == (Decimal("0.002"), Decimal("0.001"))
    assert state.total_fee == Decimal("0.003")


@pytest.mark.asyncio
async def test_realized_fee_is_half_the_total_on_each_leg(adapter: TradecraftAdapter) -> None:
    async with adapter:
        state = (await adapter.pool_states())[0]
    # (1 - 0.0015) ** 2 = 0.99700225, so a shade under the published 0.3%
    assert state.realized_fee == Decimal("0.00299775")


@pytest.mark.asyncio
async def test_local_pricing_reproduces_the_venue_quote(adapter: TradecraftAdapter) -> None:
    """The whole point of the fee model: our number is the venue's number."""
    async with adapter:
        state = (await adapter.pool_states())[0]
        quote = await adapter.quote_symbols(Decimal("1000"), "CC", "USDCx")
        local = adapter.quote_locally(state, Decimal("1000"), "CC", "USDCx")
    assert quote.returned_amount == Decimal("96.38760050838262")
    assert abs(local - quote.returned_amount) / quote.returned_amount < Decimal("1e-15")


def test_swap_output_matches_a_measured_mainnet_quote() -> None:
    got = swap_output(
        Decimal("6967855.125699786"),
        Decimal("673730.7556480888"),
        Decimal("1000"),
        Decimal("0.003"),
    )
    assert abs(got - Decimal("96.38760050838262")) < Decimal("1e-13")


@pytest.mark.asyncio
async def test_pool_state_is_oriented_by_the_venue_not_the_request(
    adapter: TradecraftAdapter,
) -> None:
    """/inspect answers in canonical order however the path is written."""
    async with adapter:
        state = await adapter.inspect("USDCx", "CC")
    assert (state.token_a, state.token_b) == ("CC", "USDCx")
    assert state.reserve_a == Decimal("6967855.125699786")
    assert state.price("CC", "USDCx") == Decimal("673730.7556480888") / Decimal(
        "6967855.125699786"
    )


@pytest.mark.asyncio
async def test_liquidity_quote_is_labelled_by_symbol(adapter: TradecraftAdapter) -> None:
    """The reversed path must not hand the caller an inverted pair."""
    async with adapter:
        lq = await adapter.liquidity_deposit_quote(
            "USDCx", "CC", Decimal("1000"), Decimal("1000")
        )
    assert (lq.token_a, lq.amount_a) == ("CC", Decimal("1000"))
    assert (lq.token_b, lq.amount_b) == ("USDCx", Decimal("96.69126919174077"))


@pytest.mark.asyncio
async def test_amounts_keep_the_digits_the_venue_sent(adapter: TradecraftAdapter) -> None:
    """Parsed with parse_float=Decimal, not through a float a second time."""
    async with adapter:
        state = (await adapter.pool_states())[0]
    assert str(state.reserve_a) == "6967855.125699786"
    assert str(state.lp_supply) == "2025781.7962135246"


@pytest.mark.asyncio
async def test_non_positive_amounts_are_refused_before_the_request(
    adapter: TradecraftAdapter,
) -> None:
    """The venue answers 200 with a quote of zero; that must not reach a caller."""
    async with adapter:
        for amount in (Decimal("0"), Decimal("-1000")):
            with pytest.raises(VenueRequestError, match="must be positive"):
                await adapter.quote_symbols(amount, "CC", "USDCx")


@pytest.mark.asyncio
async def test_history_windows_differ_per_route(adapter: TradecraftAdapter) -> None:
    async with adapter:
        rows = await adapter.yield_history("CC", "USDCx", "month")
        assert len(rows) == 2 and rows[0][1] == Decimal("0.0275211526")
        # the same window is not served by the volume route
        with pytest.raises(VenueRequestError, match="volume_history accepts"):
            await adapter.volume_history("CC", "USDCx", "month")


@pytest.mark.asyncio
async def test_quote_resolves_instruments_and_carries_them_through(
    adapter: TradecraftAdapter,
) -> None:
    async with adapter:
        pools = await adapter.pools()
        cc = pools[0].token_a
        usdcx = pools[0].token_b
        quote = await adapter.quote(Decimal("1000"), cc, usdcx)
    assert cc == Instrument(
        admin="DSO::1220b1431ef217342db44d516bb9befde802be7d8899637d290895fa58880f19accc",
        id="Amulet",
    )
    assert quote.buy_instrument == usdcx
    assert quote.trade_price == Decimal("96.38760050838262") / Decimal("1000")


@pytest.mark.asyncio
async def test_plain_text_errors_do_not_crash_the_client(adapter: TradecraftAdapter) -> None:
    async with adapter:
        with pytest.raises(VenueRequestError, match="404 page not found"):
            await adapter._get("/nope")


@pytest.mark.asyncio
async def test_unknown_pair_is_reported_not_guessed(adapter: TradecraftAdapter) -> None:
    async with adapter:
        with pytest.raises(VenueRequestError, match="no pool for"):
            await adapter.quote_symbols(Decimal("1"), "CC", "NOSUCHTOKEN")


def test_fixtures_are_the_wire_format() -> None:
    """Guard the fixtures themselves: these are JSON numbers, not strings."""
    row = json.loads(json.dumps(POOLS))["pools"][0]
    assert isinstance(row["token1_holdings"], float)
    assert isinstance(row["lp_fee_percent"], float)
