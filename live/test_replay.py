from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from live.contracts import SCHEMA_VERSION, STRATEGY_ID, digest, frozen_factor_names, hedge_keys, load_config, seal
from live.daily_scores import (ACCOUNTS, Hold, SYNTHETIC_SOURCE, fixture_panel, load_history, new_history,
                               panel_identity, save_history, score_panel, sessions, state_path, tradable_mask)
from live.replay import (account_snapshot, check_invariants, forward_returns, measured_beta,
                         measured_margin, panel_asof, summarize, walk, write_validation)
from rules.trading_calendar import exchange

CONFIG = load_config()


def ready_target(config=CONFIG, weights=None, equity=100000., **overrides):
    """A contract-valid READY target inside the frozen caps, for invariant testing."""
    body = {"schema_version": SCHEMA_VERSION, "strategy_id": STRATEGY_ID, "mode": config["mode"],
            "status": "READY", "generated_at": "2026-11-19T22:30:00Z",
            "data_timestamp": "2026-11-19T21:00:00Z", "signal_date": "2026-11-19",
            "weights": weights if weights is not None else {"AAA": -0.05, "BBB": -0.05, "CCC": -0.05},
            "vol_multiplier": 1.0, "model_coefficients": dict.fromkeys(frozen_factor_names(config), 0.01),
            "model_nonzero_factors": 1, "model_carried_forward": False, "model_retrained_today": True,
            "model_fit_date": "2026-11-19", "signal_guard": {"passed": True, "distinct_values": 118},
            "sizing_equity": equity, "rebalance_due": True, "config_checksum": digest(config),
            "position_snapshot_checksum": digest({"equity": equity})}
    for name, field in hedge_keys(config).items():
        body[field] = 0.0
    if weights is None:
        body["spy_hedge_weight"] = 0.15                    # three beta-1 shorts of 0.15 gross need 0.15 long
    return seal({**body, **overrides})


def beta_series(values):
    beta = pd.Series(1.0, index=list(values), dtype=float)
    beta.update(values)
    beta["SPY"], beta["QQQ"] = 1.0, 1.0
    return beta


def names(target, config):
    """The symbols a published target actually holds, hedge ETFs included."""
    return set(target.get("weights", {})) | {name for name, field in hedge_keys(config).items()
                                             if target.get(field)}


