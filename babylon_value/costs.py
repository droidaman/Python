"""Transaction cost model (Cure 2: control thy expenditures)."""

from __future__ import annotations

from dataclasses import dataclass

from .config import CostConfig
from .orders import Side


@dataclass(frozen=True)
class CostModel:
    config: CostConfig

    def commission(self, shares: int) -> float:
        if shares <= 0:
            return 0.0
        return max(self.config.min_commission, shares * self.config.commission_per_share)

    def slippage(self, shares: int, price: float) -> float:
        return shares * price * self.config.slippage_bps / 10_000

    def estimate(self, shares: int, price: float) -> float:
        """Total expected cost of a trade: commission plus slippage."""
        return self.commission(shares) + self.slippage(shares, price)

    def fill_price(self, side: Side, reference_price: float) -> float:
        drift = self.config.slippage_bps / 10_000
        return reference_price * (1 + drift) if side is Side.BUY else reference_price * (1 - drift)
