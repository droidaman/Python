"""Three-valued, fail-closed logic gates.

Every rule in the strategy is a gate: a small, pure check that looks at one
security on one date and answers PASS, FAIL or ERROR.

* PASS  - evaluated on valid data, and the rule is satisfied.
* FAIL  - evaluated on valid data, and the rule is not satisfied.
* ERROR - could not be evaluated: missing data, a zero denominator, an
          unexpected exception or a malformed answer.

Gates compose with AND (:class:`AllOf`) and OR (:class:`AnyOf`). ERROR is
*infectious* (Bochvar's three-valued logic): if any input is ERROR the output
is ERROR. Only an explicit PASS at the root lets capital move, so anything
unexpected fails closed. There is deliberately no NOT gate: negating an
indeterminate answer is how fail-closed systems quietly become fail-open.

    AND    | PASS   FAIL   ERROR          OR     | PASS   FAIL   ERROR
    -------+--------------------          -------+--------------------
    PASS   | PASS   FAIL   ERROR          PASS   | PASS   PASS   ERROR
    FAIL   | FAIL   FAIL   ERROR          FAIL   | PASS   FAIL   ERROR
    ERROR  | ERROR  ERROR  ERROR          ERROR  | ERROR  ERROR  ERROR

An empty AND or OR is ERROR: a rule set that checks nothing proves nothing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING, Iterable, Iterator, Mapping, Sequence

from .metrics import MetricUnavailable, is_finite_number
from .principles import Principle

if TYPE_CHECKING:
    from .config import StrategyConfig
    from .models import SecuritySnapshot


class Outcome(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


class LookAheadBiasError(RuntimeError):
    """Data from the future is visible to a decision. This is a defect, not a
    rejection, so it is never absorbed by a gate: it halts the whole run."""


def all_of(outcomes: Iterable[Outcome]) -> Outcome:
    outcomes = list(outcomes)
    if not outcomes or Outcome.ERROR in outcomes:
        return Outcome.ERROR
    return Outcome.PASS if all(o is Outcome.PASS for o in outcomes) else Outcome.FAIL


def any_of(outcomes: Iterable[Outcome]) -> Outcome:
    outcomes = list(outcomes)
    if not outcomes or Outcome.ERROR in outcomes:
        return Outcome.ERROR
    return Outcome.PASS if Outcome.PASS in outcomes else Outcome.FAIL


@dataclass(frozen=True)
class GateResult:
    gate: str
    principle: Principle
    outcome: Outcome
    reason: str
    metrics: Mapping[str, float] = field(default_factory=dict)
    children: tuple["GateResult", ...] = ()

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS

    def failures(self) -> list["GateResult"]:
        """The leaf gates responsible for a non-PASS outcome."""
        if self.passed:
            return []
        if not self.children:
            return [self]
        found = [leaf for child in self.children for leaf in child.failures()]
        return found or [self]

    def leaves(self) -> Iterator["GateResult"]:
        if not self.children:
            yield self
        for child in self.children:
            yield from child.leaves()

    def to_dict(self) -> dict:
        out = {"gate": self.gate, "principle": self.principle.value, "outcome": self.outcome.value, "reason": self.reason}
        if self.metrics:
            out["metrics"] = dict(self.metrics)
        if self.children:
            out["children"] = [child.to_dict() for child in self.children]
        return out

    def render(self, indent: int = 0) -> str:
        lines = [f"{'  ' * indent}[{self.outcome.value:<5}] {self.gate}: {self.reason}"]
        lines.extend(child.render(indent + 1) for child in self.children)
        return "\n".join(lines)


@dataclass(frozen=True)
class EvaluationContext:
    as_of: date
    config: "StrategyConfig"
    risk_free_rate: float


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str
    metrics: Mapping[str, float] = field(default_factory=dict)


class Gate(ABC):
    name: str
    principle: Principle

    @abstractmethod
    def evaluate(self, snap: "SecuritySnapshot", ctx: EvaluationContext) -> GateResult:
        ...


class LeafGate(Gate):
    """A single rule. Subclasses implement :meth:`check`; :meth:`evaluate`
    converts every way the check can go wrong into ERROR."""

    name = "unnamed"
    principle = Principle.INTEGRITY

    @abstractmethod
    def check(self, snap: "SecuritySnapshot", ctx: EvaluationContext) -> Verdict:
        ...

    def evaluate(self, snap: "SecuritySnapshot", ctx: EvaluationContext) -> GateResult:
        try:
            verdict = self.check(snap, ctx)
        except LookAheadBiasError:
            raise
        except MetricUnavailable as exc:
            return self._result(Outcome.ERROR, f"cannot be determined: {exc}")
        except Exception as exc:  # fail closed on anything unexpected
            return self._result(Outcome.ERROR, f"unexpected {type(exc).__name__}: {exc}")
        return self._seal(verdict)

    def _seal(self, verdict) -> GateResult:
        if not isinstance(verdict, Verdict) or not isinstance(verdict.passed, bool):
            return self._result(Outcome.ERROR, "gate returned a malformed verdict")
        for key, value in verdict.metrics.items():
            if not is_finite_number(value) and not isinstance(value, bool):
                return self._result(Outcome.ERROR, f"metric {key!r} is not a finite number")
        outcome = Outcome.PASS if verdict.passed else Outcome.FAIL
        return self._result(outcome, verdict.reason, verdict.metrics)

    def _result(self, outcome: Outcome, reason: str, metrics: Mapping[str, float] | None = None) -> GateResult:
        return GateResult(self.name, self.principle, outcome, reason, dict(metrics or {}))


class AllOf(Gate):
    """Logical AND. Evaluates every child, for a complete audit trail, unless
    ``short_circuit`` is set - used where later checks would only be
    computing on data that has already been found untrustworthy."""

    def __init__(self, name: str, principle: Principle, children: Sequence[Gate], short_circuit: bool = False):
        self.name = name
        self.principle = principle
        self.children = tuple(children)
        self.short_circuit = short_circuit

    def evaluate(self, snap: "SecuritySnapshot", ctx: EvaluationContext) -> GateResult:
        results: list[GateResult] = []
        for child in self.children:
            result = _evaluate_child(child, snap, ctx)
            results.append(result)
            if self.short_circuit and not result.passed:
                break
        outcome = all_of(r.outcome for r in results)
        if not self.children:
            reason = "no checks configured"
        elif outcome is Outcome.PASS:
            reason = "check passed" if len(results) == 1 else f"all {len(results)} checks passed"
        else:
            reason = _describe_problems(results)
            skipped = len(self.children) - len(results)
            if skipped:
                reason += f"; {skipped} later check(s) skipped"
        return GateResult(self.name, self.principle, outcome, reason, children=tuple(results))


class AnyOf(Gate):
    """Logical OR. Never short-circuits: an ERROR in any branch must be seen."""

    def __init__(self, name: str, principle: Principle, children: Sequence[Gate]):
        self.name = name
        self.principle = principle
        self.children = tuple(children)

    def evaluate(self, snap: "SecuritySnapshot", ctx: EvaluationContext) -> GateResult:
        results = [_evaluate_child(child, snap, ctx) for child in self.children]
        outcome = any_of(r.outcome for r in results)
        if not self.children:
            reason = "no alternatives configured"
        elif outcome is Outcome.PASS:
            reason = "satisfied by " + ", ".join(r.gate for r in results if r.passed)
        elif outcome is Outcome.ERROR:
            reason = _describe_problems(results)
        else:
            reason = "no alternative satisfied: " + ", ".join(r.gate for r in results)
        return GateResult(self.name, self.principle, outcome, reason, children=tuple(results))


def _evaluate_child(child: Gate, snap: "SecuritySnapshot", ctx: EvaluationContext) -> GateResult:
    try:
        result = child.evaluate(snap, ctx)
    except LookAheadBiasError:
        raise
    except Exception as exc:
        return GateResult(getattr(child, "name", "unknown"), getattr(child, "principle", Principle.INTEGRITY),
                          Outcome.ERROR, f"unexpected {type(exc).__name__}: {exc}")
    if not isinstance(result, GateResult):
        return GateResult(getattr(child, "name", "unknown"), getattr(child, "principle", Principle.INTEGRITY),
                          Outcome.ERROR, "gate returned a malformed result")
    return result


def _describe_problems(results: Sequence[GateResult]) -> str:
    errors = [r.gate for r in results if r.outcome is Outcome.ERROR]
    failed = [r.gate for r in results if r.outcome is Outcome.FAIL]
    parts = []
    if errors:
        parts.append("indeterminate: " + ", ".join(errors))
    if failed:
        parts.append("failed: " + ", ".join(failed))
    return "; ".join(parts)