class InvariantTests(unittest.TestCase):
    def setUp(self):
        self.beta = beta_series({"AAA": 1.0, "BBB": 1.0, "CCC": 1.0})

    def test_clean_hedged_book_passes_and_reports_measures(self):
        measured = check_invariants(ready_target(), CONFIG, self.beta, 100000.)
        self.assertAlmostEqual(measured["gross_short"], 0.15)
        self.assertAlmostEqual(measured["residual_beta"], 0.0, places=9)
        self.assertAlmostEqual(measured["margin_used"], 0.5 * 0.15 + 0.6 * 0.15)
        self.assertEqual(measured["short_names"], 3)
        self.assertFalse(measured["drift_beyond_gross_cap"])

    def test_contract_refuses_a_breached_rebalance_book(self):
        # The frozen contract itself refuses >20 names and an over-35% gross short at a rebalance,
        # so those never reach the replay measures.
        for weights in ({f"B{i:02d}": -0.02 for i in range(21)}, {f"B{i:02d}": -0.10 for i in range(4)}):
            with self.subTest(names=len(weights)):
                with self.assertRaises(ValueError):
                    check_invariants(ready_target(weights=weights), CONFIG,
                                     beta_series({k: 1.0 for k in weights}), 100000.)

    def test_all_zero_coefficients_never_trade(self):
        # A zero vector is refused by the contract; a reported zero count is refused by the replay.
        with self.assertRaises(ValueError):
            check_invariants(ready_target(model_coefficients=dict.fromkeys(frozen_factor_names(), 0.0)),
                            CONFIG, self.beta, 100000.)
        with self.assertRaisesRegex(Hold, "all_zero_traded"):
            check_invariants(ready_target(model_nonzero_factors=0), CONFIG, self.beta, 100000.)

    def test_hedge_over_the_initial_equity_cap_refuses(self):
        config = dict(CONFIG, gross_short=1.0)
        weights = {"AAA": -0.10, "BBB": -0.10, "CCC": -0.10}
        target = ready_target(config, weights=weights, spy_hedge_weight=0.39)
        with self.assertRaisesRegex(Hold, "hedge_cap"):
            check_invariants(target, config, self.beta, 100000.)

    def test_hedge_cap_loosens_when_equity_falls(self):
        config = dict(CONFIG, gross_short=1.0)
        weights = {"AAA": -0.10, "BBB": -0.10, "CCC": -0.10}
        target = ready_target(config, weights=weights, spy_hedge_weight=0.39, equity=80000.)
        self.assertAlmostEqual(check_invariants(target, config, self.beta, 100000.)["hedge_cap_of_equity"], .475)

    def test_margin_cap_is_measured_from_the_published_book(self):
        # With the frozen 35%/38% caps margin usage cannot reach 90%, so the cap is proven with a
        # tightened threshold rather than an impossible book.
        config = dict(CONFIG, gross_short=1.0, margin_cap=0.5)
        weights = {f"B{i:02d}": -0.0175 for i in range(20)}
        flat = beta_series({f"B{i:02d}": 0.0 for i in range(20)})
        self.assertAlmostEqual(check_invariants(ready_target(config, weights=weights, spy_hedge_weight=0.30,
                                                             rebalance_due=False),
                                                config, flat, 100000.)["margin_used"], .5 * .30 + .6 * .35)
        with self.assertRaisesRegex(Hold, "margin"):
            check_invariants(ready_target(config, weights=weights, spy_hedge_weight=0.38,
                                          qqq_hedge_weight=0.38, rebalance_due=False), config, flat, 100000.)

    def test_unhedged_book_refuses_on_a_rebalance_only(self):
        config = dict(CONFIG, gross_short=1.0)
        weights = {"AAA": -0.10, "BBB": -0.10, "CCC": -0.10}
        for rebalance, outcome in ((True, "residual_beta"), (False, None)):
            with self.subTest(rebalance_due=rebalance):
                target = ready_target(config, weights=weights, rebalance_due=rebalance)
                if outcome:
                    with self.assertRaisesRegex(Hold, outcome):
                        check_invariants(target, config, self.beta, 100000.)
                else:
                    self.assertAlmostEqual(check_invariants(target, config, self.beta, 100000.)["residual_beta"], -.30)

    def test_drift_over_the_sizing_caps_is_reported_not_breached(self):
        # Gross 35% and the 38% hedge caps size a rebalance. Outside one, a restated held book may sit
        # above them and the replay reports the drift instead of refusing the target.
        measured = check_invariants(ready_target(rebalance_due=False), CONFIG, self.beta, 100000.)
        self.assertFalse(measured["drift_beyond_gross_cap"])
        self.assertFalse(measured["drift_beyond_hedge_cap"])
        weights = {f"B{i:02d}": -0.10 for i in range(4)}
        measured = check_invariants(ready_target(CONFIG, weights=weights, rebalance_due=False),
                                    CONFIG, beta_series(dict.fromkeys(weights, 1.0)), 100000.)
        self.assertTrue(measured["drift_beyond_gross_cap"])
        measured = check_invariants(ready_target(spy_hedge_weight=0.39, rebalance_due=False),
                                    CONFIG, self.beta, 100000.)
        self.assertTrue(measured["drift_beyond_hedge_cap"])

    def test_single_name_weight_cap(self):
        config = dict(CONFIG, gross_short=1.0)
        weights = {"AAA": -0.11, "BBB": -0.11, "CCC": -0.11}
        with self.assertRaisesRegex(Hold, "name_cap"):
            check_invariants(ready_target(config, weights=weights, spy_hedge_weight=0.33),
                             config, self.beta, 100000.)

    def test_position_count_cap(self):
        config = dict(CONFIG, gross_short=1.0, max_positions=3)
        weights = {"AAA": -0.10, "BBB": -0.10, "CCC": -0.10}
        with self.assertRaisesRegex(Hold, "position_count"):
            check_invariants(ready_target(config, weights=weights, spy_hedge_weight=0.30),
                             config, self.beta, 100000.)

    def test_hold_targets_are_not_checked(self):
        with self.assertRaises(Hold):
            check_invariants(seal({**{k: v for k, v in ready_target().items() if k != "checksum"},
                                   "status": "HOLD"}), CONFIG, self.beta, 100000.)

    def test_missing_hedge_beta_refuses(self):
        beta = self.beta.copy()
        beta["QQQ"] = np.nan
        with self.assertRaises(Hold):
            measured_beta(pd.Series({"AAA": -0.1}), {"SPY": 0.1, "QQQ": 0.01}, beta, CONFIG)

    def test_stock_beta_follows_the_scorer_convention(self):
        # allocate_hedge measures a missing stock beta as 1.0; the replay must use the same convention,
        # otherwise it would assert a hedge the production book never aimed at.
        beta = self.beta.copy()
        beta["AAA"] = np.nan
        hedges = {"SPY": 0.10, "QQQ": 0.0}
        self.assertAlmostEqual(measured_beta(pd.Series({"AAA": -0.10}), hedges, beta, CONFIG), 0.0, places=9)
        beta["AAA"] = np.inf
        with self.assertRaises(Hold):
            measured_beta(pd.Series({"AAA": -0.10}), hedges, beta, CONFIG)

    def test_margin_and_beta_arithmetic_are_explicit(self):
        weights = pd.Series({"AAA": -0.10, "BBB": -0.20})
        hedges = {"SPY": 0.30, "QQQ": -0.05}
        self.assertAlmostEqual(measured_margin(weights, hedges, CONFIG), .5 * .30 + .6 * (.30 + .05))
        beta = beta_series({"AAA": 2.0, "BBB": -1.5})
        # BBB's -1.5 beta is clipped to the frozen [-1, 3] range, as in the research book construction.
        self.assertAlmostEqual(measured_beta(weights, hedges, beta, CONFIG), -.10 * 2.0 + .20 * 1.0 + .30 - .05)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.days = sessions("2026-11-17", 4)

    def test_positions_carry_signed_quantities_and_eod_clock(self):
        weights = pd.Series({"AAA": -0.035, "SPY": 0.035})
        prices = pd.Series({"AAA": 70., "SPY": 500.})
        beta = beta_series({"AAA": 1.0})
        snap = account_snapshot(CONFIG, "2026-11-17", 100000., 100000., 100000., 100000., weights,
                               prices, beta, [], {})
        held = {row["symbol"]: row["quantity"] for row in snap["positions"]}
        self.assertAlmostEqual(held["AAA"], -50.)
        self.assertAlmostEqual(held["SPY"], 7.)
        self.assertEqual(snap["account_id"], ACCOUNTS["test"])
        self.assertEqual(snap["as_of"], exchange().session_close(pd.Timestamp("2026-11-17")).isoformat())

    def test_missing_price_for_a_held_name_refuses(self):
        with self.assertRaises(Hold):
            account_snapshot(CONFIG, "2026-11-17", 100000., 100000., 100000., 100000.,
                             pd.Series({"AAA": -0.035}), pd.Series(dtype=float), beta_series({"AAA": 1.0}), [], {})

    def test_forward_returns_use_the_next_two_opens(self):
        opens = pd.DataFrame({"A": [1., 10., 12., 15.], "SPY": [1., 10., 10., 10.]}, index=self.days)
        returns = forward_returns(opens, self.days[:2])
        self.assertAlmostEqual(returns.loc[self.days[0], "A"], .2)
        self.assertAlmostEqual(returns.loc[self.days[1], "A"], .25)
        with self.assertRaises(Hold):
            forward_returns(opens.iloc[:2], self.days[:1])

    def test_panel_asof_rebuilds_labels_at_the_cut(self):
        panel, _ = fixture_panel("2026-11-12")
        day = str(panel["p"]["close"].index[-3].date())
        cut = panel_asof(panel, day, 5)
        self.assertEqual(str(cut["U"].index[-1].date()), day)
        self.assertTrue(cut["p"]["fwd_horizon"].iloc[-6:].isna().all().all())
        self.assertTrue(cut["p"]["fwd_horizon"].iloc[:-6].notna().all().all())
        with self.assertRaises(Hold):
            panel_asof(panel, "1999-01-04", 5)


