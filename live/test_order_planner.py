"""Offline tests for the P* order planner: the frozen execution rules of prompt 06 item 6.

The target book is the committed research run converted by the production path (helpers from
`live/test_pstar.py`), so these tests see the same weights, dollars and sleeves the deployment trades.
Prices are synthetic but deterministic: each name is priced so its target is a whole number of shares.
Nothing here places an order.
"""
from __future__ import annotations

import unittest

from live import order_planner as planner
from live import pstar
from live.contracts import load_strategy_config, seal
from live.pstar import classify
from live.test_pstar import NOW, config_for, run_file, snapshot

EQUITY = 100_000.0
LOTS = 2


def targets(rebalance_due: bool = True, **updates) -> dict:
    """The production conversion of the frozen run, resealed with the fields a plan test needs."""
    body = {k: v for k, v in pstar.make_targets(config_for(), snapshot(), NOW,
                                                run=run_file("pstar15")).items() if k != "checksum"}
    return seal({**body, "rebalance_due": rebalance_due, **updates})


def held_targets() -> dict:
    """A HOLD book: the same path with an account state the research run cannot size."""
    return pstar.make_targets(config_for(), snapshot(equity=-5), NOW, run=run_file("pstar15"))


def quotes_for(weights: dict, equity: float = EQUITY, lots: int = LOTS) -> dict:
    """Price each name so that its target is exactly `lots` whole shares."""
    return {name: {"price": round(abs(weight) * equity / lots, 2) or 0.01, "as_of": NOW.isoformat(),
                   "verified_by_relay": True} for name, weight in weights.items()}


def broker_rows(quotes: dict, positions) -> list[dict]:
    """Snapshot rows the way the relay returns them: the name, its shares and the price it is worth at."""
    rows = []
    for entry in positions:
        row = dict(zip(("symbol", "quantity", "price"), entry)) if isinstance(entry, tuple) else dict(entry)
        price = row.get("price") or (quotes.get(str(row["symbol"]).removeprefix("US.")) or {}).get("price", 100.0)
        rows.append({**row, "price": price})
    return rows


def fixture(book: dict, positions=(), **config_updates) -> dict:
    quotes = quotes_for(book["weights"])
    return {"config": {**config_for(), **config_updates}, "targets": book,
            "quotes": quotes, "snapshot": snapshot(positions=broker_rows(quotes, positions)),
            "journal": [{"symbol": row["symbol"], "strategy_id": book["strategy_id"], "action": "ALLOW"}
                        for row in book.get("source_targets") or ()]}


def build(one: dict, **kwargs) -> dict:
    return planner.plan(one["config"], one["targets"], one["snapshot"], one["quotes"],
                        journal_records=one["journal"], **kwargs)


def a_short(book: dict) -> str:
    return next(name for name, weight in book["weights"].items() if weight < 0)


def a_long(book: dict) -> str:
    return next(name for name, weight in book["weights"].items() if weight > 0)


