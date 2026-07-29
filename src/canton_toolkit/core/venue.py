"""Venue-agnostic adapter interfaces and error hierarchy.

A ``VenueAdapter`` exposes a small async read/write trading surface for a spot
venue. A ``MarketDataAdapter`` exposes read-only market state for venues whose
shape is a symbol and a book rather than a pool and a swap. Concrete adapters
wrap a venue SDK and translate its exceptions into ``VenueError`` subclasses so
callers never depend on venue-specific error types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from .models import (
    Balance,
    FundingRate,
    Instrument,
    Market,
    OrderBook,
    Pool,
    Quote,
    SwapResult,
    Ticker,
    Trade,
)


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


class MarketDataAdapter(ABC):
    """Read-only market state for a single venue.

    Deliberately separate from ``VenueAdapter``: a derivatives venue has no
    pools and nothing to swap, and its public data needs no credentials.
    A venue may implement both.
    """

    async def __aenter__(self) -> MarketDataAdapter:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.close()

    @abstractmethod
    async def connect(self) -> None:
        """Open the session and verify the venue is reachable."""

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying session and connections."""

    @abstractmethod
    async def markets(self) -> list[Market]:
        """List tradable symbols and their order constraints."""

    @abstractmethod
    async def tickers(self, symbol: str | None = None) -> list[Ticker]:
        """Current price, funding and top of book, for one symbol or all."""

    @abstractmethod
    async def order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        """A depth snapshot for one symbol."""

    @abstractmethod
    async def recent_trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        """Recent public prints, most recent first."""

    @abstractmethod
    async def funding_history(self, symbol: str, limit: int = 50) -> list[FundingRate]:
        """Settled funding rates, most recent first."""
