"""Data integrity gates: the failsafe foundation.

Nothing downstream is evaluated unless every one of these passes, and a
holding whose data fails them is frozen rather than traded.
"""

from __future__ import annotations

from ..logic import LeafGate, LookAheadBiasError, Verdict
from ..metrics import is_finite_number
from ..models import NON_NEGATIVE_FIELDS, NUMERIC_FIELDS
from ..principles import Principle


class PointInTimeGate(LeafGate):
    """Defence in depth against look-ahead bias. The data provider should
    never return future data; if it does, the run halts."""

    name = "point_in_time"
    principle = Principle.INTEGRITY

    def check(self, snap, ctx):
        leaked = [p.fiscal_year_end.isoformat() for p in snap.history if p.available_date > ctx.as_of]
        if leaked:
            raise LookAheadBiasError(
                f"{snap.ticker}: report(s) for {', '.join(leaked)} were not public on {ctx.as_of}"
            )
        if snap.market.as_of > ctx.as_of:
            raise LookAheadBiasError(f"{snap.ticker}: price dated {snap.market.as_of} is after {ctx.as_of}")
        return Verdict(True, "every input was public on the decision date")


class HistoryDepthGate(LeafGate):
    name = "history_depth"
    principle = Principle.INTEGRITY

    def check(self, snap, ctx):
        need = ctx.config.integrity.min_history_years
        have = len(snap.history)
        if have < need:
            return Verdict(False, f"{have} annual report(s) available, {need} required", {"years": have})
        recent = snap.history[:need]
        for newer, older in zip(recent, recent[1:]):
            gap = (newer.fiscal_year_end - older.fiscal_year_end).days
            if not 330 <= gap <= 400:
                return Verdict(False, f"fiscal years {older.fiscal_year_end} and {newer.fiscal_year_end} "
                                      "are not consecutive", {"years": have})
        return Verdict(True, f"{need} consecutive annual reports", {"years": have})


class CompletenessGate(LeafGate):
    """Every required field must be present, finite and of the right sign.
    Missing values are never filled with zeros or guesses."""

    name = "completeness"
    principle = Principle.INTEGRITY

    def check(self, snap, ctx):
        depth = ctx.config.integrity.min_history_years
        problems: list[str] = []
        if not is_finite_number(snap.market.price) or snap.market.price <= 0:
            problems.append("price")
        if not is_finite_number(snap.market.avg_daily_dollar_volume) or snap.market.avg_daily_dollar_volume < 0:
            problems.append("average daily dollar volume")
        for years_back, period in enumerate(snap.history[:depth]):
            for field_name in NUMERIC_FIELDS:
                value = getattr(period, field_name)
                if not is_finite_number(value):
                    problems.append(f"{field_name} (t-{years_back}) missing")
                elif field_name in NON_NEGATIVE_FIELDS and value < 0:
                    problems.append(f"{field_name} (t-{years_back}) negative")
            for field_name in ("total_assets", "shares_outstanding"):
                value = getattr(period, field_name)
                if is_finite_number(value) and value <= 0:
                    problems.append(f"{field_name} (t-{years_back}) not positive")
        if problems:
            shown = ", ".join(problems[:4])
            if len(problems) > 4:
                shown += f" (+{len(problems) - 4} more)"
            return Verdict(False, f"invalid or missing data: {shown}", {"problems": len(problems)})
        return Verdict(True, "all required fields present and valid")


class FreshnessGate(LeafGate):
    name = "freshness"
    principle = Principle.INTEGRITY

    def check(self, snap, ctx):
        cfg = ctx.config.integrity
        price_age = (ctx.as_of - snap.market.as_of).days
        filing_age = (ctx.as_of - snap.latest.available_date).days
        metrics = {"price_age_days": price_age, "filing_age_days": filing_age}
        if price_age > cfg.max_price_age_days:
            return Verdict(False, f"price is {price_age} days old (max {cfg.max_price_age_days})", metrics)
        if filing_age > cfg.max_filing_age_days:
            return Verdict(False, f"latest annual report is {filing_age} days old (max {cfg.max_filing_age_days})",
                           metrics)
        return Verdict(True, "price and annual report are current", metrics)


class AccountingIdentityGate(LeafGate):
    """Balance sheets must add up. Statements that do not reconcile are
    either data errors or something worse; either way, no capital moves."""

    name = "accounting_identity"
    principle = Principle.INTEGRITY

    def check(self, snap, ctx):
        tol = ctx.config.integrity.balance_sheet_tolerance
        for years_back, p in enumerate(snap.history[: ctx.config.integrity.min_history_years]):
            gap = abs(p.total_assets - (p.total_liabilities + p.shareholders_equity + p.minority_interest)) / p.total_assets
            if gap > tol:
                return Verdict(False, f"assets differ from liabilities + equity by {gap:.1%} (t-{years_back})",
                               {"gap": gap})
            rules = (
                (p.cash_and_equivalents + p.receivables <= p.current_assets * (1 + tol),
                 "cash plus receivables exceed current assets"),
                (p.current_assets + p.ppe_net + p.intangible_assets <= p.total_assets * (1 + tol),
                 "asset components exceed total assets"),
                (p.long_term_debt <= p.total_debt * (1 + tol), "long-term debt exceeds total debt"),
                (p.total_debt <= p.total_liabilities * (1 + tol), "debt exceeds total liabilities"),
                (p.current_liabilities <= p.total_liabilities * (1 + tol),
                 "current liabilities exceed total liabilities"),
            )
            for holds, message in rules:
                if not holds:
                    return Verdict(False, f"{message} (t-{years_back})")
        return Verdict(True, "balance sheets reconcile")
