"""Offline tests for the risk monitor: the only three things allowed to trade between rebalances.

The frozen thresholds are the ones the realistic backtest measured - a ticker above 39% of initial
assets is cut to 38%, margin above 95% is scaled to 90%, and -8% from the peak flattens and halts.
Positions the strategy did not open are measured but never traded.
"""
from __future__ import annotations

import unittest

from live.contracts import finite
from live.risk_monitor import RiskError, actions, flatten, measure
from live.test_pstar import config_for

EQUITY = 100_000.0
CONFIG = config_for()
TRIM_ABOVE = CONFIG["trim_ticker_above_of_initial"] * EQUITY
CAP = CONFIG["ticker_cap_of_initial"] * EQUITY


def position(symbol: str, quantity: int, price: float = 1_000.0) -> dict:
    return {"symbol": symbol, "quantity": quantity, "price": price}


class Measurement(unittest.TestCase):
    def test_the_book_is_measured_from_the_broker_s_own_rows(self):
        state = measure(CONFIG, [position("SPY", 10), position("US.AAOI", -4, 50.0)], EQUITY, EQUITY, EQUITY)
        self.assertEqual([state["long_gross"], state["short_gross"]], [10_000.0, 200.0])
        self.assertEqual(state["values"], {"SPY": 10_000.0, "AAOI": -200.0})
        self.assertEqual([state["quantities"]["SPY"], state["prices"]["AAOI"]], [10, 50.0])
        self.assertEqual(state["margin"], (0.5 * 10_000 + 0.6 * 200) / EQUITY)
        self.assertEqual([state["positions"], state["max_ticker"], state["over_trim"]], [2, "SPY", []])
        self.assertAlmostEqual(state["max_ticker_vs_initial"], 0.1, places=12)
        self.assertEqual(state["drawdown"], 0.0)

    def test_an_empty_account_measures_zero_rather_than_inventing_risk(self):
        state = measure(CONFIG, [], EQUITY, EQUITY, EQUITY)
        self.assertEqual([state["positions"], state["margin"], state["max_ticker"]], [0, 0.0, None])
        self.assertEqual([state["max_ticker_vs_initial"], state["over_trim"]], [0.0, []])
        self.assertEqual(actions(CONFIG, state)["orders_required"], 0)

    def test_the_trim_threshold_is_exclusive_and_the_cap_is_inclusive(self):
        self.assertEqual(measure(CONFIG, [position("SPY", 39)], EQUITY, EQUITY, EQUITY)["over_trim"], [])
        self.assertEqual(measure(CONFIG, [position("SPY", 40)], EQUITY, EQUITY, EQUITY)["over_trim"], ["SPY"])
        self.assertAlmostEqual(TRIM_ABOVE, 39_000.0, places=6)
        self.assertAlmostEqual(CAP, 38_000.0, places=6)

    def test_an_unmeasurable_account_raises_instead_of_guessing(self):
        cases = {
            "equity zero": lambda: measure(CONFIG, [], 0, EQUITY, EQUITY),
            "equity missing": lambda: measure(CONFIG, [], None, EQUITY, EQUITY),
            "no initial equity": lambda: measure(CONFIG, [], EQUITY, 0, EQUITY),
            "peak below equity": lambda: measure(CONFIG, [], EQUITY * 1.1, EQUITY, EQUITY),
            "duplicate holding": lambda: measure(CONFIG, [position("SPY", 1), position("US.SPY", 2)],
                                                 EQUITY, EQUITY, EQUITY),
            "fractional holding": lambda: measure(CONFIG, [position("SPY", 1.5)], EQUITY, EQUITY, EQUITY),
            "boolean quantity": lambda: measure(CONFIG, [{"symbol": "SPY", "quantity": True,
                                                          "price": 100.0}], EQUITY, EQUITY, EQUITY),
            "no price": lambda: measure(CONFIG, [{"symbol": "SPY", "quantity": 1}], EQUITY, EQUITY, EQUITY),
            "a negative price": lambda: measure(CONFIG, [position("SPY", 1, -10.0)], EQUITY, EQUITY, EQUITY),
            "an infinite price": lambda: measure(CONFIG, [position("SPY", 1, float("inf"))],
                                                 EQUITY, EQUITY, EQUITY),
        }
        for label, run in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(RiskError):
                    run()


class Trim(unittest.TestCase):
    def setUp(self):
        self.state = measure(CONFIG, [position("SPY", 40), position("QQQ", -5)], EQUITY, EQUITY, EQUITY)

    def test_a_name_above_the_trim_is_cut_back_to_the_cap_rounded_down(self):
        result = actions(CONFIG, self.state)
        self.assertFalse(result["halt"])
        self.assertEqual(result["delever_scale"], 1.0)
        self.assertEqual(result["target_shares"], {"SPY": 38, "QQQ": -5})
        self.assertEqual(result["share_changes"], {"SPY": -2})
        self.assertEqual(result["orders_required"], 1)
        self.assertIn("trim SPY to 38,000 of initial assets (2 shares).", result["slack_report"])
        self.assertLessEqual(38 * self.state["prices"]["SPY"], CAP)
        self.assertGreater(39 * self.state["prices"]["SPY"], CAP)

    def test_a_short_is_trimmed_toward_zero_not_through_it(self):
        result = actions(CONFIG, measure(CONFIG, [position("AAA", -40)], EQUITY, EQUITY, EQUITY))
        self.assertEqual([result["target_shares"]["AAA"], result["share_changes"]["AAA"]], [-38, 2])

    def test_a_protected_name_is_only_alerted_about(self):
        result = actions(CONFIG, measure(CONFIG, [position("DISC", 40)], EQUITY, EQUITY, EQUITY),
                         protected=["DISC"])
        self.assertEqual([result["target_shares"], result["share_changes"], result["orders_required"]],
                         [{"DISC": 40}, {}, 0])
        self.assertEqual(result["protected_skipped"], ["DISC"])
        self.assertIn("is not a strategy position", result["slack_report"][0])
        self.assertIn("alert only", result["slack_report"][0])


