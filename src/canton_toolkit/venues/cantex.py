"""Cantex venue adapter.

Wraps the ``cantex_sdk.CantexSDK`` async client (challenge-response Ed25519
operator auth + secp256k1 intent-trading key) and maps its domain models and
exceptions onto the venue-agnostic core. Credentials are read from the SDK's
own environment variables; a pre-built client can be injected for tests.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from decimal import Decimal
from typing import Iterator

from cantex_sdk import (
    AccountInfo,
    CantexAuthError,
    CantexError,
    CantexSDK,
    InstrumentId,
    IntentTradingKeySigner,
    OperatorKeySigner,
    Pool as SdkPool,
    SwapExecutedEvent,
    SwapQuote,
    TokenBalance,
)

from ..core.models import Balance, Instrument, Pool, Quote, SwapResult
from ..core.venue import VenueAdapter, VenueAuthError, VenueRequestError

DEFAULT_BASE_URL = "https://api.testnet.cantex.io"
OPERATOR_KEY_ENV = "CANTEX_OPERATOR_KEY"
TRADING_KEY_ENV = "CANTEX_TRADING_KEY"
BASE_URL_ENV = "CANTEX_BASE_URL"


@contextmanager
def _map_sdk_errors() -> Iterator[None]:
    try:
        yield
    except CantexAuthError as exc:
        raise VenueAuthError(str(exc)) from exc
    except CantexError as exc:
        raise VenueRequestError(str(exc)) from exc


def _to_instrument(sdk_id: InstrumentId) -> Instrument:
    return Instrument(admin=sdk_id.admin, id=sdk_id.id)


def _to_instrument_id(instrument: Instrument) -> InstrumentId:
    return InstrumentId(admin=instrument.admin, id=instrument.id)


def _to_pool(pool: SdkPool) -> Pool:
    return Pool(
        contract_id=pool.contract_id,
        token_a=_to_instrument(pool.token_a),
        token_b=_to_instrument(pool.token_b),
    )


def _to_balance(token: TokenBalance) -> Balance:
    return Balance(
        instrument=_to_instrument(token.instrument),
        symbol=token.instrument_symbol,
        name=token.instrument_name,
        unlocked=token.unlocked_amount,
        locked=token.locked_amount,
    )


def _to_quote(quote: SwapQuote) -> Quote:
    return Quote(
        sell_amount=quote.sell_amount,
        sell_instrument=_to_instrument(quote.sell_instrument),
        buy_instrument=_to_instrument(quote.buy_instrument),
        returned_amount=quote.returned.amount,
        returned_instrument=_to_instrument(quote.returned.instrument),
        trade_price=quote.prices.trade,
        slippage=quote.prices.slippage,
        fee_percentage=quote.fees.fee_percentage,
        estimated_time_seconds=quote.estimated_time_seconds,
    )


def _to_swap_result(event: SwapExecutedEvent) -> SwapResult:
    return SwapResult(
        input_amount=event.input_amount,
        input_instrument=_to_instrument(event.input_instrument),
        output_amount=event.output_amount,
        output_instrument=_to_instrument(event.output_instrument),
        price=event.price,
        admin_fee_amount=event.admin_fee_amount,
        liquidity_fee_amount=event.liquidity_fee_amount,
        market=event.market,
    )


class CantexAdapter(VenueAdapter):
    """Venue adapter backed by the Cantex SDK."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: CantexSDK | None = None,
    ) -> None:
        if client is not None:
            self._sdk = client
            return
        try:
            operator = OperatorKeySigner.from_env(OPERATOR_KEY_ENV)
        except ValueError as exc:
            raise VenueAuthError(str(exc)) from exc
        intent = (
            IntentTradingKeySigner.from_env(TRADING_KEY_ENV)
            if os.getenv(TRADING_KEY_ENV)
            else None
        )
        resolved_url = base_url or os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL
        self._sdk = CantexSDK(
            operator,
            intent,
            base_url=resolved_url,
            api_key_path=None,
        )

    async def connect(self) -> None:
        with _map_sdk_errors():
            await self._sdk.authenticate()

    async def close(self) -> None:
        await self._sdk.close()

    async def pools(self) -> list[Pool]:
        with _map_sdk_errors():
            info = await self._sdk.get_pool_info()
        return [_to_pool(p) for p in info.pools]

    async def quote(
        self,
        sell_amount: Decimal,
        sell_instrument: Instrument,
        buy_instrument: Instrument,
    ) -> Quote:
        with _map_sdk_errors():
            result = await self._sdk.get_swap_quote(
                sell_amount,
                _to_instrument_id(sell_instrument),
                _to_instrument_id(buy_instrument),
            )
        return _to_quote(result)

    async def balances(self) -> list[Balance]:
        with _map_sdk_errors():
            info: AccountInfo = await self._sdk.get_account_info()
        return [_to_balance(t) for t in info.tokens]

    async def swap(
        self,
        sell_amount: Decimal,
        sell_instrument: Instrument,
        buy_instrument: Instrument,
        *,
        max_network_fee: Decimal | None = None,
    ) -> SwapResult:
        # TODO(grant M2): idempotency key so a retried swap submits at most once.
        with _map_sdk_errors():
            event = await self._sdk.swap_and_confirm(
                sell_amount,
                _to_instrument_id(sell_instrument),
                _to_instrument_id(buy_instrument),
                max_network_fee=max_network_fee,
            )
        return _to_swap_result(event)

    async def transfer(
        self,
        amount: Decimal,
        instrument: Instrument,
        receiver: str,
        memo: str = "",
    ) -> dict:
        # TODO(grant M2): idempotency key so a retried transfer submits at most once.
        with _map_sdk_errors():
            return await self._sdk.transfer(
                amount,
                _to_instrument_id(instrument),
                receiver,
                memo,
            )
