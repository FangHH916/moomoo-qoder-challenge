"""Boundary tests for the P* rule gate (`rules/gate_pstar.py`), one frozen rule at a time.

The fixture is the committed `r14_targets_2026-11-18.json` research run converted by the production path
into a sealed v3 target book and planned against a synthetic account (`rules/pstar_harness.py`). Each test
either names the rule that must block or asserts that the plan line stays eligible. Nothing can execute:
`execution_enabled` is false in the shipped configuration and this module has no broker client.
"""
from __future__ import annotations

import json
import unittest
from datetime import timedelta
from pathlib import Path

from live.contracts import digest, load_config, load_strategy_config, timestamp, validate_targets
from rules.gate import evaluate_order
from rules.pstar_harness import ACCOUNTS, EQUITY, Harness, config_for, owned
from rules.trading_calendar import target_age

EXAMPLE = Path(__file__).with_name("example-order-pstar.json")
LEGACY_EXAMPLE = Path(__file__).with_name("example-order-legacy.json")
FAIL = "failed_rules"


def shares_above(value: float, price: float) -> int:
    """The smallest whole share count whose value exceeds `value` at `price`.

    The cap and trim tests need a holding that is measurably over a threshold. Naming it in shares would
    tie the test to one book's weights, so it is computed from the fixture price of the name instead.
    """
    return int(value / price) + 1


def trim_shares(harness: Harness, symbol: str = "SPY") -> int:
    """Shares of `symbol` worth just more than the trim threshold of initial assets."""
    return shares_above(harness.config["trim_ticker_above_of_initial"]
                        * harness.snapshot["initial_equity"], harness.prices[symbol])


def shorts_of(harness: Harness) -> list[str]:
    return [name for name, weight in harness.targets["weights"].items() if weight < 0]


def longs_of(harness: Harness) -> list[str]:
    return [name for name, weight in harness.targets["weights"].items() if weight > 0]


def cover_of(order: dict, **updates) -> dict:
    """A reduce-only order for the same name: the shape every frozen rule may always trade."""
    return dict(order, side="BUY" if order["side"] == "SELL" else "SELL", intent="EXIT",
                client_order_id=f"cover-{order['symbol']}", **updates)


