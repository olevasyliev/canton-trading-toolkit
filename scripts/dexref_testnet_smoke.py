#!/usr/bin/env python3
"""End-to-end smoke test of the DexRef adapter against a hosted testnet.

Read-only by default; pass ``--execute`` to submit a real swap.

    DEXREF_BASE_URL=https://testnet-dex.bitdynamics.cc/api \
    DEXREF_TRADER_PARTY='dex-tester-...::1220...' \
    PYTHONPATH=src python3 scripts/dexref_testnet_smoke.py --sell 0.01 --execute

Without ``DEXREF_TRADER_PARTY`` the script asks the deployment to host a fresh
party for it (``POST /v1/testnet/party``), which also airdrops test assets.
Every number below is read through the adapter, not the venue's HTTP API.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal

import httpx

from canton_toolkit import DexRefAdapter, HostedPartySwapRoute, Instrument

DEFAULT_BASE_URL = "https://testnet-dex.bitdynamics.cc/api"


async def host_a_party(base_url: str) -> str:
    """Ask the deployment for a hosted party. Deployment-side endpoint."""
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(f"{base_url}/v1/testnet/party", json={})
        resp.raise_for_status()
        body = resp.json()
    airdrops = ", ".join(f"{a['amount']} {a['instrumentId']}" for a in body.get("airdrops", []))
    print(f"hosted party : {body['partyId']}")
    print(f"airdrop      : {airdrops or 'none'}")
    return str(body["partyId"])


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sell", default="0.01", help="amount of the base instrument to sell")
    parser.add_argument("--base", default="dBTC")
    parser.add_argument("--quote", default="dUSD")
    parser.add_argument("--execute", action="store_true", help="submit the swap for real")
    args = parser.parse_args()

    base_url = os.getenv("DEXREF_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    trader = os.getenv("DEXREF_TRADER_PARTY") or await host_a_party(base_url)

    adapter = DexRefAdapter(
        base_url=base_url,
        trader_party=trader,
        swap_route=HostedPartySwapRoute(),
    )
    async with adapter:
        pools = await adapter.pools()
        print(f"\npools        : {len(pools)}")
        for p in pools:
            print(f"  {p.token_a.id}/{p.token_b.id}  {p.contract_id[:24]}…")

        admin = pools[0].token_a.admin
        sell_instrument = Instrument(admin=admin, id=args.base)
        buy_instrument = Instrument(admin=admin, id=args.quote)
        sell_amount = Decimal(args.sell)

        before = {b.instrument.id: b for b in await adapter.balances()}
        print("\nbalances before")
        for b in before.values():
            print(f"  {b.symbol:6} {b.unlocked:>18} free  {b.locked:>18} locked")

        quote = await adapter.quote(sell_amount, sell_instrument, buy_instrument)
        print(
            f"\nquote        : {sell_amount} {args.base} -> {quote.returned_amount} {args.quote}"
            f"\n  price      : {quote.trade_price}"
            f"\n  slippage   : {quote.slippage * 100:.4f}%"
            f"\n  pool fee   : {quote.fee_percentage * 100:.2f}%"
        )

        if not args.execute:
            print("\ndry run: pass --execute to submit")
            return 0

        result = await adapter.swap(sell_amount, sell_instrument, buy_instrument)
        print(
            f"\nEXECUTED     : {result.input_amount} {result.input_instrument.id}"
            f" -> {result.output_amount} {result.output_instrument.id}"
            f"\n  market     : {result.market}"
            f"\n  price      : {result.price}"
            f"\n  LP fee     : {result.liquidity_fee_amount} {result.input_instrument.id}"
        )

        after = {b.instrument.id: b for b in await adapter.balances()}
        print("\nbalance deltas")
        for instrument_id in sorted(set(before) | set(after)):
            was = before.get(instrument_id)
            now = after.get(instrument_id)
            delta = (now.unlocked if now else Decimal(0)) - (was.unlocked if was else Decimal(0))
            print(f"  {instrument_id:6} {delta:+.10f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
