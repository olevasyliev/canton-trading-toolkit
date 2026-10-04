"""Venue-agnostic adapter interfaces and error hierarchy.

Three interfaces, because Canton already has three venue shapes:

- ``PoolDataAdapter`` — a constant-product AMM you can read and price against:
  pools and quotes, no credentials.
- ``VenueAdapter`` — a ``PoolDataAdapter`` you can also trade: balances,
  swaps, transfers.
- ``MarketDataAdapter`` — read-only market state for venues whose shape is a
  symbol and a book rather than a pool and a swap.
- ``OrderTradingAdapter`` — a ``MarketDataAdapter`` you can also trade: limit
  orders, cancels, open orders and balances on an order-book venue.

Concrete adapters wrap a venue SDK or HTTP API and translate its exceptions
into ``VenueError`` subclasses so callers never depend on venue-specific error
types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from .models import (
    Balance,
    FundingRate,
    Instrument,
    Market,
    Order,
    OrderBook,
    Pool,
    Quote,
    Side,
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


class TradingDisabledError(VenueError):
    """A trading call on an adapter that was not explicitly opened for trading."""


class PoolDataAdapter(ABC):
    """Read-only pools and pricing for a single AMM venue.

    Separate from ``VenueAdapter`` because a venue can be fully readable
    without being tradable by us: Tradecraft publishes an open API but settles
    orders through Daml choices that need our own validator node.
    """

    async def __aenter__(self) -> PoolDataAdapter:
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


class VenueAdapter(PoolDataAdapter):
    """Async trading interface for a single venue: an AMM we can also trade."""

    async def __aenter__(self) -> VenueAdapter:
        await self.connect()
        return self

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


class OrderTradingAdapter(MarketDataAdapter):
    """Trade an order-book venue: limit orders, cancels, open orders, balances.

    Implementations must refuse every state-changing call unless trading was
    enabled explicitly at construction, so a market-data consumer holding the
    same credentials can never place an order by accident.
    """

    @abstractmethod
    async def balances(self) -> list[Balance]:
        """Trading balances held at the venue."""

    @abstractmethod
    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        """Orders currently resting, for one symbol or all."""

    @abstractmethod
    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        *,
        post_only: bool = False,
        expires_at: datetime | None = None,
    ) -> Order:
        """Place a limit order. ``post_only`` rejects it rather than let it take liquidity."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel one order; True when the venue confirms."""

    @abstractmethod
    async def cancel_all(self, symbol: str | None = None) -> int:
        """Cancel every resting order, for one symbol or all; returns how many."""
