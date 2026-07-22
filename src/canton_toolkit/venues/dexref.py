"""Reference-DEX venue adapter (Bit Dynamics Canton DEX reference implementation).

Talks HTTP to the DEX operator backend (``services/operator-backend`` in the
reference repo); there is no vendor SDK. Endpoint and payload shapes were
verified against the running backend and its source (``http/index.ts``,
``pool/index.ts``, ``http/validate.ts``) on 2026-07-18.

Venue-shape notes (vs Cantex):

- Quotes return only ``outputAmount``; trade price, slippage, and fee are
  computed here from the pool's live reserves and ``feeBps``.
- Holdings are per-contract rows; ``balances()`` aggregates them by
  instrument, splitting on the ``locked`` flag.
- Swaps settle against a wallet-authored allocation. That authorization step
  is venue-specific and pluggable here via ``AllocationAuthorizer``; the
  in-memory demo backend accepts a synthetic cid
  (``DemoAllocationAuthorizer``).
- There is no venue-level transfer; ``transfer()`` always raises.
- There is no network fee; ``max_network_fee`` is accepted and ignored.
"""

from __future__ import annotations

import os
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

import httpx

from ..core.models import Balance, Instrument, Pool, Quote, SwapResult
from ..core.venue import VenueAdapter, VenueAuthError, VenueRequestError

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
BASE_URL_ENV = "DEXREF_BASE_URL"
TRADER_PARTY_ENV = "DEXREF_TRADER_PARTY"

_TEN_DP = Decimal("1e-10")


def _fmt(amount: Decimal) -> str:
    """Format a Decimal as the plain <=10-dp string the backend validates."""
    return f"{amount.quantize(_TEN_DP, rounding=ROUND_DOWN):f}"


class AllocationAuthorizer(Protocol):
    """Authors the swapper-side allocation a pool swap settles against.

    ``needs_spec`` controls whether the adapter first calls
    ``POST /v1/pools/swap/request`` and passes the returned allocation spec to
    ``authorize``; a real wallet flow needs the spec, the demo backend does
    not (and does not implement the request choice).
    """

    needs_spec: bool

    async def authorize(self, spec: dict | None) -> str:
        """Return the allocation contract id to settle against."""
        ...


class DemoAllocationAuthorizer:
    """Synthetic allocation for the in-memory demo backend, which never
    inspects the swapper allocation cid."""

    needs_spec = False

    def __init__(self, cid: str = "#demo-alloc:0") -> None:
        self._cid = cid

    async def authorize(self, spec: dict | None) -> str:
        return self._cid