class WalkTests(unittest.TestCase):
    """A short fixture walk proves the plumbing; the real 30-session cached replay is a CLI run."""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.panel, cls.snapshot = fixture_panel("2026-11-12")
        close = cls.panel["p"]["close"]
        from research.factors import market_beta
        cls.beta = market_beta(close, close["SPY"].pct_change(), int(cls.config["beta_window"]))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cls.daily, cls.targets = walk(cls.config, cls.panel, cls.beta, cls.snapshot["eligibility"],
                                         close.index[-8:-2], 4, root / "cache", root / "state")
        cls.summary = summarize(cls.daily, cls.config, 100000.)

    def test_walk_produces_contract_targets_without_breaches(self):
        self.assertEqual(len(self.daily), 6)
        self.assertEqual(self.summary["invariant_breaches"], [])
        for target in self.targets:
            self.assertIn(target["status"], ("READY", "HOLD"))
            self.assertEqual(target["strategy_id"], STRATEGY_ID)
        ready = self.daily[self.daily.status == "READY"]
        self.assertGreater(len(ready), 0, self.daily.hold_reason.tolist())
        self.assertEqual((ready.nonzero_factors <= 0).sum(), 0)
        self.assertLessEqual(ready.short_names.max(), self.config["n_shorts"])
        self.assertLessEqual(ready[ready.rebalance_due].gross_short.max(), self.config["gross_short"] + 1e-9)
        self.assertLessEqual(ready.margin_used.max(), self.config["margin_cap"] + 1e-9)
        self.assertLessEqual(ready.largest_short.max(), self.config["max_name_weight"] + 1e-9)

    def test_hold_days_keep_the_book_instead_of_flattening(self):
        for index, (day, row) in enumerate(self.daily.iterrows()):
            if row.status != "HOLD":
                continue
            with self.subTest(day=day):
                self.assertEqual(row.orders, 0, row.to_dict())
                self.assertEqual(row.cost, 0.0)
                self.assertEqual(row.turnover, 0.0)
                if index and self.daily.status.iloc[index - 1] == "READY":
                    # A HOLD restates the drifted book; it never flattens the account.
                    self.assertEqual(names(self.targets[index], self.config),
                                     names(self.targets[index - 1], self.config))

    def test_costs_are_charged_on_the_side_that_traded(self):
        first = self.daily.iloc[0]
        self.assertEqual(first.orders, 0)                    # the walk starts flat, so day one is a HOLD
        self.assertEqual(first.status, "HOLD")
        traded = self.daily[self.daily.orders > 0]
        if len(traded):
            row = traded.iloc[0]
            self.assertGreater(row.cost, 0.0)
            self.assertGreater(row.carry, 0.0)               # a short book always pays borrow
            self.assertLess(row.net, row.gross)

    def test_summary_reports_the_window_it_asserted(self):
        self.assertEqual(self.summary["result_type"], "frozen-book-validation-replay")
        self.assertEqual(self.summary["asserted_sessions"], 4)
        self.assertEqual(self.summary["sessions_walked"], 6)
        self.assertEqual(self.summary["max_gross_short"], self.daily[self.daily.in_window &
                                                                    (self.daily.status == "READY") &
                                                                    self.daily.rebalance_due].gross_short.max()
                         if self.summary["rebalance_ready"] else None)
        disclosed = " ".join(self.summary["limitations"])
        self.assertIn("not original", disclosed)              # these are new replay numbers, not research ones
        self.assertIn("size a rebalance", disclosed)          # the caps are honest about drift days

    def test_artifacts_are_immutable_and_path_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            import live.replay as module
            original = module.ROOT
            try:
                module.ROOT = root
                with self.assertRaises(ValueError):
                    write_validation(self.daily, self.summary, root / "elsewhere")
                paths = write_validation(self.daily, self.summary, root / "reports/validation")
                self.assertEqual(paths, write_validation(self.daily, self.summary, root / "reports/validation"))
                paths[0].write_bytes(b"tampered")
                with self.assertRaises(FileExistsError):
                    write_validation(self.daily, self.summary, root / "reports/validation")
            finally:
                module.ROOT = original

    def test_summary_refuses_a_breached_window(self):
        daily = self.daily.copy()
        if (daily.in_window & (daily.status == "READY") & daily.rebalance_due).any():
            index = daily.index[daily.in_window & (daily.status == "READY") & daily.rebalance_due][0]
            daily.loc[index, "gross_short"] = 0.36
            with self.assertRaisesRegex(Hold, "gross_short"):
                summarize(daily, self.config, 100000.)