class InputContract(unittest.TestCase):
    def setUp(self):
        self.book = targets()

    def test_the_planner_only_runs_the_pstar_configuration(self):
        with self.assertRaises(planner.PlanError):
            planner.plan(load_strategy_config("legacy-a"), self.book, snapshot(), {})

    def test_unmeasurable_state_is_refused_rather_than_planned(self):
        cases = {
            "no equity": lambda one: one["snapshot"].update(equity=None),
            "zero equity": lambda one: one["snapshot"].update(equity=0),
            "a fractional holding": lambda one: one["snapshot"].update(
                positions=[{"symbol": "AAPL", "quantity": 1.5, "price": 100.0}]),
            "a non-string symbol": lambda one: one["snapshot"].update(
                positions=[{"symbol": None, "quantity": 1, "price": 100.0}]),
        }
        for label, corrupt in cases.items():
            with self.subTest(case=label):
                one = fixture(self.book)
                corrupt(one)
                with self.assertRaises(planner.PlanError):
                    build(one)
        with self.assertRaises(planner.PlanError):
            build(fixture(self.book), executed_today=-1)

    def test_holdings_and_prices_normalise_the_broker_rows(self):
        self.assertEqual(planner.holdings({"positions": [{"symbol": "US.AAOI", "quantity": -3}]}), {"AAOI": -3})
        self.assertEqual(planner.holdings({}), {})
        self.assertEqual(planner.price_of({"A": {"price": 10.0, "verified_by_relay": True}}, "A"), 10.0)
        self.assertIsNone(planner.price_of({"A": {"price": 10.0}}, "A"))
        self.assertIsNone(planner.price_of({}, "A"))

    def test_the_order_budget_keeps_the_emergency_flatten_reserve(self):
        config = config_for()
        self.assertEqual(planner.order_budget(config),
                         config["competition_order_cap"] - config["emergency_flatten_reserve"])
        for cap, reserve in ((100, 100), (100, 150), (100, -1)):
            with self.subTest(cap=cap, reserve=reserve):
                with self.assertRaises(planner.PlanError):
                    planner.order_budget({**config, "competition_order_cap": cap,
                                          "emergency_flatten_reserve": reserve})


class WholeShareSizing(unittest.TestCase):
    def setUp(self):
        self.book = targets()
        self.weights = self.book["weights"]

    def test_a_target_is_rounded_to_the_nearest_whole_share(self):
        quotes = quotes_for(self.weights)
        want, skipped = planner.target_shares(self.weights, EQUITY, quotes)
        self.assertEqual(want, {name: (LOTS if weight > 0 else -LOTS) for name, weight in self.weights.items()})
        self.assertEqual(skipped, [])

    def test_the_rounding_follows_the_equity_not_the_published_dollars(self):
        quotes = quotes_for(self.weights)
        for multiple in (1.37, 0.53):
            with self.subTest(equity=multiple):
                want, _ = planner.target_shares(self.weights, EQUITY * multiple, quotes)
                self.assertEqual(want, {name: int(round(weight * EQUITY * multiple / quotes[name]["price"]))
                                        for name, weight in self.weights.items()})

    def test_a_name_rounding_to_zero_shares_is_skipped_and_never_redistributed(self):
        quotes = {name: {"price": abs(weight) * EQUITY * 100, "verified_by_relay": True}
                  for name, weight in self.weights.items()}
        want, skipped = planner.target_shares(self.weights, EQUITY, quotes)
        self.assertEqual(want, {})
        self.assertEqual(len(skipped), len(self.weights))
        self.assertTrue(all("zero shares" in row["reason"] and "not redistributed" in row["reason"]
                            for row in skipped))
        one = fixture(self.book)
        one["quotes"] = quotes
        plan = build(one)
        self.assertEqual(plan["orders"], [])
        self.assertEqual({row["symbol"] for row in plan["skipped"]}, set(self.weights))
        self.assertEqual(plan["counts"]["skipped"], len(self.weights))

    def test_an_unverified_quote_is_skipped(self):
        quotes = quotes_for(self.weights)
        name = next(iter(self.weights))
        quotes[name]["verified_by_relay"] = False
        want, skipped = planner.target_shares(self.weights, EQUITY, quotes)
        self.assertNotIn(name, want)
        self.assertEqual([row["symbol"] for row in skipped], [name])
        self.assertIn("relay-verified", skipped[0]["reason"])

    def test_a_zero_weight_is_not_an_order(self):
        want, skipped = planner.target_shares({"AAA": 0.0, "SPY": 0.01}, EQUITY, {"AAA": 100.0, "SPY": 100.0})
        self.assertEqual([want, skipped], [{"SPY": 10}, []])


