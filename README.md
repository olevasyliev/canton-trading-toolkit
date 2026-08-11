# canton_toolkit

An open-source algorithmic-trading toolkit for the [Canton Network](https://www.canton.network/).

The core (`canton_toolkit.core`) is venue-agnostic: a small typed domain model
and three adapter interfaces, all with a shared `VenueError` hierarchy.

- `PoolDataAdapter` — read and price an AMM: `Instrument`, `Pool`, `Quote`.
- `VenueAdapter` — a `PoolDataAdapter` you can also trade: `Balance`,
  `SwapResult`.
- `MarketDataAdapter` — read-only market state for venues shaped as a symbol
  and a book rather than a pool and a swap: `Market`, `OrderBook`, `Trade`,
  `Ticker`, `FundingRate`.

Four venues, three market structures, one client:

| Adapter | Venue | Shape | Surface |
|---|---|---|---|
| `CantexAdapter` | [Cantex](https://cantex.io/) | spot AMM | read + swap, live on mainnet |
| `TradecraftAdapter` | [Tradecraft](https://tradecraft.fi/) | spot AMM | read + pricing, live on mainnet |
| `DexRefAdapter` | [Canton DEX reference implementation](https://github.com/srikanth-bitdynamics/Canton-Dex-Reference-Implementation) | spot order book + RFQ | read + swap, live on its hosted testnet |
| `EkidenAdapter` | [Ekiden](https://ekiden.fi/) | perpetual futures | market data only |

`CantexAdapter` wraps the official
[`cantex_sdk`](https://github.com/caviarnine/cantex_sdk) async client, so auth,
signing and transport live in the SDK and the adapter only translates its models
and exceptions. The other three venues ship no Python SDK and are called directly
over HTTP. Every integration point is mapped to the source or the live response
it came from in [`SOURCES.md`](SOURCES.md).

## Install

`cantex_sdk` is not published on PyPI, so it is declared as a git dependency
and pulled directly from GitHub:

```bash
pip install "canton_toolkit @ git+https://github.com/<owner>/canton_toolkit"
# or, from a checkout:
pip install -e ".[dev]"
```

## Environment variables

Credentials are read only from the environment (never hard-coded). The names
are the SDK's own:

| Variable | Required | Description |
|---|---|---|
| `CANTEX_OPERATOR_KEY` | yes | Operator Ed25519 private key (hex). Used for challenge-response auth and ledger signing. |
| `CANTEX_TRADING_KEY` | for swaps | Intent-trading secp256k1 private key (hex). Required for `swap()`. |
| `CANTEX_BASE_URL` | no | API base URL. Defaults to `https://api.testnet.cantex.io`. |
| `TRADECRAFT_BASE_URL` | no | Tradecraft API base. Defaults to mainnet, `https://api.tradecraft.fi/v1`. Reading it needs no credentials. |

## Usage

```python
import asyncio
from decimal import Decimal
from canton_toolkit import CantexAdapter, Instrument

async def main():
    async with CantexAdapter() as venue:  # reads keys from the environment
        pools = await venue.pools()
        quote = await venue.quote(
            Decimal("1"),
            pools[0].token_a,
            pools[0].token_b,
        )
        print(quote.returned_amount, quote.trade_price)

asyncio.run(main())
```

## Run tests

```bash
pip install -e ".[dev]"
pytest        # fully offline; the SDK client is mocked at its method boundary
ruff check .
```

## Live smoke

`scripts/live_smoke.py` is a read-only check against the real Cantex API: it
authenticates (`connect()`), lists pools, and prices one small quote. It never
calls `swap()` or `transfer()`.

Create (or fill) a `.env` at the toolkit root with the real keys (same names as the [Environment variables](#environment-variables)
table above):

```
CANTEX_OPERATOR_KEY=...
CANTEX_TRADING_KEY=...
CANTEX_BASE_URL=...
```

Then run:

```bash
python scripts/live_smoke.py
```

The script never prints credential values, only step results and, on
failure, whether it was an auth error or a request error (exit code 1
either way).

## Status

Live-validated on all four venues. Tests are fully offline and run against the
real response shapes captured from each venue.

- **Cantex** (mainnet, 2026-07-17) — challenge-response auth, pools, quoting,
  and an executed swap with real funds: 10 CC sold for 1.2981357151 USDCx on
  the `CC-USDC` market.
- **Reference DEX** (hosted testnet, 2026-07-29) — swaps in both directions
  through the adapter, with balance deltas and pool reserves reconciling
  exactly. Two swap routes are supported: the documented wallet-authored
  allocation path, and deployments that host the trader's party themselves.
  `scripts/dexref_testnet_report.py` additionally exercises the venue's own
  hosted routes end to end: multi-level two-sided quoting from one party, the
  RFQ lifecycle with its best-execution receipt, and liquidity provision with
  the LP token.
- **Ekiden** (Canton testnet gateway, 2026-07-27) — markets, tickers, order
  book, recent trades and funding history across all three of its perpetual
  markets. Trading needs an Ed25519-signed session and is not implemented.
- **Tradecraft** (mainnet, 2026-08-11) — 23 pools across 13 tokens, read and
  priced end to end. `scripts/tradecraft_market_smoke.py` prices **45 of 45
  directions** and reproduces the venue's own quote to ~1e-16 relative from
  pool state alone, which is how the fee model in
  [`SOURCES.md`](SOURCES.md) was established rather than assumed: the engine
  charges half the pool's total fee on *each* leg, so the realized fee is
  0.299775% where 0.3% is published. Orders settle through Daml choices on the
  venue's package and need a validator node, so the adapter reads and prices
  but cannot trade.

Run the live checks with `scripts/live_swap_smoke.py` (Cantex, dry-run by
default), `scripts/dexref_testnet_smoke.py` and
`scripts/dexref_testnet_report.py` (reference DEX, read-only unless given
`--execute`), `scripts/ekiden_market_smoke.py` (Ekiden) and
`scripts/tradecraft_market_smoke.py` (Tradecraft) — the last two are read-only
by construction.