class DexRefAdapter(VenueAdapter):
    """Venue adapter backed by the reference DEX operator-backend HTTP API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        trader_party: str | None = None,
        allocation_authorizer: AllocationAuthorizer | None = None,
        max_slippage: Decimal = Decimal("0.005"),
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        trader = trader_party or os.getenv(TRADER_PARTY_ENV)
        if not trader:
            raise VenueAuthError(
                f"trader party required: pass trader_party or set {TRADER_PARTY_ENV}"
            )
        self._trader = trader
        self._authorizer = allocation_authorizer
        self._max_slippage = max_slippage
        self._client = client
        self._context: dict | None = None
        self._raw_pools: list[dict] = []

    # === plumbing ========================================================

    async def _request(self, method: str, path: str, json: dict | None = None) -> object:
        if self._client is None:
            raise VenueRequestError("adapter is not connected; call connect() first")
        try:
            resp = await self._client.request(method, f"{self._base_url}{path}", json=json)
        except httpx.HTTPError as exc:
            raise VenueRequestError(f"{method} {path}: {exc}") from exc
        if resp.status_code == 401:
            raise VenueAuthError(self._error_text(resp))
        if resp.status_code >= 400:
            raise VenueRequestError(f"{method} {path}: {self._error_text(resp)}")
        if resp.status_code == 204:
            return {}
        return resp.json()

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            return f"{body.get('code', resp.status_code)}: {body.get('error', '')}"
        except ValueError:
            return f"HTTP {resp.status_code}"

    # === VenueAdapter ====================================================

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        self._context = await self._request("GET", "/v1/context")  # type: ignore[assignment]
        status = await self._request("GET", "/v1/status")
        if not (isinstance(status, dict) and status.get("synced")):
            raise VenueRequestError(f"backend is not synced: {status}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def pools(self) -> list[Pool]:
        raw = await self._request("GET", "/v1/pools")
        if not isinstance(raw, list):
            raise VenueRequestError(f"unexpected /v1/pools response: {raw!r}")
        self._raw_pools = raw
        return [
            Pool(
                contract_id=p["contractId"],
                token_a=Instrument(admin=p["admin"], id=p["baseInstrumentId"]),
                token_b=Instrument(admin=p["admin"], id=p["quoteInstrumentId"]),
            )
            for p in raw
        ]

    async def _resolve_pool(self, sell: Instrument, buy: Instrument) -> dict:
        """Find the active pool trading the (sell, buy) pair, either direction."""
        for attempt in range(2):
            for p in self._raw_pools:
                legs = {p["baseInstrumentId"], p["quoteInstrumentId"]}
                if {sell.id, buy.id} == legs and p.get("status") == "Active":
                    return p
            if attempt == 0:
                await self.pools()
        raise VenueRequestError(f"no active pool for {sell.id}/{buy.id}")

    async def quote(
        self,
        sell_amount: Decimal,
        sell_instrument: Instrument,
        buy_instrument: Instrument,
    ) -> Quote:
        pool = await self._resolve_pool(sell_instrument, buy_instrument)
        body = {
            "poolId": pool["contractId"],  # accepts the contract id, NOT pool["poolId"]
            "inputInstrumentId": sell_instrument.id,
            "inputAmount": _fmt(sell_amount),
        }
        raw = await self._request("POST", "/v1/swaps/quote", json=body)
        out = Decimal(str(raw["outputAmount"]))  # type: ignore[index]

        sell_is_base = sell_instrument.id == pool["baseInstrumentId"]
        reserve_in = Decimal(pool["reserves"]["baseAmount" if sell_is_base else "quoteAmount"])
        reserve_out = Decimal(pool["reserves"]["quoteAmount" if sell_is_base else "baseAmount"])
        trade_price = out / sell_amount
        spot_price = reserve_out / reserve_in
        slippage = Decimal(0) if spot_price == 0 else Decimal(1) - trade_price / spot_price
        return Quote(
            sell_amount=sell_amount,
            sell_instrument=sell_instrument,
            buy_instrument=buy_instrument,
            returned_amount=out,
            returned_instrument=buy_instrument,
            trade_price=trade_price,
            slippage=slippage,
            fee_percentage=Decimal(pool["feeBps"]) / Decimal(10000),
            estimated_time_seconds=Decimal(0),
        )

    async def balances(self) -> list[Balance]:
        raw = await self._request("GET", f"/v1/holdings?owner={self._trader}")
        if not isinstance(raw, list):
            raise VenueRequestError(f"unexpected /v1/holdings response: {raw!r}")
        totals: dict[Instrument, dict[str, Decimal]] = {}
        for h in raw:
            instrument = Instrument(admin=h["admin"], id=h["instrumentId"])
            bucket = totals.setdefault(instrument, {"unlocked": Decimal(0), "locked": Decimal(0)})
            bucket["locked" if h.get("locked") else "unlocked"] += Decimal(h["amount"])
        return [
            Balance(
                instrument=instrument,
                symbol=instrument.id,  # no instrument-metadata surface on this venue
                name=instrument.id,
                unlocked=sums["unlocked"],
                locked=sums["locked"],
            )
            for instrument, sums in totals.items()
        ]

    async def swap(
        self,
        sell_amount: Decimal,
        sell_instrument: Instrument,
        buy_instrument: Instrument,
        *,
        max_network_fee: Decimal | None = None,  # no network fee on this venue
    ) -> SwapResult:
        if self._authorizer is None:
            raise VenueRequestError(
                "swap requires an allocation_authorizer (the swapper-side allocation "
                "is wallet-authored on this venue); use DemoAllocationAuthorizer "
                "against the demo backend"
            )
        pool = await self._resolve_pool(sell_instrument, buy_instrument)
        quote = await self.quote(sell_amount, sell_instrument, buy_instrument)
        min_output = quote.returned_amount * (Decimal(1) - self._max_slippage)

        spec: dict | None = None
        if self._authorizer.needs_spec:
            spec = await self._request(  # type: ignore[assignment]
                "POST",
                "/v1/pools/swap/request",
                json={
                    "poolCid": pool["contractId"],
                    "swapper": self._trader,
                    "inputInstrumentId": sell_instrument.id,
                    "inputAmount": _fmt(sell_amount),
                },
            )
        allocation_cid = await self._authorizer.authorize(spec)

        operator = (self._context or {}).get("operator")
        raw = await self._request(
            "POST",
            "/v1/pools/swap",
            json={
                "poolCid": pool["contractId"],
                "swapperAccount": {
                    "owner": self._trader,
                    "provider": operator,
                    "id": self._trader,
                },
                "inputInstrumentId": sell_instrument.id,
                "inputAmount": _fmt(sell_amount),
                "minOutputAmount": _fmt(min_output),
                "swapperAllocationCid": allocation_cid,
            },
        )
        if not isinstance(raw, dict) or "amountOut" not in raw:
            raise VenueRequestError(f"unexpected /v1/pools/swap response: {raw!r}")
        out = Decimal(str(raw["amountOut"]))
        fee_fraction = Decimal(pool["feeBps"]) / Decimal(10000)
        return SwapResult(
            input_amount=sell_amount,
            input_instrument=sell_instrument,
            output_amount=out,
            output_instrument=buy_instrument,
            price=out / sell_amount,
            admin_fee_amount=Decimal(0),  # pool fee accrues entirely to LPs
            liquidity_fee_amount=sell_amount * fee_fraction,
            market=pool["poolId"],
        )

    async def transfer(
        self,
        amount: Decimal,
        instrument: Instrument,
        receiver: str,
        memo: str = "",
    ) -> dict:
        raise VenueRequestError(
            "the reference DEX has no venue-level transfer; move holdings through "
            "a token-standard wallet instead"
        )
