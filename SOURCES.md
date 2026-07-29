# SDK integration point provenance

Every point where `canton_toolkit` touches `cantex_sdk` is listed here with the
source location it was verified against. Line numbers refer to the SDK at
commit cloned from `https://github.com/caviarnine/cantex_sdk`, file
`src/cantex_sdk/_sdk.py` (unless noted otherwise).

## Configuration & credentials

| Integration point | Adapter site | SDK source |
|---|---|---|
| Env var `CANTEX_OPERATOR_KEY` (operator Ed25519 hex) | `cantex.OPERATOR_KEY_ENV` | `examples/example.py:11,50` |
| Env var `CANTEX_TRADING_KEY` (intent secp256k1 hex) | `cantex.TRADING_KEY_ENV` | `examples/example.py:11,54` |
| Env var `CANTEX_BASE_URL` | `cantex.BASE_URL_ENV` | `examples/example.py:48` |
| Default base URL `https://api.testnet.cantex.io` | `cantex.DEFAULT_BASE_URL` | `_sdk.py:1286` (`CantexSDK.__init__` default) |
| `OperatorKeySigner.from_env(name)` | `CantexAdapter.__init__` | `_sdk.py:573-579` (defined on `BaseSigner`), class `_sdk.py:646` |
| `IntentTradingKeySigner.from_env(name)` | `CantexAdapter.__init__` | `_sdk.py:573-579`, class `_sdk.py:714` |
| `CantexSDK(operator, intent, base_url=..., api_key_path=...)` | `CantexAdapter.__init__` | `_sdk.py:1281-1305` |
| `sdk.public_key` (env-wiring test) | test only | `_sdk.py:1362-1365` |
| `sdk._intent_signer` (env-wiring test) | test only | `_sdk.py:1284,1294` |

## Lifecycle

| Integration point | Adapter site | SDK source |
|---|---|---|
| `await sdk.authenticate()` (challenge-response, Ed25519) | `CantexAdapter.connect` | `_sdk.py:1470-1515` |
| `await sdk.close()` | `CantexAdapter.close` | `_sdk.py:1325-1332` |

## Read path

| Integration point | Adapter site | SDK source |
|---|---|---|
| `await sdk.get_pool_info() -> PoolsInfo` | `CantexAdapter.pools` | `_sdk.py:1665-1668` |
| `await sdk.get_account_info() -> AccountInfo` | `CantexAdapter.balances` | `_sdk.py:1655-1658` |
| `await sdk.get_swap_quote(sell_amount, sell_id, buy_id) -> SwapQuote` | `CantexAdapter.quote` | `_sdk.py:1670-1688` |

## Write path

| Integration point | Adapter site | SDK source |
|---|---|---|
| `await sdk.swap_and_confirm(sell_amount, sell_id, buy_id, *, max_network_fee) -> SwapExecutedEvent` | `CantexAdapter.swap` | `_sdk.py:1846-1918` |
| `await sdk.transfer(amount, instrument_id, receiver, memo) -> dict` | `CantexAdapter.transfer` | `_sdk.py:1706-1728` |

## Domain model field shapes (mirrored by `core/models.py`)

| Core model | Field(s) | SDK model → source |
|---|---|---|
| `Instrument(admin, id)` | `admin`, `id` | `InstrumentId` `_sdk.py:104-113` |
| `Pool(contract_id, token_a, token_b)` | all | `Pool` `_sdk.py:257-277`; list via `PoolsInfo.pools` `_sdk.py:280-297` |
| `Balance(instrument, symbol, name, unlocked, locked)` | `symbol`←`instrument_symbol`, `name`←`instrument_name`, `unlocked`←`unlocked_amount`, `locked`←`locked_amount` | `TokenBalance` `_sdk.py:121-158`; list via `AccountInfo.tokens` `_sdk.py:161-201` |
| `Quote.returned_amount / returned_instrument` | ←`SwapQuote.returned` (`QuoteLeg`) | `_sdk.py:422`, `QuoteLeg` `_sdk.py:300-315` |
| `Quote.trade_price / slippage` | ←`SwapQuote.prices.trade / .slippage` | `QuotePrices` `_sdk.py:318-336` |
| `Quote.fee_percentage` | ←`SwapQuote.fees.fee_percentage` | `QuoteFees` `_sdk.py:389-410` |
| `Quote.sell_amount / sell_instrument / buy_instrument / estimated_time_seconds` | direct | `SwapQuote` `_sdk.py:413-500` |
| `SwapResult(input_amount, input_instrument, output_amount, output_instrument, price, admin_fee_amount, liquidity_fee_amount, market)` | all | `SwapExecutedEvent` `_sdk.py:887-925` |

