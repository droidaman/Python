"""Portfolio state as reported by the broker, valued at decision-date prices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping


@dataclass(frozen=True)
class Position:
    ticker: str
    sector: str
    shares: int
    cost_basis: float       # total cost paid, including commissions
    opened_on: date


@dataclass(frozen=True)
class PortfolioState:
    cash: float
    positions: Mapping[str, Position]
    prices: Mapping[str, float]

    def position_value(self, ticker: str) -> float:
        position = self.positions.get(ticker)
        return position.shares * self.prices[ticker] if position else 0.0

    @property
    def invested(self) -> float:
        return sum(self.position_value(t) for t in self.positions)

    @property
    def nav(self) -> float:
        return self.cash + self.invested

    def weight(self, ticker: str) -> float:
        return self.position_value(ticker) / self.nav

    def sector_values(self) -> dict[str, float]:
        values: dict[str, float] = {}
        for ticker, position in self.positions.items():
            values[position.sector] = values.get(position.sector, 0.0) + self.position_value(ticker)
        return values
