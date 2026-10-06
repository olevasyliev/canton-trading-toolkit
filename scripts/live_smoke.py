"""Read-only live smoke test against the real Cantex API.

Loads credentials from a local ``.env``, then exercises the real
challenge-response auth (``connect()``), lists pools, and prices a single
small quote. Never calls ``swap()`` or ``transfer()``. See README.md
("Live smoke") for setup and usage.
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

from cantonvenues import VenueAuthError, VenueError
from cantonvenues.venues.cantex import BASE_URL_ENV, DEFAULT_BASE_URL, CantexAdapter

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = TOOLKIT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


async def _run() -> int:
    base_url = os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL
    print(f"base url: {base_url}")

    try:
        async with CantexAdapter() as venue:
            print("auth OK")
            pools = await venue.pools()
            print(f"pools: {len(pools)}")
            if pools:
                pool = pools[0]
                print(f"first pool pair: {pool.token_a.id}/{pool.token_b.id}")
                quote = await venue.quote(Decimal("1"), pool.token_a, pool.token_b)
                print(
                    f"quote: sell 1 {pool.token_a.id} -> "
                    f"{quote.returned_amount} {quote.returned_instrument.id} "
                    f"(trade price {quote.trade_price})"
                )
    except VenueAuthError as exc:
        print(f"AUTH FAILED: {exc}")
        return 1
    except VenueError as exc:
        print(f"REQUEST FAILED: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    _load_env_file(ENV_FILE)
    sys.exit(asyncio.run(_run()))
