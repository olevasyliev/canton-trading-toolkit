"""Read-only reserve pools: one shape for AMM venues we price from reserves alone.

OneSwap and Pool Party publish their pool reserves without a key but no quote
route (OneSwap's quote needs an SDK key; Pool Party has none). Pricing is done
locally from the reserves, the same way Canton Venues prices Cantex.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..core.models import Instrument


def constant_product_output(reserve_in: Decimal, reserve_out: Decimal, amount: Decimal, fee: Decimal) -> Decimal:
    """x*y=k with the fee taken on the input."""
    net_in = amount * (Decimal(1) - fee)
    return reserve_out * net_in / (reserve_in + net_in)


@dataclass(frozen=True)
class ReservePool:
    """One pool as its venue's public API reports it.

    ``token_a``/``token_b`` are the full Canton instruments when the venue
    publishes the issuer; Pool Party publishes only instrument ids, so there
    they carry ``admin=""`` and matching has to fall back to the id.
    """

    venue: str
    pool_id: str
    symbol_a: str
    symbol_b: str
    token_a: Instrument
    token_b: Instrument
    reserve_a: Decimal
    reserve_b: Decimal
    fee: Decimal  # fraction, on the input
    fee_source: str  # where the fee figure comes from

    def output(self, sell_a: bool, amount: Decimal) -> Decimal:
        if sell_a:
            return constant_product_output(self.reserve_a, self.reserve_b, amount, self.fee)
        return constant_product_output(self.reserve_b, self.reserve_a, amount, self.fee)