class CarryForwardProvenanceTests(unittest.TestCase):
    """A persisted anchor belongs to one data source, and one anchor serves a whole block.

    The synthetic fixture walk is dated with real business days, so training dates alone cannot tell it
    apart from a real-bar replay; `panel_identity` can, from the provenance each run publishes. The symbol
    list is deliberately not part of that identity: an anchor is frozen for its block, so a name listing or
    delisting must not make the job refuse the vectors it already published.
    """

    @staticmethod
    def _beta(close, config):
        from research.factors import market_beta
        return market_beta(close, close["SPY"].pct_change(), int(config["beta_window"]))

    def test_history_from_another_data_source_is_refused(self):
        config = CONFIG
        synthetic = panel_identity(config, {"source": SYNTHETIC_SOURCE})
        cached = panel_identity(config, {"bars_sha256": {"adjusted": "a", "raw": "b"}})
        fetched = panel_identity(config, {"source": "live/cache/bars", "through": "2026-11-18"})
        self.assertEqual(cached, fetched)              # real bars are real bars, whatever the file layout
        self.assertNotEqual(synthetic, cached)
        self.assertNotEqual(synthetic, panel_identity(config, None))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / f"coefficients-{config['mode']}.json"
            history = new_history(config, config["mode"])
            history["panel_identity"] = synthetic
            save_history(history, path)
            self.assertEqual(load_history(path, config, config["mode"], synthetic)["panel_identity"], synthetic)
            with self.assertRaisesRegex(Hold, "different data"):
                load_history(path, config, config["mode"], cached)

    def test_walk_halts_instead_of_replacing_a_foreign_state_file(self):
        config = CONFIG
        panel, snapshot = fixture_panel("2026-11-12")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            state.mkdir()
            history = new_history(config, config["mode"])
            history["panel_identity"] = panel_identity(config, {"bars_sha256": {"adjusted": "a"}})
            save_history(history, state / f"coefficients-{config['mode']}.json")
            _, targets = walk(config, panel, self._beta(panel["p"]["close"], config),
                              snapshot["eligibility"], panel["p"]["close"].index[-3:-2], 1,
                              root / "cache", state, inputs={"source": SYNTHETIC_SOURCE})
            self.assertTrue(all(t["status"] == "HOLD" for t in targets), targets)
            self.assertIn("different data", targets[0]["hold_reason"])

    def test_universe_drift_keeps_published_anchors(self):
        # A panel whose columns changed must still be served by the vectors already on record: the
        # coefficients are factor weights, so they apply to whatever names the universe now holds.
        config = CONFIG
        narrow, narrow_snapshot = fixture_panel("2026-11-12", stocks=120)
        wide, wide_snapshot = fixture_panel("2026-11-12", stocks=200)
        self.assertLess(len(narrow["U"].columns), len(wide["U"].columns))
        identity = panel_identity(config, {"source": SYNTHETIC_SOURCE})
        day = str(wide["p"]["close"].index[-1].date())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = state_path(config["mode"], root / "state")
            narrow_mask = tradable_mask(config, narrow_snapshot["eligibility"],
                                        narrow["p"]["mcap"].iloc[-1], narrow["U"].columns)
            _, _, _, anchor, _, history = score_panel(narrow, day, config, narrow_mask, root / "cache",
                                                      load_history(path, config, config["mode"], identity))
            save_history(history, path)
            fitted = sorted(p.name for p in (root / "cache" / "models").glob("*.json"))
            self.assertTrue(fitted, "the anchor session stored no model")
            _, _, _, reused, _, _ = score_panel(
                wide, day, config,
                tradable_mask(config, wide_snapshot["eligibility"], wide["p"]["mcap"].iloc[-1],
                              wide["U"].columns), root / "cache",
                load_history(path, config, config["mode"], identity))
            self.assertEqual(reused["coefficients"], anchor["coefficients"])
            self.assertEqual(sorted(p.name for p in (root / "cache" / "models").glob("*.json")), fitted)

    def test_newer_universe_vintage_cannot_refit_a_frozen_anchor(self):
        # Each session re-derives eligibility from today's market caps, which changes the training row
        # sample and therefore the fit. Inside an anchor's block the vector already on record wins;
        # re-fitting here would publish five different books for one anchor.
        config = CONFIG
        panel, snapshot = fixture_panel("2026-11-12")
        day = str(panel["p"]["close"].index[-1].date())
        flags, mcap, columns = snapshot["eligibility"], panel["p"]["mcap"].iloc[-1], panel["U"].columns
        today = tradable_mask(config, flags, mcap, columns)
        tomorrow = today.copy()
        tomorrow.loc[today[today].index[:3]] = False        # three names lose their borrow flag overnight
        self.assertEqual(int(tomorrow.sum()), int(today.sum()) - 3)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            identity = panel_identity(config, {"source": SYNTHETIC_SOURCE})
            path = state_path(config["mode"], root / "state")
            _, _, _, anchor, _, history = score_panel(panel, day, config, today, root / "cache",
                                                      load_history(path, config, config["mode"], identity))
            save_history(history, path)
            fitted = sorted(p.name for p in (root / "cache" / "models").glob("*.json"))
            self.assertTrue(fitted, "the anchor session stored no model")
            _, _, _, reused, _, _ = score_panel(panel, day, config, tomorrow, root / "cache",
                                                load_history(path, config, config["mode"], identity))
            self.assertEqual(reused["coefficients"], anchor["coefficients"])
            self.assertEqual(reused["input_checksum"], anchor["input_checksum"])
            self.assertEqual(sorted(p.name for p in (root / "cache" / "models").glob("*.json")), fitted)


if __name__ == "__main__":
    unittest.main()
