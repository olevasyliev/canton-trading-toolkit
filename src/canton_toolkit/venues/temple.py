"""Temple venue adapter — read-only market data (Canton spot order books).

Temple runs off-chain central limit order books with settlement on Canton and
is the largest venue on the network by volume. Two surfaces:

- **Settled volume, no key.** ``GET /api/exchange/settled_volume`` returns USD
  volume and trade counts per market for any RFC 3339 window. Verified live on
  MainNet on 2026-10-03.
- **Market data, key required.** Ticker, order book and recent trades under
  ``/api/v1/market/*`` need an ``X-API-Key`` header, issued per account in the
  Temple app (Settings > API Keys). Routes, parameters and payload shapes are
  taken from Temple's own SDK, ``@temple-digital-group/temple-canton-js`` 2.1.10
  (``dist/api/index.js``, ``dist/api/types.d.ts``, ``src/config/index.js``);
  **not yet verified against live responses** because we hold no key. Without a
  key these methods raise ``VenueAuthError`` instead of calling out.

Venue-shape notes:

- CC is written ``Amulet`` on the wire for market data (the SDK rewrites
  ``CC`` to ``Amulet`` in every symbol); settled volume reports ``CC/USDCx``.
  Symbols are passed through here; ``wire_symbol`` does the rewrite.
- There are no perpetuals and no funding: ``funding_history`` returns nothing.
- Mainnet REST is ``https://api.templedigitalgroup.com``; testnet is
  ``https://api-testnet.templedigitalgroup.com`` (SDK config).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from ..core.models import BookLevel, FundingRate, Market, OrderBook, Side, Ticker, Trade
from ..core.venue import MarketDataAdapter, VenueAuthError, VenueRequestError

MAINNET_BASE_URL = "https://api.templedigitalgroup.com"
TESTNET_BASE_URL = "https://api-testnet.templedigitalgroup.com"
API_KEY_ENV = "TEMPLE_API_KEY"


def _dec(value: object) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def _ts(value: object) -> datetime:
    """Timestamps are RFC 3339 strings in the SDK types; epoch milliseconds are accepted too."""
    if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def wire_symbol(symbol: str) -> str:
    """``CC/USDCx`` -> ``Amulet/USDCx``, as Temple's market routes expect."""
    return re.sub(r"\bCC\b", "Amulet", symbol)


def _rfc3339(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TempleAdapter(MarketDataAdapter):
    """Read-only Temple market data: settled volume without a key, books and tickers with one."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = MAINNET_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key if api_key is not None else os.getenv(API_KEY_ENV)
        self._base = base_url.rstrip("/")
        self._client = client

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    # === plumbing ========================================================

    async def _get(self, path: str, params: dict | None = None, *, keyed: bool = True) -> object:
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        if keyed and not self._key:
            raise VenueAuthError(f"GET {path}: Temple market data needs an API key ({API_KEY_ENV})")
        headers = {"X-API-Key": self._key} if keyed else None
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._client.get(f"{self._base}{path}", params=clean or None, headers=headers)
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"GET {path}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise VenueAuthError(f"GET {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        if resp.status_code >= 400:
            raise VenueRequestError(f"GET {path}: HTTP {resp.status_code}: {resp.text.strip()[:200]}")
        return resp.json()

    # === settled volume (no key) =========================================

    async def settled_volume(self, hours: int = 24, end: datetime | None = None) -> dict:
        """USD volume and trade count per market over the last ``hours``."""
        end = end or datetime.now(UTC)
        raw = await self._get("/api/exchange/settled_volume",
                              {"start_time": _rfc3339(end - timedelta(hours=hours)), "end_time": _rfc3339(end)},
                              keyed=False)
        if not isinstance(raw, dict) or not isinstance(raw.get("markets"), list):
            raise VenueRequestError(f"unexpected settled_volume response: {raw!r}")
        return raw

    # === MarketDataAdapter ===============================================

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        await self.settled_volume(1)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def markets(self) -> list[Market]:
        """Markets that settled anything in the last 24 hours (the keyless list we have)."""
        vol = await self.settled_volume(24)
        out = []
        for mk in vol["markets"]:
            base, _, quote = mk["symbol"].partition("/")
            out.append(Market(symbol=mk["symbol"], base=base, quote=quote, is_trading=True,
                              tick_size=Decimal(0), min_order_size=Decimal(0), size_step=Decimal(0),
                              min_notional=Decimal(0), max_leverage=Decimal(1), funding_interval_minutes=0))
        return out

    async def tickers(self, symbol: str | None = None) -> list[Ticker]:
        raw = await self._get("/api/v1/market/ticker", {"symbol": wire_symbol(symbol) if symbol else None})
        rows = raw if isinstance(raw, list) else raw.get("data", raw) if isinstance(raw, dict) else []
        rows = rows if isinstance(rows, list) else [rows]
        return [
            Ticker(
                symbol=t["symbol"], last_price=_dec(t.get("last_price")), index_price=Decimal(0),
                mark_price=Decimal(0), open_interest=Decimal(0), volume_24h=_dec(t.get("volume_24h")),
                turnover_24h=Decimal(0), funding_rate=Decimal(0), next_funding_time=None,
                best_bid=self._top(t.get("bid_price")), best_ask=self._top(t.get("ask_price")),
            )
            for t in rows
        ]

    @staticmethod
    def _top(price: object) -> BookLevel | None:
        p = _dec(price)
        return BookLevel(price=p, size=Decimal(0)) if p > 0 else None

    async def order_book(self, symbol: str, depth: int = 50) -> OrderBook:
        raw = await self._get("/api/v1/market/orderbook", {"symbol": wire_symbol(symbol), "levels": depth})
        book = raw.get("data", raw) if isinstance(raw, dict) else None
        if not isinstance(book, dict) or "bids" not in book:
            raise VenueRequestError(f"unexpected orderbook response: {raw!r}")
        return OrderBook(
            symbol=book.get("symbol", symbol),
            timestamp=_ts(book["timestamp"]) if book.get("timestamp") else datetime.now(UTC),
            bids=tuple(BookLevel(price=_dec(lv["price"]), size=_dec(lv["quantity"])) for lv in book["bids"]),
            asks=tuple(BookLevel(price=_dec(lv["price"]), size=_dec(lv["quantity"])) for lv in book["asks"]),
        )

    async def recent_trades(self, symbol: str, limit: int = 50) -> list[Trade]:
        raw = await self._get("/api/v1/market/trades", {"symbol": wire_symbol(symbol), "limit": min(limit, 500)})
        rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
        trades = [
            Trade(trade_id=str(t["trade_id"]), symbol=t.get("symbol", symbol),
                  side=Side.SELL if str(t.get("side", "")).lower() == "sell" else Side.BUY,
                  price=_dec(t["price"]), size=_dec(t["quantity"]), timestamp=_ts(t["timestamp"]))
            for t in rows
        ]
        return sorted(trades, key=lambda t: t.timestamp, reverse=True)

    async def funding_history(self, symbol: str, limit: int = 50) -> list[FundingRate]:
        return []
