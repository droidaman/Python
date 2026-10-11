"""Strategy configuration.

Every tunable number lives here, grouped by the principle it serves and
annotated with the published source it comes from. Parameters are taken from
the literature, not fitted to data, and the whole configuration is frozen: a
running strategy cannot change its own rules. Its fingerprint is written to
the decision journal so every decision can be traced to the exact rule set.

Some rules are deliberately *not* configurable (long only, no leverage, no
derivatives, limit orders only). They are hard-coded in ``orders.py`` and
``execution.py`` so no configuration can switch them off.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, field


class ConfigError(ValueError):
    """The configuration violates a strategy invariant; the strategy refuses to start."""


@dataclass(frozen=True)
class IntegrityConfig:
    """Failsafe: never act on data that cannot be trusted."""

    min_history_years: int = 5          # Graham judged earnings over many years, not one
    max_filing_age_days: int = 456      # latest annual report must be under ~15 months old
    max_price_age_days: int = 7
    balance_sheet_tolerance: float = 0.02


@dataclass(frozen=True)
class CompetenceConfig:
    """Law 4: gold slips from the owner who invests in what he does not understand."""

    allowed_security_types: frozenset = frozenset({"common_stock"})
    allowed_exchanges: frozenset = frozenset({"NYSE", "NASDAQ", "NYSE American"})
    # EV/EBIT, current ratio and Altman Z are meaningless for banks, insurers,
    # REITs and regulated utilities; Greenblatt and Carlisle exclude them too.
    excluded_sectors: frozenset = frozenset({"Financials", "Real Estate", "Utilities"})
    min_revenue: float = 50_000_000.0


@dataclass(frozen=True)
class SafetyConfig:
    """Cure 4: guard thy treasures from loss (and Cure 5: tangible backing)."""

    min_altman_z: float = 1.81          # Altman (1968): below 1.81 is the distress zone
    min_current_ratio: float = 1.5      # Graham, The Intelligent Investor (enterprising investor)
    max_debt_to_equity: float = 1.0
    min_interest_coverage: float = 3.0
    earnings_lookback_years: int = 5
    min_profitable_years: int = 4       # operating profit in at least 4 of the last 5 years


@dataclass(frozen=True)
class ValueConfig:
    """Deep value margin of safety and Law 2: profitable employment for gold."""

    max_ev_to_ebit: float = 8.0                  # Carlisle, "Deep Value" (the Acquirer's Multiple)
    max_market_cap_to_ncav: float = 0.67         # Graham net-net: pay at most 2/3 of NCAV
    min_earnings_yield_vs_risk_free: float = 2.0  # Graham: earnings yield at least twice the bond yield
    min_piotroski_score: int = 6                 # Piotroski (2000) value-trap filter, out of 9


@dataclass(frozen=True)
class TricksterConfig:
    """Law 5: gold flees those who chase impossible earnings or trust tricksters."""

    max_beneish_m: float = -1.78        # Beneish (1999): above -1.78 flags likely manipulation
    max_annual_share_growth: float = 0.05
    max_dividend_yield: float = 0.10    # double-digit yields are usually about to be cut
    max_dividend_to_fcf: float = 1.0    # a dividend must be paid from free cash flow
    min_share_price: float = 2.0        # penny stocks are where pump-and-dump schemes live
    min_market_cap: float = 50_000_000.0


@dataclass(frozen=True)
class CostConfig:
    """Cure 2: control thy expenditures."""

    min_avg_daily_dollar_volume: float = 500_000.0
    max_participation_of_adv: float = 0.10   # never be more than 10% of a day's volume
    commission_per_share: float = 0.005
    min_commission: float = 1.0
    slippage_bps: float = 15.0
    max_cost_per_rebalance: float = 0.005    # fraction of NAV a single rebalance may spend on costs
    min_trade_value: float = 1_000.0         # no dust trades


@dataclass(frozen=True)
class PortfolioConfig:
    """Cure 3 (make gold multiply), Cure 4 (diversify) and Cure 6 (keep a reserve)."""

    target_positions: int = 25          # Graham: 10-30 issues; equal weight, no conviction bets
    cash_reserve: float = 0.10          # Cure 6: a reserve that is never put at risk
    max_position_weight: float = 0.07   # positions that grow beyond this are trimmed
    max_sector_weight: float = 0.25
    max_order_fraction_of_nav: float = 0.06
    limit_price_band: float = 0.02      # limit prices must sit within 2% of the reference price
    rebalance_months: tuple = (3, 6, 9, 12)  # fixed calendar: no market timing

    @property
    def target_weight(self) -> float:
        return (1.0 - self.cash_reserve) / self.target_positions


@dataclass(frozen=True)
class ExitConfig:
    """Sell discipline. Exit thresholds are looser than entry thresholds
    (hysteresis) so the strategy does not churn around a boundary."""

    exit_ev_to_ebit: float = 15.0          # fair value reached for an earnings-based holding
    exit_market_cap_to_ncav: float = 1.0   # fair value reached for a net-net
    graham_profit_target: float = 0.50     # Graham: sell after a 50% gain ...
    max_holding_days: int = 730            # ... or after two years, unless still a bargain


@dataclass(frozen=True)
class GovernanceConfig:
    """Circuit breakers for the machinery itself."""

    max_order_rejection_ratio: float = 0.25  # more rejections than this means something is broken
    max_universe_shrink: float = 0.5         # a universe that halves overnight is a data failure


@dataclass(frozen=True)
class StrategyConfig:
    version: str = "1.0.0"
    integrity: IntegrityConfig = field(default_factory=IntegrityConfig)
    competence: CompetenceConfig = field(default_factory=CompetenceConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    tricksters: TricksterConfig = field(default_factory=TricksterConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)

    def validate(self) -> "StrategyConfig":
        """Check every invariant; raise ConfigError listing all violations."""
        errors: list[str] = []

        def need(condition: bool, message: str) -> None:
            if not condition:
                errors.append(message)

        for section in dataclasses.fields(self):
            value = getattr(self, section.name)
            if not dataclasses.is_dataclass(value):
                continue
            for item in dataclasses.fields(value):
                number = getattr(value, item.name)
                if isinstance(number, float) and not math.isfinite(number):
                    errors.append(f"{section.name}.{item.name} must be finite")

        i, s, v, t = self.integrity, self.safety, self.value, self.tricksters
        c, p, x, g = self.costs, self.portfolio, self.exits, self.governance

        need(i.min_history_years >= 3, "integrity.min_history_years must be >= 3 (Piotroski needs three balance sheets)")
        need(i.max_filing_age_days > 0 and i.max_price_age_days > 0, "integrity age limits must be positive")
        need(0 <= i.balance_sheet_tolerance <= 0.1, "integrity.balance_sheet_tolerance must be in [0, 0.1]")
        need(0 < s.earnings_lookback_years <= i.min_history_years,
             "safety.earnings_lookback_years must be positive and <= integrity.min_history_years")
        need(0 < s.min_profitable_years <= s.earnings_lookback_years,
             "safety.min_profitable_years must be in (0, earnings_lookback_years]")
        need(s.min_current_ratio >= 1.0, "safety.min_current_ratio below 1.0 admits companies that cannot pay their bills")
        need(0 < s.max_debt_to_equity <= 2.0, "safety.max_debt_to_equity must be in (0, 2]")
        need(v.max_ev_to_ebit > 0, "value.max_ev_to_ebit must be positive")
        need(x.exit_ev_to_ebit > v.max_ev_to_ebit, "exits.exit_ev_to_ebit must exceed value.max_ev_to_ebit (hysteresis)")
        need(0 < v.max_market_cap_to_ncav < x.exit_market_cap_to_ncav,
             "value.max_market_cap_to_ncav must be positive and below exits.exit_market_cap_to_ncav")
        need(v.min_earnings_yield_vs_risk_free >= 1.0, "value.min_earnings_yield_vs_risk_free must be >= 1")
        need(0 <= v.min_piotroski_score <= 9, "value.min_piotroski_score must be in [0, 9]")
        need(t.max_annual_share_growth >= 0, "tricksters.max_annual_share_growth must be >= 0")
        need(0 < t.max_dividend_yield < 1, "tricksters.max_dividend_yield must be in (0, 1)")
        need(t.min_share_price > 0 and t.min_market_cap > 0, "tricksters minimum price and size must be positive")
        need(0 < c.max_participation_of_adv <= 0.25, "costs.max_participation_of_adv must be in (0, 0.25]")
        need(c.commission_per_share >= 0 and c.min_commission >= 0 and c.slippage_bps >= 0,
             "costs must not be negative")
        need(0 < c.max_cost_per_rebalance <= 0.02, "costs.max_cost_per_rebalance must be in (0, 0.02]")
        need(p.target_positions >= 10, "portfolio.target_positions below 10 concentrates risk (Graham: 10-30)")
        need(0 <= p.cash_reserve <= 0.5, "portfolio.cash_reserve must be in [0, 0.5]")
        if p.target_positions > 0 and p.cash_reserve < 1:
            need(p.target_weight <= p.max_position_weight <= 0.10,
                 "portfolio.max_position_weight must be between the target weight and 10%")
            need(p.target_weight <= p.max_order_fraction_of_nav,
                 "portfolio.max_order_fraction_of_nav must allow a full target-weight purchase")
        need(0 < p.max_sector_weight <= 1, "portfolio.max_sector_weight must be in (0, 1]")
        need(0 < p.limit_price_band <= 0.10, "portfolio.limit_price_band must be in (0, 0.10]")
        need(bool(p.rebalance_months) and all(isinstance(m, int) and 1 <= m <= 12 for m in p.rebalance_months),
             "portfolio.rebalance_months must be a non-empty list of months 1-12")
        need(x.graham_profit_target > 0 and x.max_holding_days > 0, "exit targets must be positive")
        need(0 < g.max_order_rejection_ratio < 1, "governance.max_order_rejection_ratio must be in (0, 1)")
        need(0 < g.max_universe_shrink < 1, "governance.max_universe_shrink must be in (0, 1)")

        if errors:
            raise ConfigError("; ".join(errors))
        return self

    def to_dict(self) -> dict:
        return json.loads(json.dumps(dataclasses.asdict(self), sort_keys=True, default=_jsonable))

    def fingerprint(self) -> str:
        """Stable short hash of every parameter, recorded with every decision."""
        payload = json.dumps(dataclasses.asdict(self), sort_keys=True, default=_jsonable)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _jsonable(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")