## Exceptions (mapped in `venue.py` hierarchy)

| SDK exception | Mapped to | SDK source |
|---|---|---|
| `CantexAuthError` (401/403) | `VenueAuthError` | `_sdk.py:84-89` |
| `CantexAPIError`, `CantexTimeoutError`, `CantexError` | `VenueRequestError` | `_sdk.py:71-89` |

## Notes / judgment calls

- `swap()` maps onto the SDK's `swap_and_confirm` (not the fire-and-forget
  `swap`) so the adapter can return a typed `SwapResult`; the fire-and-forget
  `swap` (`_sdk.py:1816-1844`) returns an untyped `dict`.
- `transfer()` returns the SDK's raw submit `dict`; the SDK provides no typed
  model for transfer results, so no fields are invented.
- Default base URL follows the SDK class default (testnet, `_sdk.py:1286`).
  The example script defaults to mainnet (`api.cantex.io`, `examples/example.py:48`);
  the adapter deliberately defaults to testnet per the toolkit's scope.

---

# Reference DEX adapter (`venues/dexref.py`)

Upstream: [srikanth-bitdynamics/Canton-Dex-Reference-Implementation](https://github.com/srikanth-bitdynamics/Canton-Dex-Reference-Implementation),
`services/operator-backend` (no vendor SDK; the adapter speaks the HTTP API
directly). Verified against a live demo-mode backend + source on 2026-07-18.

## Endpoints consumed

| Adapter call | Endpoint | Upstream source |
|---|---|---|
| `connect()` | `GET /v1/context`, `GET /v1/status` | `http/index.ts` |
| `pools()` | `GET /v1/pools` | `http/index.ts`, shapes `types.ts` |
| `quote()` | `POST /v1/swaps/quote` | `http/index.ts:941`; NB request field `poolId` takes the pool **contract id** |
| `balances()` | `GET /v1/holdings?owner=` | `http/index.ts`; per-contract rows, aggregated client-side |
| `swap()` | `POST /v1/pools/swap/request` (only when the authorizer needs a spec) + `POST /v1/pools/swap` | `pool/index.ts:37-84` (inputs), `:345-454` (flow) |
| `transfer()` | none — venue has no transfer surface; always raises | — |

## Model mapping / judgment calls

- `Instrument(admin, id)` ← pool `admin` + `baseInstrumentId`/`quoteInstrumentId`
  (holdings rows carry `admin` + `instrumentId`). No instrument-metadata
  endpoint exists, so `Balance.symbol = Balance.name = instrument id`.
- `Quote`: venue returns only `outputAmount`. `trade_price` = out/in;
  `slippage` = 1 − realized/spot with spot from pool `reserves`;
  `fee_percentage` = `feeBps`/10000 (fraction, matching the Cantex
  convention); `estimated_time_seconds` = 0 (not provided).
- `SwapResult`: `amountOut` from the `PoolRules_Swap` result;
  `admin_fee_amount` = 0 and `liquidity_fee_amount` = input × fee fraction
  (pool fee accrues entirely to LPs, `dev-server.ts:115`); `market` =
  pool `poolId` (e.g. `BTC-USDC`).
- Amount strings are ≤10-dp fixed-point per `DECIMAL_RE`
  (`http/validate.ts:25`); party ids must be canonical
  `hint::hexfingerprint{8,}` unless the server runs with
  `DEX_ALLOW_BARE_PARTIES=1` (`http/validate.ts:38-49`).
- The swapper-side allocation is wallet-authored on this venue
  (`pool/index.ts:69-84`); the adapter delegates it to a pluggable
  `AllocationAuthorizer`. The in-memory demo mock ignores the allocation cid
  (`dev-server.ts:94-160`), so `DemoAllocationAuthorizer` supplies a
  synthetic one; a testnet authorizer will drive the wallet relay
  (`http/index.ts:655`, needs `DEX_DEV_WALLET_RELAY=1` + a real ledger).
- Errors: HTTP 401 → `VenueAuthError`; other 4xx/5xx and transport errors →
  `VenueRequestError`, carrying the server's `{code, error}` envelope.

---

# Ekiden adapter (`venues/ekiden.py`) — read-only market data

Perpetuals venue on Canton. No SDK is used: the gateway (`ekiden-gateway`,
Rust/Axum) is called directly over HTTP. Shapes below were captured from live
responses on the Canton testnet gateway `https://api.cnt.ekiden.fi` on
2026-07-27, **not** from the published OpenAPI document — see the caveat at the
bottom. Staging is `https://api.canton.ekiden.fi`; public WebSocket streams
exist at `wss://api.cnt.ekiden.fi/ws/public` and are not consumed yet.

Public market data needs no credentials. Trading requires an Ed25519-signed
session (`AUTHORIZE|<timestamp_ms>|<nonce>`) plus API keys, and a root-whitelist
surface exists (`/api/v1/authorize/whitelist/*`); none of that is implemented
here, so this adapter cannot place an order.

## Endpoints consumed

| Adapter method | Endpoint | Notes |
|---|---|---|
| `connect()` | `GET /api/v1/info` | asserts `runtime_manifest_phase == "ready"` |
| `markets()` | `GET /api/v1/market/instruments-info` | no params; `{"list": [...]}` envelope |
| `tickers()` | `GET /api/v1/market/tickers` | optional `symbol`; returns all markets when omitted |
| `order_book()` | `GET /api/v1/market/orderbook` | `symbol` + `depth`; **`depth` ∈ {10, 50, 200} only** |
| `recent_trades()` | `GET /api/v1/market/recent-trade` | `symbol`, `limit`; rows use single-letter keys |
| `funding_history()` | `GET /api/v1/market/funding/history` | `symbol`, `limit` |

## Model mapping / judgment calls

- Perps have no pools and nothing to swap, so this implements the separate
  `MarketDataAdapter` interface rather than `VenueAdapter`, against new models
  (`Market`, `OrderBook`, `BookLevel`, `Trade`, `Ticker`, `FundingRate`).
- `Market` carries the constraints an order must satisfy: `tick_size` from
  `price_filter`, `min_order_size`/`size_step`/`min_notional` from
  `lot_size_filter`, `max_leverage` from `leverage_filter`.
- Book rows are bare `[price, size]` pairs under `result.b` / `result.a`;
  trade rows key on `i`/`s`/`S`/`v`/`p`/`seq`/`T`. `Trade.side` is the
  aggressor's side.
- All amounts are decimal strings parsed exactly; funding rates arrive with up
  to 28 significant digits, so nothing may pass through a float.
- Timestamps are epoch milliseconds, surfaced as timezone-aware UTC datetimes.
- `OrderBook.mid_price` / `.spread` return `None` on a one-sided book, which is
  a real state here (the CC book had zero offers on 2026-07-27 11:22 UTC after
  a run of buys).

## Venue quirks the adapter absorbs

- **`depth` is an undeclared enum.** Only 10/50/200 are accepted; the OpenAPI
  parameter carries no schema. The adapter rejects other values before the
  request.
- **Query-string rejections come back as plain text**, not the JSON error
  envelope every other route uses, so the error mapper handles both.
- **An empty book side is reported as `"0"`/`"0"` in the ticker's
  `best_ask_price`/`best_ask_size`**, not as null. Passed through, a caller
  reads a best ask of zero as a real price. `_top()` maps non-positive
  price-or-size to `None`.
- **The published `api-reference/openapi.json` does not parse** — trailing
  comma after the staging server entry — so client generation from it fails at
  step one.
- `GET /api/v1/info` reports the Canton validator under a field named
  `aptos_network`, left over from their migration off Aptos.