class GoLiveBatch(unittest.TestCase):
    def setUp(self):
        self.book = targets()
        self.one = fixture(self.book)
        self.plan = build(self.one)

    def test_the_go_live_day_is_one_entry_batch_of_the_frozen_book(self):
        orders, counts = self.plan["orders"], self.plan["counts"]
        self.assertEqual(self.plan["status"], "PLAN")
        self.assertTrue(self.plan["entry_batch"], self.plan["note"])
        self.assertEqual(self.plan["intents"], ["ENTRY"])
        self.assertEqual(len(orders), len(self.book["weights"]))
        self.assertEqual(counts["orders"], counts["entries"])
        self.assertEqual({row["symbol"] for row in orders}, set(self.book["weights"]))
        self.assertLessEqual(counts["orders"], config_for()["max_orders_per_day"])
        self.assertLessEqual(counts["orders"] + counts["positions_after"],
                             config_for()["competition_order_cap"])
        self.assertLessEqual(counts["positions_after"], config_for()["max_positions"])

    def test_every_order_line_carries_the_frozen_schema(self):
        account = "5322438" if config_for()["mode"] == "test" else "5832812"
        for order in self.plan["orders"]:
            with self.subTest(symbol=order["symbol"]):
                weight = self.book["weights"][order["symbol"]]
                self.assertEqual(order["side"], "BUY" if weight > 0 else "SELL")
                self.assertEqual([order["order_type"], order["time_in_force"], order["session"],
                                  order["workflow"], order["account_id"]],
                                 ["LIMIT", "DAY", "RTH", "paper-trading-gate", account])
                self.assertEqual(order["quantity"], LOTS)
                self.assertEqual(order["limit_price"], self.one["quotes"][order["symbol"]]["price"])
                self.assertEqual([order["sleeve"], order["weight"], order["target_checksum"]],
                                 [self.book["sleeves"][order["symbol"]], weight, self.book["checksum"]])

    def test_client_order_ids_are_unique_and_reproducible_for_the_same_book(self):
        ids = [row["client_order_id"] for row in self.plan["orders"]]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(ids, [row["client_order_id"] for row in build(self.one)["orders"]])
        self.assertEqual(self.plan["plan_checksum"], build(self.one)["plan_checksum"])
        self.assertNotEqual(self.plan["plan_checksum"], build(fixture(targets(False)))["plan_checksum"])

    def test_an_order_id_is_deterministic_down_to_its_token(self):
        order_id = planner.client_order_id(self.book, "AAOI", "SELL", 3)
        self.assertTrue(order_id.startswith(f"pstar-{self.book['signal_date']}-AAOI-SELL-3-"))
        self.assertEqual(order_id, planner.client_order_id(self.book, "AAOI", "SELL", 3))
        for other in (("AAOI", "SELL", 4), ("AAOI", "BUY", 3), ("AAPL", "SELL", 3)):
            self.assertNotEqual(order_id, planner.client_order_id(self.book, *other))
        changed = seal({**{k: v for k, v in self.book.items() if k != "checksum"}, "sizing_equity": 1.0})
        self.assertNotEqual(order_id, planner.client_order_id(changed, "AAOI", "SELL", 3))

    def test_the_plan_reports_which_frozen_run_it_came_from(self):
        self.assertEqual([self.plan["variant"], self.plan["strategy_id"], self.plan["session"],
                          self.plan["signal"], self.plan["rebalance_due"], self.plan["equity"]],
                         ["pstar15", "r14-pstar15", "2026-11-18", "READY", True, EQUITY])
        self.assertEqual(self.plan["counts"]["positions_after"], len(self.book["weights"]))
        self.assertEqual(self.plan["final_shares"], self.plan["target_shares"])
        self.assertEqual(self.plan["targets_checksum"], self.book["checksum"])


