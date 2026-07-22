# Canton Trading Toolkit

An open-source algorithmic-trading layer for the [Canton Network](https://www.canton.network/):
one typed async Python interface to quote, trade, and run strategies on Canton venues,
designed venue-agnostic from day one. Two adapters today: [Cantex](https://cantex.io/) and
the [Canton DEX reference implementation](https://github.com/srikanth-bitdynamics/Canton-Dex-Reference-Implementation).

## Why this exists

Canton carries the largest tokenized-RWA value of any chain, but its open DeFi layer is
young — venues are appearing faster than open tooling. Today anyone who wants to trade
programmatically (a market-maker, a fund, an AI agent) has to hand-roll key handling,
challenge-response auth, intent signing, and venue-specific plumbing. This toolkit builds
that missing layer once, in the open.

## What you can do today

- **Read the market from Python** — list pools, price swap quotes, read account balances
  through `pools()`, `quote()`, `balances()`. Good for monitoring, analytics, price feeds,
  and strategy research.
- **Execute programmatically** — `swap()` and `transfer()` through the same typed
  interface. Ed25519 challenge-response auth and secp256k1 intent signing are handled
  under the hood via the official venue SDK; your code never touches the crypto.
- **Write venue-agnostic strategies** — code against the `VenueAdapter` interface, not a
  specific exchange. A strategy written today runs on the next venue by swapping the
  adapter, not the strategy.
- **Develop offline** — the full test suite mocks the venue SDK at its boundary, so bot
  development needs no keys and no network.

## Who it's for

- **Market-makers and bot operators** who want resting liquidity on Canton pools
- **Funds and trading desks** that need programmatic access to Canton DeFi
- **AI-agent builders** — a planned MCP execution interface will expose trading as typed
  tools behind hard server-side risk caps (max position, max slippage, allow-listed pairs)

## Live-validated

**Cantex adapter** — verified against **mainnet** with real credentials:

- 2026-07-14, read path: auth, pool listing, quoting:

```
base url: https://api.cantex.io
auth OK
pools: 9
first pool pair: Amulet/USDCx
quote: sell 1 Amulet -> 0.1321382958 USDCx (trade price 0.1321382958)
```

- 2026-07-17, write path: executed a real swap on the `CC-USDC` pool — sold 10.0 Amulet,
  received 1.2981357151 USDCx (trade price 0.12963), confirmed on ledger.

**Reference DEX adapter** — verified end to end on 2026-07-18 against the operator
backend running in local demo mode: pools, holdings aggregation into balances, quotes
with derived price/slippage/fee, and an executed demo swap that moved pool reserves.

## Architecture

The core (`canton_toolkit.core`) is venue-agnostic: a small typed domain model
(`Instrument`, `Pool`, `Balance`, `Quote`, `SwapResult`) and a `VenueAdapter`
interface with a `VenueError` hierarchy. The first adapter,
`CantexAdapter` (`canton_toolkit.venues.cantex`), wraps the official
[`cantex_sdk`](https://github.com/caviarnine/cantex_sdk) async client. Auth,
signing, and transport all live in the SDK; the adapter only translates its
models and exceptions into the venue-agnostic core. The second adapter,
`DexRefAdapter` (`canton_toolkit.venues.dexref`), speaks the reference DEX
operator-backend HTTP API directly (no vendor SDK exists) and delegates the
venue-specific wallet-authored allocation step to a pluggable
`AllocationAuthorizer` strategy. Every integration point of both adapters is
mapped to its exact upstream source location in [`SOURCES.md`](SOURCES.md).

## Install

`cantex_sdk` is not published on PyPI, so it is declared as a git dependency
and pulled directly from GitHub:

```bash
pip install "canton_toolkit @ git+https://github.com/olevasyliev/canton-trading-toolkit"
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

## Usage

```python
import asyncio
from decimal import Decimal
from canton_toolkit import CantexAdapter

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

Copy `.env.example` to `.env` at the repo root and fill in the real keys, then:

```bash
python scripts/live_smoke.py
```

The script never prints credential values, only step results and, on
failure, whether it was an auth error or a request error (exit code 1
either way).

## Roadmap

1. **Venue connector** (this repo, live) — unified interface; Cantex adapter validated
   on mainnet (read + write), reference-DEX adapter validated against the operator
   backend in demo mode
2. **Reference liquidity bots** — grid/DCA engines that keep measurable resting
   depth on thin pools, with a DevNet dry-run mode
3. **Same strategy, two venues** — one strategy config running unmodified on both
   adapters against live networks
4. **MCP agent-execution interface** — trading as typed tools for any MCP-capable
   agent framework, behind enforced risk limits

## License

[Apache-2.0](LICENSE)
