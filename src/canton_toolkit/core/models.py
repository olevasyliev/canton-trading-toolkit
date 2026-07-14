"""Venue-agnostic domain models.

Every field mirrors a real field on the Cantex SDK response models; see
SOURCES.md for the exact ``_sdk.py`` line references. Amounts are ``Decimal``
because the SDK carries all monetary values as ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Instrument:
    """A Canton instrument (token), keyed by its admin party and instrument id."""

    admin: str
    id: str


@dataclass(frozen=True)
class Pool:
    """A single liquidity pool between two instruments."""

    contract_id: str
    token_a: Instrument
    token_b: Instrument


@dataclass(frozen=True)
class Balance:
    """A single token's balance within an account."""

    instrument: Instrument
    symbol: str
    name: str
    unlocked: Decimal
    locked: Decimal


@dataclass(frozen=True)
class Quote:
    """A price quote for swapping one instrument for another."""

    sell_amount: Decimal
    sell_instrument: Instrument
    buy_instrument: Instrument
    returned_amount: Decimal
    returned_instrument: Instrument
    trade_price: Decimal
    slippage: Decimal
    fee_percentage: Decimal
    estimated_time_seconds: Decimal


@dataclass(frozen=True)
class SwapResult:
    """A confirmed, on-ledger swap execution."""

    input_amount: Decimal
    input_instrument: Instrument
    output_amount: Decimal
    output_instrument: Instrument
    price: Decimal
    admin_fee_amount: Decimal
    liquidity_fee_amount: Decimal
    market: str
