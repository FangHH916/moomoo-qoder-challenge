import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from live.contracts import SCHEMA_VERSION, STRATEGY_ID, digest, frozen_factor_names, load_config, seal
from rules.trading_calendar import completed_session, target_age, trading_window
from rules.gate import evaluate_order, order_fingerprint

EXAMPLE = Path(__file__).with_name("example-order-legacy.json")


def target_payload(config, snap, **overrides):
    """A READY target for the frozen short-only book, with every provenance field the gate requires."""
    body = {"schema_version": SCHEMA_VERSION, "strategy_id": STRATEGY_ID, "mode": "test", "status": "READY",
            "generated_at": "2026-11-19T22:30:00Z", "data_timestamp": "2026-11-19T21:00:00Z",
            "signal_date": "2026-11-19", "weights": {"AAA": -0.035},
            "spy_hedge_weight": 0.0, "qqq_hedge_weight": 0.0, "vol_multiplier": 1.0,
            "model_coefficients": dict.fromkeys(frozen_factor_names(config), 0.01),
            "model_carried_forward": False, "model_retrained_today": True, "model_fit_date": "2026-11-19",
            "signal_guard": {"passed": True, "distinct_values": 118},
            "sizing_equity": 100000, "rebalance_due": True,
            "config_checksum": digest(config), "position_snapshot_checksum": digest(snap)}
    return seal({**body, **overrides})


def fixture():
    config = load_config()
    snap = {"mode": "test", "account_id": "5322438", "account_type": "SIMULATE", "equity": 100000,
            "initial_equity": 100000, "previous_close_equity": 100000, "peak_equity": 100000,
            "as_of": "2026-11-20T14:35:00Z", "positions": []}
    targets = target_payload(config, snap)
    order = {"account_id": "5322438", "symbol": "AAA", "side": "SELL", "quantity": 35, "limit_price": 100,
             "order_type": "LIMIT", "time_in_force": "DAY", "session": "RTH", "intent": "REBALANCE",
             "client_order_id": "test-order-1", "workflow": "paper-trading-gate"}
    context = {"now": "2026-11-20T14:35:00Z", "mode": "test", "account_id": "5322438", "snapshot": snap,
               "targets": targets, "expected_target_checksum": targets["checksum"], "guard_session_id": "guard-fixture",
               "quotes": {"AAA": {"price": 100, "beta": 1, "as_of": snap["as_of"], "verified_by_relay": True}},
               "eligibility": {"AAA": {"market": "US", "security_type": "COMMON_STOCK", "market_cap": 1e9,
                   "median_dollar_volume_20d": 1e7, "shortable": True, "easy_to_borrow": True, "halted": False,
                   "earnings_clear_2_sessions": True, "as_of": snap["as_of"], "verified_by_relay": True}},
               "open_orders": [], "executed_order_ids": [], "seen_client_order_ids": [], "reconciled": True,
               "snapshot_verified_by_relay": True, "release_verified": True, "lease_verified": True, "daily_entry_halt": False,
               "permission_policy_verified": True, "scout": {"AAA": "PASS"},
               "kill_switch": {"halted": False, "verified_by_relay": True}, "run_id": "fixture-only"}
    approve(order, context, config)
    return order, context, config


def approve(order, context, config):
    context["approved_plan_orders"] = [copy.deepcopy(order)]
    context["plan_checksum"] = digest(context["approved_plan_orders"])
    context["auditor_approval"] = {"approval_id": "audit1", "verified_by_relay": True, "decision": "APPROVE",
        "issued_at": context["now"], "expires_at": context["now"][:14] + "39:00Z",
        "session_id": "independent-auditor", "mode": context["mode"], "account_id": str(context["account_id"]),
        "target_checksum": context["targets"]["checksum"], "snapshot_checksum": digest(context["snapshot"]),
        "plan_checksum": context["plan_checksum"], "config_checksum": digest(config)}
    context["human_approvals"] = [{"verified_by_relay": True, "decision": "APPROVE", "issued_at": context["now"],
        "expires_at": context["now"][:14] + "39:00Z", "account_id": str(context["account_id"]), "mode": context["mode"],
        "order_fingerprint": order_fingerprint(order, context), "consumed": False}]