class RebalanceRules(unittest.TestCase):
    def setUp(self):
        self.book = targets()
        self.short = a_short(self.book)
        self.band = config_for()["trade_band_of_equity"] * EQUITY

    def off_target(self, shares_off: int) -> dict:
        return build(fixture(self.book, [(self.short, -LOTS - shares_off)]))

    def test_an_existing_position_is_resized_only_outside_the_trade_band(self):
        quotes = quotes_for(self.book["weights"])
        price = quotes[self.short]["price"]
        for shares_off, expect_line in ((int(self.band * 0.4 / price), False), (int(self.band * 3 / price), True)):
            with self.subTest(shares_off=shares_off):
                plan = self.off_target(shares_off)
                lines = [row for row in plan["orders"] if row["symbol"] == self.short]
                self.assertEqual(bool(lines), expect_line)
                self.assertEqual(plan["final_shares"][self.short], -LOTS if expect_line else -LOTS - shares_off)
                if expect_line:
                    self.assertEqual((lines[0]["side"], lines[0]["quantity"], lines[0]["intent"],
                                      lines[0]["reason"]),
                                     ("BUY", shares_off, "REBALANCE", "off target by more than the trade band"))

    def test_entries_and_exits_always_trade_even_inside_the_band(self):
        one = fixture(self.book, [("GONE", -1)])
        one["quotes"]["GONE"] = {"price": 100.0, "as_of": NOW.isoformat(), "verified_by_relay": True}
        one["journal"] = one["journal"] + [{"symbol": "GONE", "strategy_id": self.book["strategy_id"],
                                            "action": "ALLOW"}]
        plan = build(one)
        exit_line = next(row for row in plan["orders"] if row["symbol"] == "GONE")
        self.assertEqual((exit_line["side"], exit_line["quantity"], exit_line["intent"]), ("BUY", 1, "EXIT"))
        self.assertEqual(plan["final_shares"]["GONE"], 0)
        self.assertIn("exit", exit_line["reason"])
        other = next(name for name in self.book["weights"] if name != self.short)
        small = build(fixture(self.book, [(self.short, -LOTS - 1)]))
        self.assertEqual([row["intent"] for row in small["orders"] if row["symbol"] == other], ["ENTRY"])

    def test_a_drift_session_places_no_strategy_order_at_all(self):
        plan = build(fixture(targets(False), [(self.short, -100)]))
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["counts"]["orders"], 0)
        self.assertFalse(plan["rebalance_due"])
        self.assertFalse(plan["entry_batch"])

    def test_a_hold_book_trades_nothing_and_keeps_the_account_as_it_is(self):
        book = held_targets()
        one = fixture(book, [(self.short, -7)])
        one["journal"] = [{"symbol": self.short, "strategy_id": book["strategy_id"], "action": "ALLOW"}]
        plan = build(one)
        self.assertEqual([plan["status"], plan["signal"], plan["orders"]], ["PLAN", "HOLD", []])
        self.assertEqual(plan["final_shares"][self.short], -7)
        self.assertEqual(plan["target_shares"], {})


class ProtectedPositions(unittest.TestCase):
    def setUp(self):
        self.book = targets()
        self.short = a_short(self.book)

    def test_a_position_the_strategy_did_not_open_is_never_traded(self):
        one = fixture(self.book, [(self.short, -40)])
        one["journal"] = [row for row in one["journal"] if row["symbol"] != self.short]
        plan = build(one)
        self.assertNotIn(self.short, [row["symbol"] for row in plan["orders"]])
        self.assertEqual(plan["final_shares"][self.short], -40)
        self.assertEqual(plan["protected"]["unclassified"], [self.short])
        self.assertTrue(any(row["symbol"] == self.short and "never sold" in row["reason"]
                            for row in plan["skipped"]))

    def test_a_whitelisted_name_is_discretionary_rather_than_unclassified(self):
        config = {**config_for(), "discretionary_symbols": [self.short]}
        book_state = snapshot(positions=[{"symbol": self.short, "quantity": -5, "price": 100.0}])
        attribution = classify(book_state, config, self.book["strategy_id"], [])
        self.assertEqual([attribution["discretionary"], attribution["unclassified"]], [[self.short], []])
        self.assertEqual(attribution["hold"], {self.short: -500.0})

    def test_protection_survives_the_drawdown_flatten(self):
        one = fixture(targets(False), [(self.short, -40)])
        one["journal"] = []
        risk = {"halt": True, "target_shares": {self.short: 0}, "share_changes": {self.short: 40}}
        plan = build(one, risk=risk)
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["final_shares"][self.short], -40)


