"""Offline tests for the Cantex adapter.

The SDK client is mocked at its own method boundary (AsyncMock) and fed the
SDK's real response objects, constructed via their ``_from_raw`` classmethods
from the raw JSON shapes the Cantex API returns.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from cantex_sdk import (
    AccountInfo,
    CantexAPIError,
    CantexAuthError,
    InstrumentId,
    PoolsInfo,
    SwapExecutedEvent,
    SwapQuote,
)

from canton_toolkit import (
    Balance,
    CantexAdapter,
    Instrument,
    Pool,
    Quote,
    SwapResult,
    VenueAuthError,
    VenueRequestError,
)
from canton_toolkit.venues.cantex import DEFAULT_BASE_URL

# --- Public test key vectors (RFC 8032 Ed25519 / standard secp256k1); not secrets.
ED25519_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
SECP256K1_HEX = "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35"

POOL_RAW = {
    "contract_id": "pool-abc",
    "token_a_instrument_id": "USDCx",
    "token_a_instrument_admin": "usdc-rep::1220def",
    "token_b_instrument_id": "Amulet",
    "token_b_instrument_admin": "DSO::1220abc",
}

ACCOUNT_INFO_RAW = {
    "party_id": {"address": "Cantex::1220xyz"},
    "user_id": "uid-info",
    "tokens": [
        {
            "instrument_id": "USDCx",
            "instrument_admin": "usdc-rep::1220def",
            "instrument_name": "USD Coin",
            "instrument_symbol": "USDCx",
            "balances": {"unlocked_amount": "500.0", "locked_amount": "50.0"},
            "pending_deposit_transfers": [],
            "pending_withdraw_transfers": [],
            "expired_allocations": [],
        },
    ],
}

QUOTE_RAW = {
    "estimated_time_seconds": "4.72",
    "fees": {
        "amount_admin": "0.0000500000",
        "amount_liquidity": "0.0004500000",
        "fee_percentage": "0.0005000000",
        "instrument_admin": "DSO::1220abc",
        "instrument_id": "Amulet",
        "network_fee": {
            "amount": "0.1000",
            "instrument_admin": "DSO::1220abc",
            "instrument_id": "Amulet",
        },
    },
    "pool_price_after_trade": "0.1548223373",
    "pool_price_before_trade": "0.1548228128",
    "pool_size": {
        "amount": "1301596.7091451541",
        "instrument_admin": "DSO::1220abc",
        "instrument_id": "Amulet",
    },
    "pools": [
        {
            "buy": {"amount": "51.66", "instrument_admin": "DSO::1220abc", "instrument_id": "Amulet"},
            "contract_id": "pool-contract-001",
            "fees": {
                "admin": {"amount": "0.0003", "instrument_admin": "usdc-rep::1220def", "instrument_id": "USDCx"},
                "fee_percentage": "0.0005000000",
                "liquidity": {"amount": "0.0033", "instrument_admin": "usdc-rep::1220def", "instrument_id": "USDCx"},
            },
            "pool_id": "2820898830768735469",
            "pool_price_after": "0.1426125549",
            "pool_price_before": "0.1426033624",
            "prices": {
                "pool_after": "7.0120053636",
                "pool_before": "7.0124573744",
                "slippage": "0.0000322297",
                "trade": "7.0087252497",
                "trade_no_fees": "7.0122313653",
            },
            "sell": {"amount": "7.37", "instrument_admin": "usdc-rep::1220def", "instrument_id": "USDCx"},
            "size": {"amount": "3205755.13", "instrument_admin": "DSO::1220abc", "instrument_id": "Amulet"},
            "trade_price": "0.1426792982",
            "trade_price_no_fees": "0.1426079586",
        },
    ],
    "prices": {
        "pool_after": "0.1548223373",
        "pool_before": "0.1548228128",
        "slippage": "0.0000015358",
        "trade": "0.1548225750",
        "trade_no_fees": "0.1548226500",
    },
    "returned": {
        "amount": "0.1547451638",
        "instrument_admin": "usdc-rep::1220def",
        "instrument_id": "USDCx",
    },
    "sent": {
        "buy_instrument_admin": "usdc-rep::1220def",
        "buy_instrument_id": "USDCx",
        "sell_amount": "1",
        "sell_instrument_admin": "DSO::1220abc",
        "sell_instrument_id": "Amulet",
    },
    "slippage": "0.0000015358",
    "trade_price": "0.1548225750",
}

SWAP_EXECUTED_RAW = {
    "category": "trading",
    "created_at": "2026-04-07T05:58:04.353447+00:00",
    "data": {
        "ledger_created_at": "2026-04-07T05:57:58.790361+00:00",
        "swap_details": {
            "admin_fee_amount": "0.0005538314",
            "input_amount": "11.0766285914",
            "input_instrument_id": {"admin": "usdc-rep::1220abc", "id": "USDCx"},
            "liquidity_fee_amount": "0.0049844829",
            "output_amount": "74.8590517011",
            "output_instrument_id": {"admin": "DSO::1220def", "id": "Amulet"},
        },
        "ticker": {"market": "CC-USDC", "price": "0.14815", "ts": 1775541451587},
    },
    "event_id": "bc324667-38fd-4fb8-8bdc-998472fbc802",
    "severity": "info",
    "source": "ledger",
    "type": "Pool.SwapExecuted",
    "user_id": "uid-1",
    "wallet_address": "Cantex::1220wallet",
}


def _adapter_with_mock() -> tuple[CantexAdapter, AsyncMock]:
    client = AsyncMock()
    return CantexAdapter(client=client), client


# --- Read path ------------------------------------------------------------


async def test_pools_happy_path():
    adapter, client = _adapter_with_mock()
    client.get_pool_info.return_value = PoolsInfo._from_raw({"pools": [POOL_RAW]})

    pools = await adapter.pools()

    assert pools == [
        Pool(
            contract_id="pool-abc",
            token_a=Instrument(admin="usdc-rep::1220def", id="USDCx"),
            token_b=Instrument(admin="DSO::1220abc", id="Amulet"),
        )
    ]
    client.get_pool_info.assert_awaited_once()


async def test_balances_happy_path():
    adapter, client = _adapter_with_mock()
    client.get_account_info.return_value = AccountInfo._from_raw(ACCOUNT_INFO_RAW)

    balances = await adapter.balances()

    assert balances == [
        Balance(
            instrument=Instrument(admin="usdc-rep::1220def", id="USDCx"),
            symbol="USDCx",
            name="USD Coin",
            unlocked=Decimal("500.0"),
            locked=Decimal("50.0"),
        )
    ]


async def test_quote_happy_path():
    adapter, client = _adapter_with_mock()
    client.get_swap_quote.return_value = SwapQuote._from_raw(QUOTE_RAW)

    sell = Instrument(admin="DSO::1220abc", id="Amulet")
    buy = Instrument(admin="usdc-rep::1220def", id="USDCx")
    quote = await adapter.quote(Decimal("1"), sell, buy)

    assert quote == Quote(
        sell_amount=Decimal("1"),
        sell_instrument=sell,
        buy_instrument=buy,
        returned_amount=Decimal("0.1547451638"),
        returned_instrument=buy,
        trade_price=Decimal("0.1548225750"),
        slippage=Decimal("0.0000015358"),
        fee_percentage=Decimal("0.0005000000"),
        estimated_time_seconds=Decimal("4.72"),
    )
    # SDK is called with converted InstrumentId objects, not the core Instrument.
    args = client.get_swap_quote.await_args.args
    assert args[0] == Decimal("1")
    assert args[1] == InstrumentId(admin="DSO::1220abc", id="Amulet")
    assert args[2] == InstrumentId(admin="usdc-rep::1220def", id="USDCx")


# --- Write path -----------------------------------------------------------


async def test_swap_happy_path():
    adapter, client = _adapter_with_mock()
    client.swap_and_confirm.return_value = SwapExecutedEvent._from_raw(SWAP_EXECUTED_RAW)

    sell = Instrument(admin="usdc-rep::1220abc", id="USDCx")
    buy = Instrument(admin="DSO::1220def", id="Amulet")
    result = await adapter.swap(Decimal("11"), sell, buy, max_network_fee=Decimal("0.5"))

    assert result == SwapResult(
        input_amount=Decimal("11.0766285914"),
        input_instrument=sell,
        output_amount=Decimal("74.8590517011"),
        output_instrument=buy,
        price=Decimal("0.14815"),
        admin_fee_amount=Decimal("0.0005538314"),
        liquidity_fee_amount=Decimal("0.0049844829"),
        market="CC-USDC",
    )
    assert client.swap_and_confirm.await_args.kwargs["max_network_fee"] == Decimal("0.5")


async def test_transfer_passthrough():
    adapter, client = _adapter_with_mock()
    client.transfer.return_value = {"status": "submitted"}

    instrument = Instrument(admin="DSO::1220abc", id="Amulet")
    result = await adapter.transfer(Decimal("1.5"), instrument, "Cantex::1220rcv", memo="hi")

    assert result == {"status": "submitted"}
    args = client.transfer.await_args.args
    assert args[0] == Decimal("1.5")
    assert args[1] == InstrumentId(admin="DSO::1220abc", id="Amulet")
    assert args[2] == "Cantex::1220rcv"
    assert args[3] == "hi"


# --- Lifecycle ------------------------------------------------------------


async def test_connect_authenticates():
    adapter, client = _adapter_with_mock()

    await adapter.connect()

    client.authenticate.assert_awaited_once()


async def test_context_manager_connects_and_closes():
    client = AsyncMock()
    client.get_pool_info.return_value = PoolsInfo._from_raw({"pools": []})

    async with CantexAdapter(client=client) as adapter:
        await adapter.pools()

    client.authenticate.assert_awaited_once()
    client.close.assert_awaited_once()


# --- Config / env wiring --------------------------------------------------


def test_env_wiring_builds_sdk_with_keys_and_default_url(monkeypatch):
    monkeypatch.setenv("CANTEX_OPERATOR_KEY", ED25519_HEX)
    monkeypatch.setenv("CANTEX_TRADING_KEY", SECP256K1_HEX)
    monkeypatch.delenv("CANTEX_BASE_URL", raising=False)

    adapter = CantexAdapter()

    assert adapter._sdk.base_url == DEFAULT_BASE_URL
    # Operator signer loaded from env: its public key is derivable.
    assert isinstance(adapter._sdk.public_key, str)
    # Intent signer loaded because CANTEX_TRADING_KEY was set.
    assert adapter._sdk._intent_signer is not None


def test_env_wiring_respects_base_url_override(monkeypatch):
    monkeypatch.setenv("CANTEX_OPERATOR_KEY", ED25519_HEX)
    monkeypatch.setenv("CANTEX_BASE_URL", "https://api.cantex.io")
    monkeypatch.delenv("CANTEX_TRADING_KEY", raising=False)

    adapter = CantexAdapter()

    assert adapter._sdk.base_url == "https://api.cantex.io"
    # No trading key in env -> no intent signer.
    assert adapter._sdk._intent_signer is None


def test_missing_operator_key_raises_auth_error(monkeypatch):
    monkeypatch.delenv("CANTEX_OPERATOR_KEY", raising=False)

    with pytest.raises(VenueAuthError):
        CantexAdapter()


# --- Error mapping --------------------------------------------------------


async def test_auth_error_is_mapped():
    adapter, client = _adapter_with_mock()
    client.get_pool_info.side_effect = CantexAuthError(401, "Unauthorized")

    with pytest.raises(VenueAuthError):
        await adapter.pools()


async def test_api_error_is_mapped_to_request_error():
    adapter, client = _adapter_with_mock()
    client.get_account_info.side_effect = CantexAPIError(400, "Bad Request")

    with pytest.raises(VenueRequestError):
        await adapter.balances()
