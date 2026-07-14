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