class EligibilityScreen(unittest.TestCase):
    def setUp(self):
        self.book = targets()
        self.short = a_short(self.book)

    def test_a_blocked_name_is_skipped_and_its_weight_is_not_redistributed(self):
        reason = "borrow is not confirmed shortable and easy to borrow"
        plan = build(fixture(self.book), eligibility={
            "blocked": [self.short], "blocked_detail": {self.short: [reason]},
            "skipped": [{"symbol": self.short, "reasons": [reason]}],
            "slack_report": [f"short {self.short} skipped: {reason}. Not replaced."]})
        self.assertNotIn(self.short, [row["symbol"] for row in plan["orders"]])
        self.assertNotIn(self.short, plan["target_shares"])
        line = next(row for row in plan["skipped"] if row["symbol"] == self.short)
        self.assertEqual([line["reason"], line["weight"]], [reason, self.book["weights"][self.short]])
        self.assertEqual(plan["counts"]["orders"], len(self.book["weights"]) - 1)
        self.assertEqual(plan["eligibility"]["blocked"], [self.short])
        self.assertTrue(any("Not replaced" in row for row in plan["slack_report"]))

    def test_a_held_short_that_loses_its_borrow_is_closed_out(self):
        plan = build(fixture(self.book, [(self.short, -LOTS)]),
                     eligibility={"blocked": [self.short], "blocked_detail": {}})
        line = next(row for row in plan["orders"] if row["symbol"] == self.short)
        self.assertEqual((line["side"], line["quantity"], line["intent"]), ("BUY", LOTS, "EXIT"))

    def test_an_etf_that_fails_the_competition_screen_is_dropped_from_the_batch(self):
        etf = a_long(self.book)
        plan = build(fixture(self.book), eligibility={
            "blocked": [etf], "blocked_detail": {etf: [f"price $0.50 below the $1 limit"]}})
        self.assertNotIn(etf, plan["target_shares"])
        self.assertTrue(any(row["symbol"] == etf and "below the $1 limit" in row["reason"]
                            for row in plan["skipped"]))


