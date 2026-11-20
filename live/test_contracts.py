import copy
import tempfile
import unittest
from pathlib import Path

from live.contracts import seal, strict_json, timestamp, validate_targets, verify, write_targets
from rules.test_gate import fixture


class ContractTests(unittest.TestCase):
    def test_canonical_hash_order_independent(self):
        self.assertEqual(seal({"a": 1, "b": 2}), seal({"b": 2, "a": 1}))
        self.assertFalse(verify({"a": 1, "checksum": "incorrect"}))

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        for text in ('{"symbol":1,"symbol":2}', '{"price":NaN}', '{"price":Infinity}'):
            with self.assertRaises(ValueError):
                strict_json(text)

    def test_timestamp_must_be_aware(self):
        with self.assertRaises(ValueError):
            timestamp("2026-11-19T09:00:00")

    def frozen_target(self):
        return fixture()[1]["targets"]

    def test_frozen_factor_set_required_for_ready(self):
        artifact = self.frozen_target()
        artifact["model_coefficients"].pop("amihud")
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))

    def test_all_zero_coefficients_are_refused(self):
        artifact = self.frozen_target()
        artifact["model_coefficients"] = {name: 0.0 for name in artifact["model_coefficients"]}
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))

    def test_carried_forward_coefficients_must_predate_the_signal(self):
        artifact = self.frozen_target()
        artifact["model_carried_forward"] = True
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))
        artifact["model_fit_date"] = "2026-11-12"
        validate_targets(seal(artifact))

    def test_ready_requires_a_passed_signal_guard(self):
        artifact = self.frozen_target()
        artifact["signal_guard"] = {"passed": False, "distinct_values": 4}
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))

    def test_no_single_stock_long_and_no_extra_shorts(self):
        for mutate in (lambda a: a["weights"].__setitem__("AAA", 0.035),
                       lambda a: a["weights"].update({f"B{i}": -0.01 for i in range(21)})):
            artifact = self.frozen_target()
            mutate(artifact)
            with self.assertRaises(ValueError):
                validate_targets(seal(artifact))

    def test_hedge_weight_is_a_separate_field(self):
        artifact = self.frozen_target()
        artifact["weights"]["SPY"] = -0.02
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))
        artifact["weights"].pop("SPY")
        artifact["spy_hedge_weight"] = 1.4
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))

    def test_gross_short_ceiling_is_enforced_at_a_rebalance(self):
        artifact = self.frozen_target()
        artifact["weights"] = {f"B{i}": -0.04 for i in range(20)}
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))
        artifact["rebalance_due"] = False
        validate_targets(seal(artifact))

    def test_synthetic_fixture_targets_cannot_name_the_competition_account(self):
        artifact = self.frozen_target()
        artifact["fixture"] = True
        validate_targets(seal(artifact))
        artifact["mode"] = "competition"
        with self.assertRaises(ValueError):
            validate_targets(seal(artifact))

    def test_output_is_immutable_and_repeatable(self):
        artifact = self.frozen_target()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "targets.json"
            write_targets(path, artifact)
            write_targets(path, artifact)
            artifact["weights"]["AAA"] = -.04
            with self.assertRaises(FileExistsError):
                write_targets(path, seal(artifact))


if __name__ == "__main__":
    unittest.main()