class EligiblePath(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.config = self.harness.config

    def test_a_plan_line_is_eligible_but_execution_is_inactive(self):
        result = self.harness.result(0)
        self.assertTrue(result["eligible"], result[FAIL])
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(result["action"], "ALLOW")
        self.assertEqual(result["strategy"], "pstar")
        self.assertEqual(result["variant"], "pstar15")
        self.assertEqual(len(result["checks"]), 21)
        self.assertTrue(all(row["passed"] for row in result["checks"]), result)

    def test_every_plan_line_of_the_go_live_batch_is_eligible(self):
        for index in range(len(self.harness.plan["orders"])):
            with self.subTest(index=index):
                result = self.harness.result(index)
                self.assertTrue(result["eligible"], result[FAIL])

    def test_the_go_live_plan_is_one_entry_batch_of_the_frozen_book(self):
        plan, counts = self.harness.plan, self.harness.plan["counts"]
        self.assertTrue(plan["entry_batch"], plan["note"])
        self.assertEqual({row["intent"] for row in plan["orders"]}, {"ENTRY"})
        self.assertEqual(counts["orders"], len(self.harness.targets["weights"]))
        self.assertEqual(len(shorts_of(self.harness)), self.config["n_short"])
        self.assertTrue(set(longs_of(self.harness)) <= set(self.config["etf_universe"]))
        self.assertLessEqual(counts["positions_after"], self.config["max_positions"])
        self.assertLessEqual(counts["orders"], self.config["max_orders_per_day"])
        self.assertLessEqual(counts["orders"] + counts["positions_after"],
                             self.config["competition_order_cap"])

    def test_the_shipped_example_fixture_reproduces_an_eligible_evaluation(self):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        result = evaluate_order(data["order"], data["context"], load_strategy_config())
        self.assertTrue(result["eligible"], result[FAIL])
        self.assertFalse(result["execution_allowed"])

    def test_the_deployed_strategy_is_the_default_of_the_gate(self):
        result = evaluate_order(self.harness.rebind(), self.harness.context)
        self.assertEqual(result["variant"], self.config["variant"])
        self.assertTrue(result["eligible"], result[FAIL])

    def test_an_incomplete_input_contract_is_refused(self):
        for mutation, label in (
                (lambda h: h.context["snapshot"].update(equity=None), "no equity"),
                (lambda h: h.context["snapshot"].update(positions=[{"symbol": "SPY", "quantity": 1.5,
                                                                    "price": 10.0}]), "fractional share"),
                (lambda h: h.context["snapshot"].pop("peak_equity"), "no peak"),
                (lambda h: h.context["snapshot"].pop("initial_equity"), "no initial equity")):
            with self.subTest(case=label):
                harness = Harness()
                order = harness.order(0)
                harness.rebind(order)
                mutation(harness)
                self.assertIn("R0", evaluate_order(order, harness.context, harness.config)[FAIL])
        harness = Harness()
        harness.rebind()
        self.assertIn("R0", evaluate_order(dict(harness.order(0), quantity=0), harness.context,
                                          harness.config)[FAIL])


class AccountAndInstrument(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()

    def test_r1_account_identity(self):
        for account in ("5832812", "1", "", None):
            with self.subTest(account=account):
                order = dict(self.harness.order(0), account_id=str(account))
                self.assertIn("R1", self.harness.check_order(order)[FAIL])
        harness = Harness()
        harness.snapshot["account_type"] = "REAL"
        self.assertIn("R1", harness.result(0)[FAIL])

    def test_r2_etf_fund_size_below_the_competition_limit(self):
        harness = Harness()
        harness.eligibility["SPY"]["market_value"] = 4e8
        harness.rebuild_plan()
        self.assertNotIn("SPY", harness.plan_symbols())
        self.assertIn("SPY", harness.eligibility_result["blocked"])
        order = dict(harness.order(0), symbol="SPY", side="BUY", intent="ENTRY", quantity=1,
                     limit_price=harness.prices["SPY"], client_order_id="r2-spy")
        self.assertEqual(order["quantity"], 1)
        self.assertIn("R2", harness.check_order(order)[FAIL])

    def test_r2_instrument_type_and_broker_record(self):
        symbol = self.harness.order(0)["symbol"]
        for field, value in (("security_type", "ETF"), ("verified_by_relay", False),
                             ("halted", True), ("market", "HK")):
            with self.subTest(field=field):
                harness = Harness()
                harness.eligibility[symbol][field] = value
                harness.context["eligibility"] = harness.eligibility
                harness.rebind()
                self.assertIn("R2", evaluate_order(harness.order(0), harness.context,
                                                   harness.config)[FAIL])

    def test_r2_an_etf_under_one_dollar_is_screened_before_the_planner_runs(self):
        harness = Harness()
        for record in (harness.quotes["UUP"], harness.eligibility["UUP"]):
            record["price"] = 0.5
        harness.rebuild_plan()
        self.assertIn("UUP", harness.eligibility_result["blocked"])
        self.assertNotIn("UUP", harness.plan_symbols())
        self.assertTrue(any("UUP skipped" in line for line in harness.eligibility_result["slack_report"]))

    def test_r2_the_gate_still_refuses_a_sub_dollar_name_the_planner_proposed(self):
        harness = Harness(lots=100)      # the smallest shorts fall below the competition's $1 price floor
        cheap = min(harness.prices, key=lambda name: harness.prices[name])
        self.assertLess(harness.prices[cheap], harness.config["min_price"])
        self.assertIn(cheap, harness.plan_symbols())
        self.assertIn("R2", harness.check_order(harness.entry_order(cheap))[FAIL])

    def test_r2_a_reduce_only_order_needs_no_new_eligibility(self):
        harness = Harness()
        symbol = shorts_of(harness)[0]
        harness.place([(symbol, -5)])
        harness.eligibility[symbol].update(security_type="ETF", market_value=4e8)
        cover = harness.order(symbol=symbol)
        self.assertEqual(cover["side"], "BUY")
        result = harness.check_order(cover)
        self.assertNotIn("R2", result[FAIL])
        self.assertTrue(result["eligible"], result[FAIL])


class CompositionAndCaps(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(lots=10)
        self.config = self.harness.config

    def test_r3_no_ticker_above_38_percent_of_initial_assets(self):
        spy = dict(self.harness.order(symbol="SPY"),
                   quantity=shares_above(self.config["ticker_cap_of_initial"]
                                         * self.harness.snapshot["initial_equity"],
                                         self.harness.prices["SPY"]))
        result = self.harness.check_order(spy)
        self.assertIn("R3", result[FAIL])
        observed = next(row for row in result["checks"] if row["id"] == "R3")["observed"]
        self.assertGreater(observed["this_ticker"], self.config["ticker_cap_of_initial"])
        self.assertIn("SPY", observed["over_cap"])

    def test_r3_a_plan_line_stays_inside_the_cap(self):
        result = self.harness.result(symbol="SPY")
        self.assertTrue(result["eligible"], result[FAIL])
        observed = next(row for row in result["checks"] if row["id"] == "R3")["observed"]
        self.assertLessEqual(observed["this_ticker"], self.config["ticker_cap_of_initial"])

    def test_r4_a_long_must_be_a_sleeve_etf_or_a_whitelisted_name(self):
        order = dict(self.harness.order(0), symbol="NVDA", side="BUY", intent="ENTRY", quantity=10,
                     limit_price=100.0, client_order_id="r4-long")
        self.assertIn("R4", self.harness.check_order(order)[FAIL])
        config = dict(self.config, discretionary_symbols=["NVDA"])
        self.assertNotIn("R4", self.harness.check_order(order, config=config)[FAIL])

    def test_r4_a_short_must_be_in_the_current_target_book(self):
        order = dict(self.harness.order(0), symbol="NVDA", side="SELL", intent="ENTRY", quantity=10,
                     limit_price=100.0, client_order_id="r4-short")
        result = self.harness.check_order(order)
        self.assertIn("R4", result[FAIL])
        observed = next(row for row in result["checks"] if row["id"] == "R4")["observed"]
        self.assertEqual(observed["requested_shorts"], len(shorts_of(self.harness)))

    def test_r4_the_short_name_count_stays_at_the_frozen_seventy_seven(self):
        shorts, etf = shorts_of(self.harness), longs_of(self.harness)[0]
        held = [(name, -10) for name in shorts]
        self.harness.place(held)
        order = self.harness.order(symbol=etf)
        self.assertNotIn("R4", self.harness.check_order(order)[FAIL])
        self.harness.place(held + [("EXTRA", -10)], unowned=["EXTRA"])
        result = self.harness.check_order(order)
        self.assertIn("R4", result[FAIL])
        self.assertGreater(next(row for row in result["checks"] if row["id"] == "R4")["observed"]["shorts"],
                           self.config["n_short"])

    def test_r5_borrow_and_market_cap_of_a_short(self):
        symbol = self.harness.order(0)["symbol"]
        for field, value in (("shortable", False), ("easy_to_borrow", False), ("market_cap", 5e8)):
            with self.subTest(field=field):
                harness = Harness(lots=10)
                harness.eligibility[symbol][field] = value
                harness.context["eligibility"] = harness.eligibility
                harness.rebind()
                self.assertIn("R5", evaluate_order(harness.order(0), harness.context,
                                                   harness.config)[FAIL])

    def test_r5_an_un_borrowable_name_is_skipped_and_never_replaced(self):
        blocked = shorts_of(self.harness)[:3]
        harness = Harness(blocked=blocked, lots=10)
        self.assertFalse(harness.plan_symbols() & set(blocked))
        self.assertEqual(len([name for name in harness.plan_symbols()
                              if harness.targets["weights"][name] < 0]),
                         len(shorts_of(harness)) - len(blocked))
        self.assertTrue(any("Not replaced" in line for line in harness.plan["slack_report"]))

    def test_r5_a_cover_of_an_un_borrowable_short_still_trades(self):
        symbol = shorts_of(self.harness)[0]
        harness = Harness(positions=[self.harness.position(symbol, -10)],
                          journal=owned([symbol], self.harness.targets["strategy_id"]),
                          blocked=[symbol], lots=10)
        cover = harness.order(symbol=symbol)
        self.assertEqual((cover["side"], cover["intent"]), ("BUY", "EXIT"))
        result = harness.check_order(cover)
        self.assertTrue(result["eligible"], result[FAIL])

    def test_r6_the_daily_strategy_order_budget(self):
        for executed, expect_blocked in ((99, False), (100, True)):
            with self.subTest(executed=executed):
                harness = Harness(lots=10)
                harness.context["executed_order_ids"] = [f"o{i}" for i in range(executed)]
                blocked = "R6" in harness.result(0)[FAIL]
                self.assertEqual(blocked, expect_blocked)

    def test_r6_a_reduction_may_use_the_emergency_reserve(self):
        symbol = shorts_of(self.harness)[0]
        harness = Harness(lots=10).place([(symbol, -60)])
        harness.context["executed_order_ids"] = [f"o{i}" for i in range(100)]
        cover = harness.order(symbol=symbol)
        self.assertEqual((cover["side"], cover["quantity"]), ("BUY", 50))
        self.assertNotIn("R6", harness.check_order(cover)[FAIL])

    def test_r6_position_cap(self):
        names = list(self.harness.targets["weights"]) + [f"X{i}" for i in range(30)]
        self.harness.snapshot["positions"] = [self.harness.position(name, -1) for name in names]
        result = self.harness.result(0)
        self.assertIn("R6", result[FAIL])
        self.assertGreater(next(row for row in result["checks"] if row["id"] == "R6")["observed"]["names"],
                           self.config["max_positions"])


class RiskRules(unittest.TestCase):
    """Item 6 is the only thing that may trade between rebalances, so these fixtures start from a held book."""

    def drift(self, holdings=(), **kwargs) -> Harness:
        return Harness(lots=10, **kwargs).place(list(holdings), rebalance_due=False)

    def test_a_drift_session_places_no_strategy_order_at_all(self):
        harness = self.drift()
        self.assertEqual(harness.plan["orders"], [])
        self.assertFalse(harness.plan["rebalance_due"])

    def test_r8_a_name_above_the_trim_is_cut_back_to_the_cap(self):
        held = trim_shares(self.drift())
        harness = self.drift([("SPY", held)])
        order = harness.order(symbol="SPY")
        left = held - order["quantity"]
        self.assertEqual((order["side"], order["intent"]), ("SELL", "EXIT"))
        cap = harness.config["ticker_cap_of_initial"] * harness.snapshot["initial_equity"]
        self.assertLessEqual(harness.prices["SPY"] * left, cap)
        self.assertGreater(harness.prices["SPY"] * (left + 1), cap)
        result = harness.check_order(order)
        self.assertTrue(result["eligible"], result[FAIL])

    def test_r8_another_name_may_not_be_increased_while_one_is_over_the_trim(self):
        harness = self.drift([("SPY", trim_shares(self.drift()))])
        etf = next(name for name in longs_of(harness) if name != "SPY")
        result = harness.check_order(harness.entry_order(etf))
        self.assertIn("R8", result[FAIL])
        self.assertIn("SPY", next(row for row in result["checks"] if row["id"] == "R8")
                      ["observed"]["above_trim"])

    def test_r9_the_drawdown_halt_leaves_only_flattening(self):
        harness = Harness(lots=10, peak=120_000.0).place([("QQQ", -1)])
        self.assertIn("R9", harness.check_order(harness.entry_order(longs_of(harness)[0]))[FAIL])
        flatten = harness.order(symbol="QQQ")
        self.assertEqual(flatten["intent"], "EMERGENCY_EXIT")
        result = harness.check_order(flatten)
        self.assertNotIn("R9", result[FAIL])
        self.assertTrue(result["eligible"], result[FAIL])

    def test_r19_a_drift_session_may_only_reduce(self):
        harness = self.drift()
        self.assertEqual(harness.plan["orders"], [])
        result = harness.check_order(harness.entry_order(longs_of(harness)[0]))
        self.assertIn("R19", result[FAIL])

    def test_r19_on_a_drift_session_only_a_reduce_only_order_passes(self):
        trim = self.drift([("SPY", trim_shares(self.drift()))])
        result = trim.check_order(trim.order(symbol="SPY"))
        self.assertTrue(result["eligible"], result[FAIL])
        harness = self.drift()
        result = harness.check_order(harness.entry_order(next(name for name in longs_of(harness) if name != "SPY")))
        self.assertEqual(result[FAIL], ["R19"], result)

    def test_r20_margin_above_90_percent_blocks_an_increase(self):
        harness = Harness(lots=10)
        harness.place([(name, -300) for name in shorts_of(harness)])
        result = harness.check_order(harness.entry_order(longs_of(harness)[0]))
        observed = next(row for row in result["checks"] if row["id"] == "R20")["observed"]
        self.assertIn("R20", result[FAIL])
        self.assertGreater(observed, harness.config["margin_cap"])

    def test_r20_a_discretionary_position_counts_toward_the_margin(self):
        harness = Harness(lots=10)
        shorts = [(name, -200) for name in shorts_of(harness)]
        harness.place(shorts)
        self.assertNotIn("R20", harness.check_order(harness.entry_order(longs_of(harness)[0]))[FAIL])
        harness.place(shorts + [("DISC", 300)], unowned=["DISC"])
        result = harness.check_order(harness.entry_order(longs_of(harness)[0]))
        self.assertIn("R20", result[FAIL])
        self.assertGreater(next(row for row in result["checks"] if row["id"] == "R20")["observed"], 0.9)
        self.assertEqual(harness.plan["protected"]["unclassified"], ["DISC"])
        self.assertNotIn("DISC", harness.plan_symbols())

    def test_r20_a_reduction_may_trade_through_the_margin_cap(self):
        harness = Harness(lots=10)
        harness.place([(name, -300) for name in shorts_of(harness)])
        result = harness.result(0)
        self.assertNotIn("R20", result[FAIL])
        self.assertTrue(result["eligible"], result[FAIL])


class TargetFreshnessAndApprovals(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()

    def test_r10_a_target_book_that_is_not_the_latest_completed_session(self):
        # Walk the clock forward a day at a time until the calendar says the fixture book is older than
        # the latest completed session: that is the condition R10 holds on, whatever book and variant the
        # harness loaded, and hard-coding a date would only ever be true of one of them.
        signal = self.harness.targets["signal_date"]
        limit = self.harness.config["max_target_age_sessions"]
        now = timestamp(self.harness.now)
        for _ in range(10):
            now += timedelta(days=1)
            if target_age(signal, now) > limit:
                break
        else:
            self.fail(f"the {signal} book never became stale")
        self.harness.context["now"] = now.isoformat()
        self.assertGreater(target_age(signal, now), limit)
        self.assertIn("R10", self.harness.result(0)[FAIL])

    def test_r10_the_expected_target_checksum_must_match(self):
        harness = Harness()
        harness.rebind()
        harness.context["expected_target_checksum"] = "0" * 64
        self.assertIn("R10", evaluate_order(harness.order(0), harness.context, harness.config)[FAIL])

    def test_r10_the_config_checksum_must_match_the_running_configuration(self):
        harness = Harness()
        harness.set_targets(config_checksum="0" * 64)
        harness.rebind()
        self.assertIn("R10", evaluate_order(harness.order(0), harness.context, harness.config)[FAIL])

    def test_r10_a_run_from_the_other_variant_is_rejected(self):
        for variant in ("pstar", "pstar15"):
            with self.subTest(variant=variant):
                harness = Harness(variant=variant)
                self.assertTrue(harness.result(0)["eligible"])
                foreign = config_for("pstar" if variant == "pstar15" else "pstar15")
                result = harness.result(0, config=foreign)
                self.assertIn("R10", result[FAIL])

    def test_r11_the_quote_and_the_account_must_be_fresh_and_verified(self):
        self.harness.context["snapshot_verified_by_relay"] = False
        self.assertIn("R11", self.harness.result(0)[FAIL])
        harness = Harness()
        harness.quotes[harness.order(0)["symbol"]]["as_of"] = "2026-11-18T14:35:00+00:00"
        self.assertIn("R11", harness.result(0)[FAIL])

    def test_r12_reconciliation_and_order_identity(self):
        symbol = self.harness.order(0)["symbol"]
        cases = (
            ("duplicate client order id", lambda c, s: c["seen_client_order_ids"].append(
                c["approved_plan_orders"][0]["client_order_id"])),
            ("same symbol already open", lambda c, s: c["open_orders"].append(
                {"order_id": "p1", "symbol": s, "side": "SELL", "status": "OPEN",
                 "remaining_quantity": 1, "limit_price": 100.0, "submitted_at": c["now"]})),
            ("not reconciled", lambda c, s: c.update(reconciled=False)),
            ("unknown broker state", lambda c, s: c["open_orders"].append(
                {"order_id": "p2", "symbol": "OTHER", "side": "SELL", "status": "UNKNOWN",
                 "remaining_quantity": 1, "limit_price": 100.0, "submitted_at": c["now"]})),
            ("stale pending order", lambda c, s: c["open_orders"].append(
                {"order_id": "p3", "symbol": "OTHER", "side": "SELL", "status": "OPEN",
                 "remaining_quantity": 1, "limit_price": 100.0,
                 "submitted_at": "2026-11-19T09:00:00+00:00"})))
        for label, mutate in cases:
            with self.subTest(case=label):
                harness = Harness()
                order = harness.order(0)
                harness.rebind(order)
                mutate(harness.context, symbol)
                self.assertIn("R12", evaluate_order(order, harness.context, harness.config)[FAIL])

    def test_r13_only_an_exchange_session_inside_the_competition_calendar(self):
        for day in ("2026-11-21T14:35:00+00:00", "2026-12-25T14:35:00+00:00",
                    "2026-11-27T15:00:00+00:00"):
            with self.subTest(day=day):
                harness = Harness()
                harness.context["now"] = day
                self.assertIn("R13", harness.result(0)[FAIL])

    def test_r14_the_auditor_is_independent_and_bound_to_this_plan(self):
        for field, value in (("session_id", "guard-pstar"), ("plan_checksum", "0" * 64),
                             ("decision", "REJECT"), ("expires_at", "2026-11-19T14:40:01+00:00")):
            with self.subTest(field=field):
                harness = Harness()
                order = harness.rebind()
                harness.context["auditor_approval"][field] = value
                self.assertIn("R14", evaluate_order(order, harness.context, harness.config)[FAIL])

    def test_r15_the_human_approval_is_exact_and_one_use(self):
        harness = Harness()
        order = harness.order(0)
        harness.rebind(order)
        harness.context["human_approvals"][0]["consumed"] = True
        self.assertIn("R15", evaluate_order(order, harness.context, harness.config)[FAIL])
        harness = Harness()
        order = harness.order(0)
        harness.rebind(order)
        harness.context["human_approvals"][0]["order_fingerprint"] = "0" * 64
        self.assertIn("R15", evaluate_order(order, harness.context, harness.config)[FAIL])

    def test_r16_the_manual_kill_switch_liquidates_only(self):
        for switch in ({"halted": True, "verified_by_relay": True},
                       {"halted": False, "verified_by_relay": False}):
            with self.subTest(switch=switch):
                harness = Harness()
                harness.context["kill_switch"] = switch
                self.assertIn("R16", harness.result(0)[FAIL])

    def test_r17_release_lease_workflow_and_permission_policy(self):
        for field in ("release_verified", "lease_verified", "permission_policy_verified"):
            with self.subTest(field=field):
                harness = Harness()
                harness.context[field] = False
                self.assertIn("R17", harness.result(0)[FAIL])
        order = dict(self.harness.order(0), workflow="something-else")
        self.assertIn("R17", self.harness.check_order(order)[FAIL])

    def test_r7_the_limit_must_track_the_live_quote_in_regular_hours(self):
        order = dict(self.harness.order(0),
                     limit_price=round(self.harness.order(0)["limit_price"] * 1.004, 2))
        self.assertIn("R7", self.harness.check_order(order)[FAIL])
        harness = Harness()
        harness.context["now"] = "2026-11-19T22:00:00+00:00"
        self.assertIn("R7", harness.result(0)[FAIL])

    def test_r18_an_order_must_be_a_line_of_the_approved_plan(self):
        harness = Harness()
        order = dict(harness.order(0), quantity=harness.order(0)["quantity"] + 1)
        harness.rebind()
        self.assertIn("R18", evaluate_order(order, harness.context, harness.config)[FAIL])

    def test_r18_a_hold_target_places_no_new_strategy_order(self):
        harness = Harness()
        order = dict(harness.order(0))
        harness.plan["orders"] = []
        harness.set_targets(status="HOLD", weights={}, dollars={}, sleeves={}, source_targets=[],
                            source={"file": None, "checksum_sha256": "0" * 64,
                                    "run_generated_at_utc": None}, hold_reason="fixture: no verified run")
        harness.rebind(order)
        result = evaluate_order(order, harness.context, harness.config)
        self.assertIn("R18", result[FAIL])
        self.assertNotIn("R0", result[FAIL])

    def test_r21_the_frozen_composite_scale_and_diagnostics_are_required(self):
        for field, value in (("factors", ["not_a_frozen_factor"]), ("k_scale", 1.0),
                             ("strategy_id", "r14-pstar"),
                             ("diagnostics", {"ok": False, "equity": 100000.0, "margin": 0.3,
                                              "positions": 82, "n_short": 77})):
            with self.subTest(field=field):
                harness = Harness()
                harness.set_targets(**{field: value})
                result = harness.result(0)
                self.assertIn("R21", result[FAIL])
                self.assertIn("R10", result[FAIL])


class StrategySwitch(unittest.TestCase):
    def test_the_retired_plan_a_rules_only_run_for_strategy_legacy_a(self):
        legacy = load_config()
        self.assertEqual(legacy["strategy"], "legacy-a")
        harness = Harness()
        result = harness.result(0, config=legacy)
        self.assertFalse(result["eligible"], result[FAIL])
        self.assertNotIn("strategy", result)
        self.assertNotIn("variant", result)
        self.assertIn("R2", result[FAIL])

    def test_plan_a_targets_are_not_valid_pstar_targets(self):
        payload = json.loads(LEGACY_EXAMPLE.read_text(encoding="utf-8"))["context"]["targets"]
        with self.assertRaises(ValueError):
            validate_targets(payload, load_strategy_config())

    def test_the_plan_a_example_fixture_is_not_eligible_under_pstar(self):
        data = json.loads(LEGACY_EXAMPLE.read_text(encoding="utf-8"))
        result = evaluate_order(data["order"], data["context"], load_strategy_config())
        self.assertFalse(result["eligible"])
        self.assertEqual([result["strategy"], result["variant"]], ["pstar", "pstar15"])
        self.assertTrue(len(result["checks"]) > 1 and {"R10", "R21"} <= set(result[FAIL]), result[FAIL])

    def test_the_snapshot_not_the_targets_size_the_book(self):
        harness = Harness(equity=EQUITY, peak=EQUITY * 1.02)
        self.assertTrue(harness.result(0)["eligible"], harness.result(0)[FAIL])
        self.assertAlmostEqual(harness.state["drawdown"], EQUITY / (EQUITY * 1.02) - 1, places=12)
        self.assertEqual(harness.plan["equity"], EQUITY)


if __name__ == "__main__":
    unittest.main()