class RiskOverlay(unittest.TestCase):
    def setUp(self):
        self.book = targets(False)
        self.long, self.short = a_long(self.book), a_short(self.book)

    def test_only_the_risk_actions_trade_between_rebalances_and_only_downward(self):
        one = fixture(self.book, [(self.long, LOTS * 3)])
        price = one["quotes"][self.long]["price"]
        cut = int(LOTS * 3 * 0.9)
        risk = {"halt": False, "flatten": False, "delever_scale": 0.9,
                "target_shares": {self.long: cut}, "share_changes": {self.long: cut - LOTS * 3},
                "slack_report": ["margin 96.0% above 95%: scaled the strategy book by 0.9000 to reach 90%."]}
        plan = build(one, risk=risk)
        line = next(row for row in plan["orders"] if row["symbol"] == self.long)
        self.assertEqual([line["side"], line["intent"], line["reason"], line["quantity"]],
                         ["SELL", "EXIT", "risk rule", LOTS * 3 - cut])
        self.assertEqual(plan["final_shares"][self.long], cut)
        self.assertAlmostEqual(price * plan["final_shares"][self.long], price * cut, places=6)
        self.assertIn("0.9000", plan["slack_report"][0])

    def test_a_risk_target_that_would_add_exposure_is_ignored(self):
        one = fixture(self.book, [(self.long, LOTS)])
        risk = {"halt": False, "target_shares": {self.long: LOTS * 5}, "share_changes": {self.long: LOTS * 4}}
        plan = build(one, risk=risk)
        self.assertEqual([plan["orders"], plan["final_shares"][self.long]], [[], LOTS])

    def test_a_drawdown_halt_flattens_with_emergency_intents(self):
        one = fixture(self.book, [(self.long, LOTS), (self.short, -LOTS)])
        risk = {"halt": True, "flatten": True, "target_shares": {self.long: 0, self.short: 0},
                "share_changes": {self.long: -LOTS, self.short: LOTS},
                "slack_report": ["drawdown -8.00% reached -8% of the peak: flatten everything and halt."]}
        plan = build(one, risk=risk)
        self.assertEqual(plan["intents"], ["EMERGENCY_EXIT"])
        self.assertEqual(plan["counts"]["emergency_exits"], 2)
        self.assertEqual([plan["final_shares"][self.long], plan["final_shares"][self.short]], [0, 0])
        self.assertTrue(all(row["reason"] == "drawdown halt: flatten" for row in plan["orders"]))

    def test_the_monitor_and_the_planner_agree_on_the_trim(self):
        from live.risk_monitor import actions, measure
        one = fixture(self.book, [(self.long, 60)])
        config = config_for()
        measured = measure(config, one["snapshot"]["positions"], EQUITY, EQUITY, EQUITY)
        self.assertIn(self.long, measured["over_trim"])
        plan = build(one, risk=actions(config, measured, protected=()))
        line = next(row for row in plan["orders"] if row["symbol"] == self.long)
        self.assertEqual([line["side"], line["intent"], line["reason"]], ["SELL", "EXIT", "risk rule"])
        self.assertLessEqual(one["quotes"][self.long]["price"] * plan["final_shares"][self.long],
                             config["ticker_cap_of_initial"] * EQUITY + 1e-9)


class OrderBudget(unittest.TestCase):
    def setUp(self):
        self.book = targets()
        self.short = a_short(self.book)
        self.normal = len(self.book["weights"])

    def test_more_orders_than_the_budget_left_today_is_a_hold(self):
        plan = build(fixture(self.book), executed_today=config_for()["competition_order_cap"]
                     - config_for()["emergency_flatten_reserve"] - self.normal + 1)
        self.assertEqual(plan["status"], "HOLD")
        self.assertEqual(plan["orders"], [])
        self.assertIn(f"{self.normal} strategy orders exceed", plan["note"])
        self.assertIn("emergency reserve", plan["note"])

    def test_a_halt_never_becomes_permission_to_place_the_rest_of_the_batch(self):
        config = config_for()
        one = fixture(self.book, [(self.short, -4)])
        risk = {"halt": True, "target_shares": {self.short: 0}, "share_changes": {self.short: 4}}
        plan = build(one, executed_today=70, risk=risk)
        self.assertEqual([row["intent"] for row in plan["orders"]], ["EMERGENCY_EXIT"])
        self.assertEqual([plan["counts"]["entries"], plan["counts"]["rebalances"],
                          plan["counts"]["emergency_exits"]], [0, 0, 1])
        self.assertLessEqual(len(plan["orders"]), config["emergency_flatten_reserve"])

    def test_the_budget_reports_what_is_left_today(self):
        plan = build(fixture(self.book), executed_today=10)
        self.assertEqual([plan["counts"]["order_budget"], plan["counts"]["budget_left_today"]], [100, 90])
        self.assertEqual(plan["status"], "PLAN")
        self.assertEqual(build(fixture(self.book), executed_today=100)["status"], "HOLD")

    def test_a_projected_book_over_the_position_cap_places_nothing(self):
        plan = build(fixture(self.book, [("DISC", 1)], max_positions=50))
        self.assertEqual(plan["status"], "HOLD")
        self.assertEqual(plan["orders"], [])
        self.assertIn("exceed the 50 cap", plan["note"])
        self.assertGreater(plan["counts"]["positions_after"], 50)


if __name__ == "__main__":
    unittest.main()
