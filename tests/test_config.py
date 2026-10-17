import dataclasses
import unittest

from babylon_value.config import ConfigError, ExitConfig, PortfolioConfig, StrategyConfig, ValueConfig


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        StrategyConfig().validate()

    def test_exit_threshold_must_be_looser_than_entry(self):
        with self.assertRaisesRegex(ConfigError, "hysteresis"):
            StrategyConfig(exits=ExitConfig(exit_ev_to_ebit=7.0)).validate()

    def test_concentration_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "target_positions"):
            StrategyConfig(portfolio=PortfolioConfig(target_positions=5)).validate()

    def test_reserve_and_position_limits(self):
        with self.assertRaisesRegex(ConfigError, "cash_reserve"):
            StrategyConfig(portfolio=PortfolioConfig(cash_reserve=0.8)).validate()
        with self.assertRaisesRegex(ConfigError, "max_position_weight"):
            StrategyConfig(portfolio=PortfolioConfig(max_position_weight=0.25)).validate()

    def test_non_finite_parameters_are_refused(self):
        with self.assertRaisesRegex(ConfigError, "finite"):
            StrategyConfig(value=ValueConfig(max_ev_to_ebit=float("nan"))).validate()

    def test_fingerprint_is_stable_and_sensitive(self):
        base = StrategyConfig()
        self.assertEqual(base.fingerprint(), StrategyConfig().fingerprint())
        changed = dataclasses.replace(base, value=ValueConfig(max_ev_to_ebit=9.0))
        self.assertNotEqual(base.fingerprint(), changed.fingerprint())


if __name__ == "__main__":
    unittest.main()
