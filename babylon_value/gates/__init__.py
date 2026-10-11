"""Assembles the individual gates into the two pipelines the strategy uses.

Entry pipeline (may we buy this?):

    entry = AND[short-circuit](
        data_integrity,                       # failsafe foundation
        investment_case = AND(
            circle_of_competence,             # Law 4
            guard_thy_treasure,               # Cure 4 / Cure 5
            no_tricksters,                    # Law 5
            control_expenditures,             # Cure 2
            profitable_employment = AND(      # Law 2 / Law 3
                margin_of_safety = OR(
                    AND(acquirers_multiple, earnings_yield_hurdle),
                    net_net),
                piotroski)))

Thesis pipeline (is the reason we own this still intact?) is the same
without the valuation and tradability stages, which the exit rules handle.
"""

from __future__ import annotations

from ..logic import AllOf, GateResult
from ..principles import Principle
from .competence import EstablishedBusinessGate, ExchangeGate, SectorGate, SecurityTypeGate
from .costs import TradabilityGate
from .integrity import AccountingIdentityGate, CompletenessGate, FreshnessGate, HistoryDepthGate, PointInTimeGate
from .safety import AltmanZGate, EarningsStabilityGate, LeverageGate, LiquidityGate, TangibleBackingGate
from .tricksters import BeneishGate, DilutionGate, DividendSustainabilityGate, PennyStockGate
from .value import PiotroskiGate, margin_of_safety_gate

DATA_INTEGRITY = "data_integrity"
CIRCLE_OF_COMPETENCE = "circle_of_competence"
GUARD_THY_TREASURE = "guard_thy_treasure"
NO_TRICKSTERS = "no_tricksters"
CONTROL_EXPENDITURES = "control_expenditures"
PROFITABLE_EMPLOYMENT = "profitable_employment"
INVESTMENT_CASE = "investment_case"
THESIS_INTACT = "thesis_intact"

STAGE_ORDER = (
    DATA_INTEGRITY,
    CIRCLE_OF_COMPETENCE,
    GUARD_THY_TREASURE,
    NO_TRICKSTERS,
    CONTROL_EXPENDITURES,
    PROFITABLE_EMPLOYMENT,
)


def integrity_stage() -> AllOf:
    return AllOf(DATA_INTEGRITY, Principle.INTEGRITY, [
        PointInTimeGate(), HistoryDepthGate(), CompletenessGate(), FreshnessGate(), AccountingIdentityGate(),
    ], short_circuit=True)


def competence_stage() -> AllOf:
    return AllOf(CIRCLE_OF_COMPETENCE, Principle.LAW_4, [
        SecurityTypeGate(), ExchangeGate(), SectorGate(), EstablishedBusinessGate(),
    ])


def safety_stage() -> AllOf:
    return AllOf(GUARD_THY_TREASURE, Principle.CURE_4, [
        AltmanZGate(), LiquidityGate(), LeverageGate(), EarningsStabilityGate(), TangibleBackingGate(),
    ])


def trickster_stage() -> AllOf:
    return AllOf(NO_TRICKSTERS, Principle.LAW_5, [
        BeneishGate(), DilutionGate(), DividendSustainabilityGate(), PennyStockGate(),
    ])


def cost_stage() -> AllOf:
    return AllOf(CONTROL_EXPENDITURES, Principle.CURE_2, [TradabilityGate()])


def value_stage() -> AllOf:
    return AllOf(PROFITABLE_EMPLOYMENT, Principle.LAW_2, [margin_of_safety_gate(), PiotroskiGate()])


def build_entry_pipeline() -> AllOf:
    case = AllOf(INVESTMENT_CASE, Principle.ALL, [
        competence_stage(), safety_stage(), trickster_stage(), cost_stage(), value_stage(),
    ])
    return AllOf("entry", Principle.ALL, [integrity_stage(), case], short_circuit=True)


def build_thesis_pipeline() -> AllOf:
    intact = AllOf(THESIS_INTACT, Principle.ALL, [competence_stage(), safety_stage(), trickster_stage()])
    return AllOf("thesis", Principle.ALL, [integrity_stage(), intact], short_circuit=True)


def first_failing_stage(result: GateResult) -> str:
    """Name of the first pipeline stage that stopped a security, for the funnel."""
    for child in result.children:
        if not child.passed:
            if child.gate in (INVESTMENT_CASE, THESIS_INTACT):
                return first_failing_stage(child)
            return child.gate
    return result.gate