class Delever(unittest.TestCase):
    def spread(self, names: int, shares: int = -100, price: float = 100.0) -> list[dict]:
        """A book of equal shorts, each far below the trim threshold, so only margin can trigger."""
        return [position(f"B{i:02d}", shares, price) for i in range(names)]

    def test_margin_above_the_alert_is_scaled_to_the_cap(self):
        state = measure(CONFIG, self.spread(20), EQUITY, EQUITY, EQUITY)
        self.assertAlmostEqual(state["margin"], 1.2, places=12)
        self.assertEqual(state["over_trim"], [])
        result = actions(CONFIG, state)
        self.assertFalse(result["halt"])
        self.assertAlmostEqual(result["delever_scale"], CONFIG["delever_to"] / state["margin"], places=12)
        self.assertEqual(set(result["target_shares"].values()), {-75})
        self.assertEqual(result["orders_required"], 20)
        self.assertAlmostEqual(result["margin_after"], CONFIG["delever_to"], places=9)
        self.assertIn(f"margin {state['margin']:.1%} above 95%", result["slack_report"][0])

    def test_the_alert_starts_above_ninety_five_percent(self):
        for names, expect_action in ((15, False), (16, True)):
            with self.subTest(names=names):
                state = measure(CONFIG, self.spread(names), EQUITY, EQUITY, EQUITY)
                result = actions(CONFIG, state)
                self.assertEqual(result["delever_scale"] < 1.0, expect_action)
                self.assertEqual(result["orders_required"], names if expect_action else 0)

    def test_a_protected_book_the_strategy_may_not_sell_stays_over_the_cap(self):
        state = measure(CONFIG, self.spread(20) + [position("DISC", -60)], EQUITY, EQUITY, EQUITY)
        self.assertGreater(state["margin"], CONFIG["delever_margin_above"])
        result = actions(CONFIG, state, protected=["DISC"])
        self.assertGreater(result["margin_after"], CONFIG["delever_to"])
        self.assertEqual(result["target_shares"]["DISC"], -60)
        self.assertEqual(result["protected_skipped"], ["DISC"])
        self.assertIn("protected positions are not tradable by the strategy", result["slack_report"][-1])


class Halt(unittest.TestCase):
    def positions(self):
        return [position("SPY", 10), position("AAA", -4, 50.0), position("BBB", -6, 50.0)]

    def state(self, equity=92_000.0):
        return measure(CONFIG, self.positions(), equity, EQUITY, 100_000.0)

    def test_eight_percent_from_the_peak_flattens_everything_the_strategy_owns(self):
        result = actions(CONFIG, self.state())
        self.assertAlmostEqual(self.state()["drawdown"], -0.08, places=12)
        self.assertTrue(result["halt"] and result["flatten"])
        self.assertEqual(result["delever_scale"], 1.0)
        self.assertEqual([result["target_shares"], result["orders_required"], result["margin_after"]],
                         [{"SPY": 0, "AAA": 0, "BBB": 0}, 3, 0.0])
        self.assertIn("flatten everything and halt.", result["slack_report"][0])
        self.assertEqual(result["share_changes"]["AAA"], 4)

    def test_the_halt_line_is_the_boundary_and_recovery_clears_it(self):
        for equity, expected in ((92_000.0, True), (92_000.01, False)):
            with self.subTest(equity=equity):
                self.assertEqual(actions(CONFIG, self.state(equity))["halt"], expected)

    def test_the_halt_overrides_the_trim_and_the_margin_cut(self):
        state = measure(CONFIG, [position("SPY", -160), position("QQQ", -160)], 92_000.0, EQUITY, 100_000.0)
        self.assertGreater(state["margin"], CONFIG["delever_margin_above"])
        self.assertEqual(state["over_trim"], ["QQQ", "SPY"])
        result = actions(CONFIG, state)
        self.assertTrue(result["halt"])
        self.assertEqual([result["delever_scale"], set(result["target_shares"].values())], [1.0, {0}])
        self.assertEqual(actions(CONFIG, state, protected=["SPY"])["target_shares"], {"SPY": -160, "QQQ": 0})

    def test_flatten_leaves_protected_names_and_zeros_the_rest(self):
        state = self.state(92_000.0)
        self.assertEqual(flatten(state), {"SPY": 0, "AAA": 0, "BBB": 0})
        self.assertEqual(flatten(state, protected=["AAA"]), {"SPY": 0, "BBB": 0})
        self.assertEqual(flatten(measure(CONFIG, [], EQUITY, EQUITY, EQUITY)), {})

    def test_the_result_is_always_readable_by_the_planner(self):
        result = actions(CONFIG, self.state(), protected=["SPY"])
        self.assertIsInstance(result["halt"], bool)
        self.assertIsInstance(result["slack_report"], list)
        self.assertIsInstance(result["target_shares"], dict)
        self.assertIsInstance(result["protected_skipped"], list)
        self.assertTrue(finite(result["margin_after"]) and finite(result["delever_scale"]))
        self.assertEqual([result["target_shares"]["SPY"], result["share_changes"], result["orders_required"]],
                         [10, {"AAA": 4, "BBB": 6}, 2])


if __name__ == "__main__":
    unittest.main()