class GateTests(unittest.TestCase):
    def setUp(self):
        self.order, self.context, self.config = fixture()

    def result(self):
        return evaluate_order(self.order, self.context, self.config)

    def blocked(self, rule):
        self.assertIn(rule, self.result()["failed_rules"])

    def only(self, rule):
        self.assertEqual(self.result()["failed_rules"], [rule], self.result())

    def rebind(self):
        """Re-issue the checksum-bound approvals after mutating the snapshot or targets."""
        approve(self.order, self.context, self.config)

    def set_targets(self, **updates):
        payload = {k: v for k, v in self.context["targets"].items() if k != "checksum"}
        self.context["targets"] = seal({**payload, **updates})
        self.context["expected_target_checksum"] = self.context["targets"]["checksum"]
        self.rebind()

    def short_book(self, names, quantity=1, beta=0.0):
        self.context["snapshot"]["positions"] = [{"symbol": name, "quantity": -quantity, "price": 100, "beta": beta}
                                                 for name in names]
        self.rebind()

    def hedge_context(self, instrument="SPY", beta=1.0):
        """Broker rows that classify an ETF as a usable hedge leg for the short book."""
        self.context["eligibility"][instrument] = {"market": "US", "security_type": "ETF", "market_cap": 5e11,
            "median_dollar_volume_20d": 1e9, "shortable": True, "easy_to_borrow": True, "halted": False,
            "earnings_clear_2_sessions": True, "as_of": self.context["now"], "verified_by_relay": True}
        self.context["quotes"][instrument] = {"price": 100, "beta": beta, "as_of": self.context["now"],
                                              "verified_by_relay": True}
        self.context["scout"][instrument] = "PASS"

    def hedge_order(self, instrument="SPY", side="BUY", quantity=20, weight=0.02, field="spy_hedge_weight"):
        self.hedge_context(instrument)
        self.order.update(symbol=instrument, side=side, quantity=quantity, purpose="BETA_HEDGE")
        self.set_targets(**{field: weight})

    def test_complete_gate_is_eligible_but_execution_inactive(self):
        result = self.result()
        self.assertTrue(result["eligible"], result)
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(len(result["checks"]), 21)

    def test_shipped_example_fixture_still_evaluates_eligible(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        result = evaluate_order(data["order"], data["context"], load_config())
        self.assertTrue(result["eligible"], result["failed_rules"])
        self.assertFalse(result["execution_allowed"])

    def test_r1_wrong_account_and_real_account(self):
        for account in ("5832812", "1", "", None):
            with self.subTest(account=account):
                self.order["account_id"] = account
                self.blocked("R1")
        self.order["account_id"] = "5322438"
        self.context["snapshot"]["account_type"] = "REAL"
        self.blocked("R1")

    def test_r2_short_eligibility_boundaries_and_borrow(self):
        e = self.context["eligibility"]["AAA"]
        for key, invalid in (("market_cap", 499999999), ("median_dollar_volume_20d", 4999999), ("halted", True),
                             ("earnings_clear_2_sessions", None), ("verified_by_relay", False), ("market", "HK")):
            with self.subTest(key=key):
                old = e[key]; e[key] = invalid
                self.blocked("R2"); e[key] = old
        # The frozen book is short-only, so borrow and the $1B short market-cap floor always apply.
        for key, invalid in (("easy_to_borrow", False), ("shortable", False), ("market_cap", 999999999)):
            with self.subTest(key=key):
                old = e[key]; e[key] = invalid
                self.blocked("R2"); e[key] = old

    def test_r2_hedge_etf_needs_the_beta_hedge_purpose(self):
        self.hedge_order(quantity=20, weight=0.02)
        self.order.pop("purpose")
        self.rebind()
        self.only("R2")
        self.order["purpose"] = "BETA_HEDGE"
        self.rebind()
        self.assertTrue(self.result()["eligible"], self.result()["failed_rules"])

    def test_r3_current_and_initial_equity_caps(self):
        self.order["quantity"] = 101
        self.blocked("R3")
        self.order["quantity"] = 80
        self.context["snapshot"]["initial_equity"] = 10000
        self.rebind()
        self.blocked("R3")

    def test_r3_hedge_etf_cap_is_a_share_of_initial_equity(self):
        self.hedge_order(quantity=10, weight=0.38)
        self.context["snapshot"]["positions"] = [{"symbol": "SPY", "quantity": 370, "price": 100, "beta": 1.0}]
        self.rebind()
        self.assertNotIn("R3", self.result()["failed_rules"], self.result())
        self.order["quantity"] = 20
        self.rebind()
        self.blocked("R3")

    def test_r4_gross_short_ceiling_counts_pending_orders(self):
        self.context["open_orders"] = [{"order_id": f"s{i}", "symbol": f"B{i}", "side": "SELL",
            "remaining_quantity": 100, "limit_price": 100, "status": "OPEN", "submitted_at": self.context["now"],
            "reduce_only_verified": False} for i in range(4)]
        for i in range(4):
            self.context["quotes"][f"B{i}"] = {"price": 100, "beta": 0.0, "as_of": self.context["now"],
                                               "verified_by_relay": True}
        self.blocked("R4")

    def test_r4_long_side_must_fit_inside_hedge_etf_caps(self):
        self.context["open_orders"] = [{"order_id": "p", "symbol": "SPY", "side": "BUY", "remaining_quantity": 800,
            "limit_price": 100, "status": "OPEN", "submitted_at": self.context["now"]}]
        self.context["quotes"]["SPY"] = {"price": 100, "beta": 1.0, "as_of": self.context["now"],
                                         "verified_by_relay": True}
        self.blocked("R4")
        self.blocked("R3")

    def test_r5_unknown_and_excess_beta(self):
        self.context["quotes"]["AAA"]["beta"] = 4
        self.blocked("R5")
        self.context["quotes"]["AAA"]["beta"] = None
        self.blocked("R5")

    def test_r6_reserve_and_position_cap(self):
        self.context["executed_order_ids"] = [str(i) for i in range(59)]
        self.blocked("R6")
        self.context["executed_order_ids"].pop()
        self.assertNotIn("R6", self.result()["failed_rules"])
        self.short_book([f"P{i:03d}" for i in range(100)])
        self.order["quantity"] = 1
        self.set_targets(weights={"AAA": -0.001})
        self.blocked("R6")

    def test_r7_limit_band_and_market(self):
        self.order["order_type"] = "MARKET"
        self.blocked("R7")
        self.order["order_type"] = "LIMIT"; self.order["limit_price"] = 100.26
        self.blocked("R7")

    def test_r7_undeclared_or_extended_time_in_force_rejected(self):
        # The discovered paper order schema exposes no TIF field, so only an explicitly DAY/RTH order is even eligible.
        self.order.pop("time_in_force")
        self.blocked("R7")
        self.order["time_in_force"] = "GTC"
        self.blocked("R7")
        self.order["time_in_force"] = "DAY"
        self.order["session"] = "RTH+PRE+POST"
        self.blocked("R7")

    def test_r8_daily_loss_boundary(self):
        self.context["snapshot"]["equity"] = 98000
        self.rebind()
        self.blocked("R8")

    def test_r8_daily_halt_survives_recovery(self):
        self.context["daily_entry_halt"] = True
        self.blocked("R8")

    def test_r9_drawdown_boundary(self):
        self.context["snapshot"]["equity"] = 92000
        self.rebind()
        self.blocked("R9")

    def test_r10_checksum_stale_and_future(self):
        self.context["targets"]["weights"]["AAA"] = -.04
        self.blocked("R10")
        for day in ("2026-11-12", "2026-11-20"):
            self.set_targets(signal_date=day)
            self.blocked("R10")

    def test_r10_rejects_targets_the_frozen_contract_refuses(self):
        for name, updates in (
            ("all-zero coefficients", {"model_coefficients": dict.fromkeys(frozen_factor_names(self.config), 0.0)}),
            ("wrong factor set", {"model_coefficients": {n: 0.01 for n in frozen_factor_names(self.config)[:-1]}}),
            ("single-stock long weight", {"weights": {"AAA": 0.035}}),
            ("missing guard", {"signal_guard": {"passed": False, "distinct_values": 4}}),
            ("carried forward from today", {"model_carried_forward": True}),
            ("too many shorts", {"weights": {f"B{i}": -0.001 for i in range(21)}}),
            ("gross short breach", {"weights": {f"B{i}": -0.03 for i in range(20)}}),
            ("v1 schema", {"schema_version": 1}),
        ):
            with self.subTest(case=name):
                self.set_targets(**updates)
                self.blocked("R10")

    def test_r11_missing_and_future_quote(self):
        self.context["quotes"]["AAA"]["as_of"] = "2026-11-20T14:36:00Z"
        self.blocked("R11")
        self.context["quotes"] = {}
        self.blocked("R11")

    def test_r12_duplicate_unknown_partial_orders(self):
        self.context["seen_client_order_ids"] = [self.order["client_order_id"]]
        self.blocked("R12")
        self.context["seen_client_order_ids"] = []
        self.context["open_orders"] = [{"order_id": "p", "symbol": "AAA", "side": "BUY", "remaining_quantity": 5,
            "limit_price": 100, "status": "PARTIAL", "submitted_at": self.context["now"]}]
        self.blocked("R12")

    def test_r13_holiday_halfday_and_end(self):
        for day in ("2026-11-26", "2026-11-27", "2026-11-28"):
            self.context["now"] = day + "T14:35:00Z"
            self.blocked("R13")

    def test_r14_auditor_same_session_expired_and_modified(self):
        self.context["auditor_approval"]["session_id"] = self.context["guard_session_id"]
        self.blocked("R14")
        self.context["auditor_approval"]["session_id"] = "other"
        self.context["auditor_approval"]["expires_at"] = self.context["now"]
        self.blocked("R14")

    def test_r15_approval_required_exact_and_expired(self):
        self.context["human_approvals"] = []
        self.blocked("R15")
        self.rebind()
        self.order["quantity"] += 1
        self.blocked("R15")

    def test_r16_manual_halt(self):
        self.context["kill_switch"]["halted"] = True
        self.blocked("R16")

    def test_r17_release_lease_permission(self):
        for key in ("release_verified", "lease_verified", "permission_policy_verified"):
            self.context[key] = False; self.blocked("R17"); self.context[key] = True

    def test_r18_veto_target_and_plan(self):
        self.context["scout"]["AAA"] = "HOLD"
        self.blocked("R18")
        self.context["scout"]["AAA"] = "PASS"
        self.context["approved_plan_orders"] = []
        self.blocked("R18")

    def test_r18_order_size_must_move_toward_the_published_target(self):
        self.order["quantity"] = 71
        self.rebind()
        self.only("R18")

    def test_r19_single_stock_long_is_blocked(self):
        self.order.update(side="BUY")
        self.rebind()
        result = self.result()
        self.assertIn("R19", result["failed_rules"], result)
        self.assertIn("R18", result["failed_rules"], result)

    def test_r19_hedge_etf_long_is_allowed(self):
        self.hedge_order(quantity=20, weight=0.02)
        result = self.result()
        self.assertTrue(result["eligible"], result["failed_rules"])

    def test_r19_qqq_hedge_uses_its_own_beta_field(self):
        self.hedge_order("QQQ", quantity=20, weight=0.02, field="qqq_hedge_weight")
        self.assertTrue(self.result()["eligible"], self.result()["failed_rules"])
        self.context["eligibility"]["QQQ"]["security_type"] = "COMMON_STOCK"
        self.blocked("R2")

    def test_r20_margin_cap_counts_both_sides(self):
        snap = self.context["snapshot"]
        snap.update(equity=25000, previous_close_equity=25000, peak_equity=25000)
        snap["positions"] = [{"symbol": "SPY", "quantity": 370, "price": 100, "beta": 0.05},
                             {"symbol": "QQQ", "quantity": 370, "price": 100, "beta": 0.05}]
        self.order["quantity"] = 20
        self.set_targets(weights={"AAA": -0.08})
        self.only("R20")

    def test_r21_carry_forward_provenance(self):
        self.set_targets(model_carried_forward=True, model_retrained_today=False, model_fit_date="2026-11-12")
        self.assertTrue(self.result()["eligible"], self.result()["failed_rules"])
        for name, updates in (
            ("all-zero coefficients", {"model_coefficients": dict.fromkeys(frozen_factor_names(self.config), 0.0)}),
            ("failed signal guard", {"signal_guard": {"passed": False, "distinct_values": 4}}),
            ("non-boolean carry flag", {"model_carried_forward": None}),
        ):
            with self.subTest(case=name):
                self.set_targets(**updates)
                self.blocked("R21")

    def test_r21_only_20_short_names_may_be_published(self):
        self.set_targets(weights={f"B{i}": -0.017 for i in range(20)})
        self.assertNotIn("R21", self.result()["failed_rules"], self.result())
        self.set_targets(weights={f"B{i}": -0.016 for i in range(21)})
        self.blocked("R21")

    def test_r18_and_r21_refuse_a_signal_guard_hold_target(self):
        self.set_targets(status="HOLD", signal_guard={"passed": False, "distinct_values": 3})
        self.only("R18")
        self.context["snapshot"]["positions"] = [{"symbol": "AAA", "quantity": -35, "price": 100, "beta": 1}]
        self.order.update(side="BUY", intent="EMERGENCY_EXIT")
        self.rebind()
        self.assertTrue(self.result()["eligible"], self.result()["failed_rules"])

    def test_nonfinite_and_boolean_qty_fail_closed(self):
        for value in (True, -1, 0, 0.5, float("nan")):
            self.order["quantity"] = value
            self.blocked("R0")

    def test_emergency_is_not_permission_to_increase(self):
        self.order["intent"] = "EMERGENCY_EXIT"
        self.context["kill_switch"]["halted"] = True
        self.blocked("R16")

    def test_authorised_halfday_emergency_exit_can_pass(self):
        self.context["now"] = "2026-11-27T17:35:00Z"
        snap = self.context["snapshot"]
        snap.update(as_of=self.context["now"], equity=92000)
        snap["positions"] = [{"symbol": "AAA", "quantity": -35, "price": 100, "beta": 1}]
        self.context["quotes"]["AAA"]["as_of"] = self.context["now"]
        self.context["kill_switch"]["halted"] = True
        self.order.update(side="BUY", intent="EMERGENCY_EXIT")
        self.rebind()
        result = self.result()
        self.assertTrue(result["eligible"], result)
        self.assertFalse(result["execution_allowed"])
        self.context["human_approvals"] = []
        self.blocked("R15")

    def test_calendar_uses_completed_exchange_sessions(self):
        now = datetime(2026, 11, 27, 14, 35, tzinfo=timezone.utc)
        self.assertEqual(completed_session(now), "2026-11-25")
        self.assertEqual(target_age("2026-11-24", now), 1)
        self.assertTrue(trading_window(datetime(2026, 11, 27, 17, 45, tzinfo=timezone.utc), True))
        self.assertFalse(trading_window(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc), True))


if __name__ == "__main__":
    unittest.main()
