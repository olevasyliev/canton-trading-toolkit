"""Venue-agnostic adapter interface and error hierarchy.

A ``VenueAdapter`` exposes a small async read/write trading surface. Concrete
adapters wrap a venue SDK and translate its exceptions into ``VenueError``
subclasses so callers never depend on venue-specific error types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from .models import Balance, Instrument, Pool, Quote, SwapResult


class VenueError(Exception):
    """Base class for all venue adapter errors."""


class VenueAuthError(VenueError):
    """Authentication or credential failure."""


class VenueRequestError(VenueError):
    """A request to the venue failed (bad request, timeout, transport error)."""


class VenueAdapter(ABC):
    """Async trading interface for a single venue."""

    async def __aenter__(self) -> VenueAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.close()

    @abstractmethod
    async def connect(self) -> None:
        """Establish an authenticated session with the venue."""

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying session and connections."""

    @abstractmethod
    async def pools(self) -> list[Pool]:
        """List the venue's liquidity pools."""

    @abstractmethod
    async def quote(
        self,
        sell_amount: Decimal,
        sell_instrument: Instrument,
        buy_instrument: Instrument,
    ) -> Quote:
        """Price a swap without executing it."""

    @abstractmethod
    async def balances(self) -> list[Balance]:
        """Return the authenticated account's token balances."""

    @abstractmethod
    async def swap(
        self,
        sell_amount: Decimal,
        sell_instrument: Instrument,
        buy_instrument: Instrument,
        *,
        max_network_fee: Decimal | None = None,
    ) -> SwapResult:
        """Execute a swap and return its confirmed on-ledger result."""

    @abstractmethod
    async def transfer(
        self,
        amount: Decimal,
        instrument: Instrument,
        receiver: str,
        memo: str = "",
    ) -> dict:
        """Transfer tokens to another account (raw venue submit response)."""
