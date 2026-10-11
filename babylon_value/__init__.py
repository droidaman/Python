"""Babylon Value: a fail-closed, rules-based deep value equity strategy.

Grounded in deep value investing (Graham, Greenblatt, Carlisle) and the Five
Laws of Gold and Seven Cures for a Lean Purse from George S. Clason's "The
Richest Man in Babylon". See docs/PLAN.md for the design.
"""

from .backtest import BacktestResult, equal_weight_benchmark, run_backtest
from .config import ConfigError, StrategyConfig
from .engine import Rebalancer, RebalanceReport, RunStatus
from .execution import PaperBroker
from .gates import build_entry_pipeline, build_thesis_pipeline
from .logic import EvaluationContext, GateResult, LookAheadBiasError, Outcome

__version__ = "0.2.0"

__all__ = [
    "BacktestResult",
    "ConfigError",
    "EvaluationContext",
    "GateResult",
    "LookAheadBiasError",
    "Outcome",
    "PaperBroker",
    "RebalanceReport",
    "Rebalancer",
    "RunStatus",
    "StrategyConfig",
    "build_entry_pipeline",
    "build_thesis_pipeline",
    "equal_weight_benchmark",
    "run_backtest",
]
