import itertools
import math
import unittest

from babylon_value.logic import AllOf, AnyOf, LeafGate, LookAheadBiasError, Outcome, Verdict
from babylon_value.metrics import MetricUnavailable
from babylon_value.principles import Principle

from .factories import make_context, make_snapshot

P, F, E = Outcome.PASS, Outcome.FAIL, Outcome.ERROR

AND_TABLE = {
    (P, P): P, (P, F): F, (P, E): E,
    (F, P): F, (F, F): F, (F, E): E,
    (E, P): E, (E, F): E, (E, E): E,
}
OR_TABLE = {
    (P, P): P, (P, F): P, (P, E): E,
    (F, P): P, (F, F): F, (F, E): E,
    (E, P): E, (E, F): E, (E, E): E,
}


class Stub(LeafGate):
    principle = Principle.INTEGRITY

    def __init__(self, behaviour, name="stub"):
        self.behaviour = behaviour
        self.name = name
        self.calls = 0

    def check(self, snap, ctx):
        self.calls += 1
        if self.behaviour is P:
            return Verdict(True, "ok")
        if self.behaviour is F:
            return Verdict(False, "no")
        if self.behaviour is E:
            raise MetricUnavailable("missing")
        return self.behaviour(snap, ctx)


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.snap, self.ctx = make_snapshot(), make_context()

    def test_and_truth_table(self):
        for a, b in itertools.product((P, F, E), repeat=2):
            result = AllOf("and", Principle.ALL, [Stub(a), Stub(b)]).evaluate(self.snap, self.ctx)
            self.assertIs(result.outcome, AND_TABLE[(a, b)], (a, b))

    def test_or_truth_table(self):
        for a, b in itertools.product((P, F, E), repeat=2):
            result = AnyOf("or", Principle.ALL, [Stub(a), Stub(b)]).evaluate(self.snap, self.ctx)
            self.assertIs(result.outcome, OR_TABLE[(a, b)], (a, b))

    def test_empty_composites_fail_closed(self):
        self.assertIs(AllOf("and", Principle.ALL, []).evaluate(self.snap, self.ctx).outcome, E)
        self.assertIs(AnyOf("or", Principle.ALL, []).evaluate(self.snap, self.ctx).outcome, E)

    def test_unexpected_exception_is_error(self):
        def boom(snap, ctx):
            raise ZeroDivisionError("bad data")
        self.assertIs(Stub(boom).evaluate(self.snap, self.ctx).outcome, E)

    def test_malformed_verdicts_are_errors(self):
        for bad in (lambda s, c: Verdict(1, "truthy int"), lambda s, c: None, lambda s, c: True,
                    lambda s, c: Verdict(True, "nan metric", {"x": math.nan}),
                    lambda s, c: Verdict(True, "inf metric", {"x": math.inf})):
            self.assertIs(Stub(bad).evaluate(self.snap, self.ctx).outcome, E)

    def test_look_ahead_bias_propagates_through_composites(self):
        def leak(snap, ctx):
            raise LookAheadBiasError("future data")
        tree = AllOf("and", Principle.ALL, [AnyOf("or", Principle.ALL, [Stub(P), Stub(leak)])])
        with self.assertRaises(LookAheadBiasError):
            tree.evaluate(self.snap, self.ctx)

    def test_short_circuit_skips_later_checks(self):
        later = Stub(P)
        result = AllOf("and", Principle.ALL, [Stub(F), later], short_circuit=True).evaluate(self.snap, self.ctx)
        self.assertIs(result.outcome, F)
        self.assertEqual(later.calls, 0)
        self.assertIn("skipped", result.reason)

    def test_failures_point_at_the_responsible_leaves(self):
        tree = AllOf("and", Principle.ALL, [Stub(P, "a"), AnyOf("or", Principle.ALL, [Stub(F, "b"), Stub(F, "c")])])
        result = tree.evaluate(self.snap, self.ctx)
        self.assertEqual([leaf.gate for leaf in result.failures()], ["b", "c"])


if __name__ == "__main__":
    unittest.main()
