"""Focused offline tests for the frozen scorer: no full research panel, no data client.

The one end-to-end case builds the deterministic synthetic panel so the shipped book math,
signal guard and provenance fields can be checked without credentials or the research cache.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from live.contracts import digest, frozen_factor_names, load_config, seal, timestamp, validate_targets, verify, write_targets
from live.daily_scores import (HISTORY, Hold, _fetch_bars, adjustment_consistent, allocate_hedge, append_entries,
                               borrow_confirmed, broker_eligibility, build_book, carry_forward, eligible,
                               fit_coefficients, fit_day, fixture_panel, incremental_start, keep_drifted,
                               load_history, make_targets, margin_scale, new_history, normalize_bars, save_history,
                               score_row, select_shorts, sessions, signal_guard, smooth_scores, tradable_mask,
                               training_dates, training_rows, validate_bars, validate_config, validate_snapshot,
                               vol_multiplier)
from rules.trading_calendar import completed_session, exchange, rebalance_signal

CONFIG = load_config()
NAMES = frozen_factor_names(CONFIG)


def snapshot(day="2026-11-12", **overrides):
    base = {"as_of": exchange().session_close(day).isoformat(), "mode": "test", "account_id": 5322438,
            "account_type": "SIMULATE", "equity": 100000., "initial_equity": 100000.,
            "previous_close_equity": 100000., "peak_equity": 100000., "positions": [],
            "recent_daily_returns": [], "eligibility": {}}
    return {**base, **overrides}


def stock_flags(shortable=True, borrow=True, kind="COMMON_STOCK"):
    return {"security_type": kind, "shortable": shortable, "easy_to_borrow": borrow}


def universe(n=110, vol=.02):
    names = [f"S{i:03d}" for i in range(n)]
    return (pd.Series(np.arange(n, dtype=float), index=names), pd.Series(vol, index=names),
            pd.Series(True, index=names))


def bars(day="2026-11-12", names=("A", "B", "SPY", "QQQ")):
    return normalize_bars(pd.DataFrame([{"date": day, "symbol": c, "open": 10., "close": 11., "low": 9.,
                                         "high": 12., "vwap": 10.5, "volume": 10000., "trades": 100.}
                                        for c in names]))


class TimingTests(unittest.TestCase):
    def test_only_matured_labels_reach_the_training_window(self):
        dates = sessions("2026-11-12", 12)
        day = dates[-1].date().isoformat()
        chosen = training_dates(dates, day, count=2, horizon=5)
        self.assertEqual(list(chosen), list(dates[-9:-7]))
        # A 5-day target measured on the last training day settles six sessions later, on t-1.
        self.assertEqual(dates.get_loc(chosen[-1]) + 6, dates.get_loc(pd.Timestamp(day)) - 1)

    def test_short_training_history_holds(self):
        with self.assertRaises(Hold):
            training_dates(sessions("2026-11-12", 20), "2026-11-12")

    def test_weekly_anchor_uses_exchange_sessions(self):
        dates = exchange().sessions_in_range("2021-11-18", "2021-12-10")
        for i, day in enumerate(dates):
            self.assertEqual(rebalance_signal(str(day.date())), i % 5 == 0)
            self.assertEqual(fit_day(str(day.date())), str(dates[(i // 5) * 5].date()))

    def test_anchor_precedes_the_strategy(self):
        with self.assertRaises(Hold):
            fit_day("2021-11-17")

    def test_smoothing_preserves_current_missing(self):
        s = pd.DataFrame({"A": [1., 2., 3., 4., 5., 6., np.nan], "B": [np.nan, 2., 4., 6., 8., 10., 12.]})
        smoothed = smooth_scores(s, CONFIG)
        self.assertEqual(smoothed.A.iloc[5], 4.)
        self.assertTrue(np.isnan(smoothed.A.iloc[6]))
        self.assertEqual(smoothed.B.iloc[1], 2.)

    def test_partial_session_and_sgt_date(self):
        now = timestamp("2026-11-13T05:01:00+08:00")
        self.assertEqual(completed_session(now), "2026-11-12")
        self.assertEqual(completed_session(timestamp("2026-11-12T20:59:00Z")), "2026-11-11")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.now = timestamp("2026-11-12T22:00:00Z")

    def test_no_snapshot_does_not_assume_flat(self):
        result = make_targets(CONFIG, None, self.now)
        self.assertEqual(result["status"], "HOLD")
        self.assertIn("snapshot", result["hold_reason"])
        validate_targets(result, CONFIG)
        self.assertEqual(set(result["model_coefficients"]), set(NAMES))
        self.assertTrue(all(v == 0. for v in result["model_coefficients"].values()))
        self.assertEqual(result["position_snapshot_checksum"], digest(None))

    def test_wrong_account_and_live_account_rejected(self):
        for field, value in (("account_id", 5832812), ("mode", "competition"), ("account_type", "REAL")):
            with self.subTest(field=field):
                with self.assertRaises(Hold):
                    validate_snapshot(snapshot(**{field: value}), CONFIG, self.now, "2026-11-12")

    def test_competition_account_separate(self):
        config = {**CONFIG, "mode": "competition"}
        s = snapshot(mode="competition", account_id=5832812)
        self.assertTrue(validate_snapshot(s, config, self.now, "2026-11-12").empty)
        with self.assertRaises(Hold):
            validate_snapshot(snapshot(), config, self.now, "2026-11-12")

    def test_eod_previous_session_allowed_not_intraday_or_old(self):
        validate_snapshot(snapshot("2026-11-11"), CONFIG, self.now, "2026-11-12")
        for as_of in ("2026-11-12T20:00:00Z", "2026-11-10T22:00:00Z", "2026-11-13T22:00:00Z"):
            with self.subTest(as_of=as_of):
                with self.assertRaises(Hold):
                    validate_snapshot(snapshot(as_of=as_of), CONFIG, self.now, "2026-11-12")

    def test_snapshot_drift_is_current_equity_not_initial(self):
        s = snapshot(equity=80000., positions=[{"symbol": "US.A", "quantity": -40., "price": 100., "beta": 1.}])
        self.assertEqual(validate_snapshot(s, CONFIG, self.now, "2026-11-12")["A"], -.05)

    def test_snapshot_rejects_duplicates_and_untrusted_numbers(self):
        for s in (snapshot(positions=[{"symbol": "A", "quantity": 1., "price": 1., "beta": 1.},
                                      {"symbol": "A", "quantity": 1., "price": 1., "beta": 1.}]),
                  snapshot(equity=float("nan")), snapshot(peak_equity=50000., equity=80000.),
                  snapshot(positions="flat"), snapshot(eligibility=[]),
                  snapshot(recent_daily_returns=[-1.5])):
            with self.assertRaises(Hold):
                validate_snapshot(s, CONFIG, self.now, "2026-11-12")

    def test_broker_eligibility_and_borrow_are_explicit_booleans(self):
        self.assertEqual(broker_eligibility(snapshot(eligibility={"US.A": stock_flags()}))["A"], stock_flags())
        with self.assertRaises(Hold):
            broker_eligibility(snapshot(eligibility={"A": [], "US.A": {}}))
        self.assertTrue(borrow_confirmed(stock_flags()))
        for flags in (None, {}, {"shortable": True}, {"shortable": "true", "easy_to_borrow": True}):
            self.assertFalse(borrow_confirmed(flags))
        self.assertFalse(eligible(stock_flags(kind="ETF")))
        self.assertTrue(eligible(stock_flags(), short=True))
        self.assertFalse(eligible(stock_flags(borrow=False), short=True))

    def test_tradable_universe_needs_borrow_and_a_dollar_short_floor(self):
        columns = pd.Index(["A", "B", "C", "SPY"])
        flags = {"A": stock_flags(), "B": stock_flags(borrow=False), "C": stock_flags(), "SPY": stock_flags(kind="ETF")}
        mcap = pd.Series({"A": 2e9, "B": 2e9, "C": 999_999_999, "SPY": 5e11})
        mask = tradable_mask(CONFIG, flags, mcap, columns)
        self.assertEqual(list(mask[mask].index), ["A"])
        # float32 market-cap panels must not be rejected as "not a finite number".
        self.assertEqual(list(tradable_mask(CONFIG, flags, mcap.astype("float32"), columns)[mask].index), ["A"])

    def test_config_is_frozen(self):
        validate_config(CONFIG)
        for key, value in (("train_days", 251), ("factor_set", "v3"), ("options_enabled", True),
                           ("execution_enabled", True), ("gross_short", .70), ("n_shorts", 45),
                           ("hedge_cap_of_initial_equity", .45), ("margin_cap", .95),
                           ("single_stock_longs_allowed", True), ("factor_names", NAMES[:-1]),
                           ("hedge_instruments", ["SPY"])):
            with self.subTest(key=key):
                with self.assertRaises(Hold):
                    validate_config({**CONFIG, key: value})

    def test_immutable_output(self):
        result = make_targets(CONFIG, None, self.now)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / f"{result['signal_date']}.json"
            write_targets(path, result)
            write_targets(path, result)
            with self.assertRaises(FileExistsError):
                write_targets(path, seal({**result, "hold_reason": "different"}))
            self.assertEqual(json.loads(path.read_text()), result)


class BookTests(unittest.TestCase):
    def setUp(self):
        self.scores, self.vol, self.tradable = universe()

    def test_short_book_is_20_names_gross_35_and_never_long(self):
        w = select_shorts(self.scores, pd.Series(dtype=float), self.tradable, self.vol, CONFIG, 1.)
        self.assertEqual(len(w), CONFIG["n_shorts"])
        self.assertEqual(w.index.tolist(), [f"S{i:03d}" for i in range(20)])
        self.assertAlmostEqual(float(w.sum()), -.35)
        self.assertTrue((w < 0).all())

    def test_exact_tie_at_the_name_boundary_breaks_by_symbol_not_row_order(self):
        # Symbol order is used once and only once: two equal smoothed scores that straddle the
        # 20-name cut resolve identically whatever order the panel supplied the names in.
        scores = self.scores.copy()
        scores["S019"] = scores["S020"]                  # a tie exactly across the boundary
        chosen = select_shorts(scores, pd.Series(dtype=float), self.tradable, self.vol, CONFIG, 1.)
        self.assertIn("S019", chosen.index)
        self.assertNotIn("S020", chosen.index)
        shuffled = scores.sample(frac=1.0, random_state=7)
        repeated = select_shorts(shuffled, pd.Series(dtype=float), self.tradable, self.vol, CONFIG, 1.)
        self.assertEqual(repeated.index.tolist(), chosen.index.tolist())

    def test_volatility_multiplier_only_scales_new_entries(self):
        previous = pd.Series({"S000": -.03})
        w = select_shorts(self.scores, previous, self.tradable, self.vol, CONFIG, .5)
        self.assertEqual(w["S000"], -.03)
        self.assertAlmostEqual(w["S001"], -.0175 * .5)

    def test_inverse_vol_concentration_is_clipped_near_the_name_cap(self):
        self.vol["S000"] = .0001
        w = select_shorts(self.scores, pd.Series(dtype=float), self.tradable, self.vol, CONFIG, 1.)
        cap = .35 / 20 * 3
        self.assertGreater(abs(w["S000"]), abs(w["S001"]))
        self.assertLessEqual(abs(w["S000"]), cap * 1.06)
        self.assertAlmostEqual(float(w.sum()), -.35)

    def test_rank_buffer_keeps_held_names(self):
        previous = pd.Series({"S000": -.03, "S059": -.02})
        w = select_shorts(self.scores, previous, self.tradable, self.vol, CONFIG, 1.)
        self.assertIn("S059", w.index)                     # rank 60 stays inside the buffer
        self.assertNotIn("S060", w.index)                  # rank 61 does not
        self.assertEqual(w["S059"], -.02)

    def test_thin_universe_holds_instead_of_broadening(self):
        self.assertEqual(len(select_shorts(self.scores.iloc[:80], pd.Series(dtype=float), self.tradable.iloc[:80],
                                           self.vol.iloc[:80], CONFIG, 1.)), 20)
        with self.assertRaises(Hold):
            select_shorts(self.scores.iloc[:79], pd.Series(dtype=float), self.tradable.iloc[:79],
                          self.vol.iloc[:79], CONFIG, 1.)

    def test_hedge_takes_spy_first_then_qqq_on_its_own_beta(self):
        shorts = pd.Series(-.03, index=[f"S{i:03d}" for i in range(20)])           # beta 1 -> need .60
        beta = pd.Series(1., index=shorts.index, dtype=float)
        beta["QQQ"] = 1.1
        hedges, info = allocate_hedge(shorts, beta, CONFIG, 100000., 100000.)
        self.assertAlmostEqual(hedges["SPY"], .38)
        self.assertAlmostEqual(hedges["QQQ"], .2)
        self.assertAlmostEqual(info["hedge_scale"], 1.)
        self.assertAlmostEqual(info["residual_unhedged_beta"], 0., places=9)

    def test_hedge_capacity_beyond_both_etfs_shrinks_the_whole_book(self):
        shorts = pd.Series(-.05, index=[f"S{i:03d}" for i in range(20)])           # beta 1 -> need 1.0
        beta = pd.Series(1., index=shorts.index, dtype=float)
        beta["QQQ"] = 1.1
        _, info = allocate_hedge(shorts, beta, CONFIG, 100000., 100000.)
        self.assertGreater(info["residual_unhedged_beta"], 0.)
        self.assertAlmostEqual(info["hedge_scale"], .38 * 2.1, places=6)

    def test_hedge_cap_uses_initial_assets_not_current_equity(self):
        shorts = pd.Series(-.0035, index=[f"S{i:03d}" for i in range(20)])         # need .07 of beta
        beta = pd.Series(1., index=shorts.index, dtype=float)
        beta["QQQ"] = 1.1
        hedges, info = allocate_hedge(shorts, beta, CONFIG, 200000., 100000.)
        self.assertAlmostEqual(info["hedge_cap_of_equity"], .19)
        self.assertAlmostEqual(hedges["SPY"], .07)
        self.assertEqual(hedges["QQQ"], 0.)

    def test_unknown_qqq_beta_holds(self):
        shorts = pd.Series(-.03, index=[f"S{i:03d}" for i in range(20)])
        with self.assertRaises(Hold):
            allocate_hedge(shorts, pd.Series(1., index=shorts.index), CONFIG, 1e5, 1e5)

    def test_margin_cap_scales_both_sides(self):
        shorts = pd.Series(-.0175, index=[f"S{i:03d}" for i in range(20)])
        scale, info = margin_scale(shorts, {"SPY": 1.5, "QQQ": 0.}, CONFIG)
        self.assertAlmostEqual(info["margin_used"], .5 * 1.5 + .6 * .35)
        self.assertLess(scale, 1.)
        self.assertAlmostEqual(scale, .9 / info["margin_used"])
        scale, info = margin_scale(shorts, {"SPY": .35, "QQQ": 0.}, CONFIG)
        self.assertEqual(scale, 1.)
        self.assertAlmostEqual(info["margin_used"], .385)

    def test_build_book_is_short_only_beta_neutral_and_inside_every_cap(self):
        beta = pd.Series(.9, index=list(self.scores.index) + ["SPY", "QQQ"], dtype=float)
        beta["QQQ"] = 1.1
        shorts, hedges, info = build_book(self.scores, pd.Series(dtype=float), self.tradable, self.vol,
                                          beta, CONFIG, 1., 100000., 100000.)
        self.assertTrue((shorts < 0).all())
        self.assertAlmostEqual(info["gross_short"], .35)
        self.assertEqual(info["short_names"], 20)
        self.assertAlmostEqual(info["margin_used"], .5 * .315 + .6 * .35)
        self.assertLessEqual(abs(info["residual_unhedged_beta"]), .1)
        self.assertEqual(sorted(hedges), ["QQQ", "SPY"])
        self.assertLessEqual(max(hedges.values()), .38 + 1e-12)

    def test_drift_only_days_keep_the_book_and_require_current_borrow(self):
        previous = pd.Series({"S000": -.03, "SPY": .03})
        shorts, hedges = keep_drifted(previous, CONFIG, {"S000": stock_flags()})
        self.assertEqual(dict(shorts), {"S000": -.03})
        self.assertEqual(hedges, {"SPY": .03, "QQQ": 0.0})
        with self.assertRaises(Hold):
            keep_drifted(pd.Series({"S000": -.03}), CONFIG, {"S000": stock_flags(borrow=False)})
        with self.assertRaises(Hold):
            keep_drifted(pd.Series({"S000": .03}), CONFIG, {"S000": stock_flags()})

    def test_vol_targeting_floor_and_cap(self):
        self.assertEqual(vol_multiplier([], CONFIG), 1.)
        self.assertEqual(vol_multiplier([.1, -.1] * 20, CONFIG), CONFIG["vol_floor"])
        self.assertEqual(vol_multiplier([.001] * 20, CONFIG), 1.)


class GuardTests(unittest.TestCase):
    def row(self, values):
        return pd.Series(values, index=[f"S{i:03d}" for i in range(len(values))])

    def test_constant_scores_cannot_pick_names(self):
        guard = signal_guard(self.row([0.] * 120), CONFIG)
        self.assertFalse(guard["passed"])
        self.assertEqual(guard["distinct_values"], 1)

    def test_distinct_floor_and_dispersion(self):
        self.assertTrue(signal_guard(self.row(np.arange(120, dtype=float)), CONFIG)["passed"])
        self.assertFalse(signal_guard(self.row(np.arange(49, dtype=float)), CONFIG)["passed"])
        self.assertFalse(signal_guard(self.row(np.linspace(0., 1., 120) * 1e-15), CONFIG)["passed"])
        self.assertFalse(signal_guard(self.row([]), CONFIG)["passed"])

    def test_non_finite_and_missing_scores_are_ignored(self):
        row = self.row([float("inf")] + list(np.arange(1., 61.)) + [np.nan] * 10)
        guard = signal_guard(row, CONFIG)
        self.assertEqual(guard["distinct_values"], 60)
        self.assertEqual(guard["scored_names"], 60)
        self.assertTrue(guard["passed"])


class ModelTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        self.x = rng.normal(size=(60, 4))
        self.y = self.x[:, 0] * 2. + rng.normal(scale=.05, size=60)
        self.names = NAMES[:4]

    def test_fit_reports_the_used_coefficient_vector(self):
        result = fit_coefficients(self.x, self.y, self.names, {**CONFIG, "cv_splits": 3})
        self.assertEqual(set(result), {"coefficients", "alpha", "intercept", "nonzero_count", "all_zero"})
        self.assertEqual(set(result["coefficients"]), set(self.names))
        self.assertFalse(result["all_zero"])
        self.assertGreater(result["nonzero_count"], 0)
        self.assertGreater(abs(result["coefficients"][self.names[0]]), .5)

    def test_pure_noise_fit_is_flagged_all_zero(self):
        rng = np.random.default_rng(1)
        result = fit_coefficients(rng.normal(size=(60, 4)), rng.normal(scale=1e-9, size=60),
                                 self.names, {**CONFIG, "cv_splits": 3})
        self.assertTrue(result["all_zero"])
        self.assertEqual(result["nonzero_count"], 0)

    def test_carry_forward_reuses_the_last_nonzero_fit(self):
        good = {"fit_date": "2026-11-05", "coefficients": {n: 1. for n in self.names},
                "all_zero": False, "alpha": .01, "train_dates": ["2026-11-05"]}
        zero = {"fit_date": "2026-11-12", "coefficients": {n: 0. for n in self.names},
                "all_zero": True, "alpha": .01, "train_dates": ["2026-11-12"]}
        resolved = carry_forward(["2026-11-05", "2026-11-12"], {good["fit_date"]: good, zero["fit_date"]: zero}, [])
        self.assertFalse(resolved["2026-11-05"]["carried_forward"])
        self.assertTrue(resolved["2026-11-12"]["carried_forward"])
        self.assertEqual(resolved["2026-11-12"]["coefficients"], good["coefficients"])
        self.assertEqual(resolved["2026-11-12"]["fit_date"], "2026-11-05")

    def test_first_all_zero_fit_holds_until_a_nonzero_fit_exists(self):
        zero = {"fit_date": "2026-11-12", "coefficients": {n: 0. for n in self.names}, "all_zero": True}
        with self.assertRaises(Hold):
            carry_forward(["2026-11-12"], {zero["fit_date"]: zero}, [])
        persisted = [{"fit_date": "2026-10-01", "coefficients": {n: 1. for n in self.names}, "all_zero": False,
                      "alpha": .02}]
        resolved = carry_forward(["2026-11-12"], {zero["fit_date"]: zero}, persisted)
        self.assertTrue(resolved["2026-11-12"]["carried_forward"])
        self.assertEqual(resolved["2026-11-12"]["fit_date"], "2026-10-01")

    def test_training_rows_require_coverage_and_sample_deterministically(self):
        dates = pd.DatetimeIndex(pd.date_range("2026-10-01", periods=5, freq="B"))
        columns = ["A", "B", "C"]
        features = {n: pd.DataFrame(np.arange(15, dtype=float).reshape(5, 3) / 15, index=dates,
                                    columns=columns) for n in self.names}
        target = pd.DataFrame(.01, index=dates, columns=columns)
        tradable = pd.DataFrame(True, index=dates, columns=columns)
        config = {**CONFIG, "min_fit_samples_per_session": 2, "max_training_rows": 6}
        x, y, info = training_rows(features, target, tradable, dates, self.names, config)
        self.assertEqual(info["training_rows"], 6)
        self.assertEqual(info["training_rows_before_sample"], 15)
        self.assertEqual(info["train_start"], "2026-10-01")
        self.assertEqual(info["train_end"], "2026-10-07")
        np.testing.assert_array_equal(x, training_rows(features, target, tradable, dates, self.names,
                                                       config)[0])
        tradable.iloc[1] = False
        with self.assertRaises(Hold):
            training_rows(features, target, tradable, dates, self.names, config)

    def test_score_row_requires_raw_inputs_for_active_coefficients(self):
        columns = pd.Index(["A", "B"])
        value = pd.Series(1., index=columns)
        normalized = {n: value.copy() for n in self.names}
        raw = {n: value.copy() for n in self.names}
        tradable = pd.Series(True, index=columns)
        coefficients = {n: 0. for n in self.names}
        coefficients[self.names[0]] = 2.
        np.testing.assert_array_equal(score_row(normalized, raw, tradable, coefficients, self.names,
                                                minimum=2).to_numpy(), [2., 2.])
        raw[self.names[0]]["A"] = np.nan
        result = score_row(normalized, raw, tradable, coefficients, self.names, minimum=1)
        self.assertTrue(np.isnan(result["A"]))
        self.assertEqual(result["B"], 2.)
        raw[self.names[0]] = pd.Series(np.nan, index=columns)
        with self.assertRaises(Hold):
            score_row(normalized, raw, tradable, coefficients, self.names, minimum=2)
        with self.assertRaises(Hold):
            score_row(normalized, raw, tradable, dict.fromkeys(self.names, 0.), self.names, minimum=1)
        with self.assertRaises(Hold):
            score_row(normalized, raw, tradable, {self.names[0]: 1.}, self.names, minimum=1)

    def test_state_history_round_trip_is_refused_when_foreign(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "coefficients-test.json"
            self.assertEqual(load_history(path, CONFIG, "test"), new_history(CONFIG, "test"))
            entry = {"fit_date": "2026-11-05", "coefficients": dict.fromkeys(NAMES, 0.), "all_zero": False}
            entry["coefficients"]["pv1"] = 1.
            history = append_entries(new_history(CONFIG, "test"), {"2026-11-05": entry})
            save_history(history, path)
            self.assertEqual(load_history(path, CONFIG, "test")["entries"], [entry])
            with self.assertRaises(Hold):
                load_history(path, CONFIG, "competition")
            with self.assertRaises(Hold):
                load_history(path, {**CONFIG, "factor_names": NAMES[:-1]}, "test")
            save_history(seal({**history, "entries": [{**entry, "coefficients": dict.fromkeys(NAMES, 0.)}]}), path)
            with self.assertRaises(Hold):
                load_history(path, CONFIG, "test")
            path.write_text(path.read_text().replace('"entries"', '"entries2"'), encoding="utf-8")
            with self.assertRaises(Hold):
                load_history(path, CONFIG, "test")
            nested = Path(temp) / "nested" / "deeper.json"
            save_history(history, nested)
            self.assertEqual(load_history(nested, CONFIG, "test")["entries"], [entry])


class DataTests(unittest.TestCase):
    def test_stale_incomplete_or_misaligned_bars_hold(self):
        valid = bars()
        validate_bars(valid, valid, "2026-11-12", CONFIG, minimum=2)
        for bad in (bars("2026-11-11"), valid.iloc[:-1], valid.assign(volume=np.nan),
                    valid.assign(high=8.), bars(names=("A", "B", "SPY")), bars(names=("A", "B", "QQQ"))):
            with self.assertRaises(Hold):
                validate_bars(valid, bad, "2026-11-12", CONFIG, minimum=2)
        with self.assertRaises(Hold):
            validate_bars(valid, valid, "2026-11-12", CONFIG, minimum=3)

    def test_timezone_normalization(self):
        frame = bars()
        frame["date"] = "2026-11-12T05:00:00Z"
        self.assertEqual(normalize_bars(frame).date.iloc[0], pd.Timestamp("2026-11-12"))

    def test_adjustment_revision_or_missing_overlap_fails(self):
        original = bars()
        revised = original.copy()
        revised["close"] *= .5
        self.assertTrue(adjustment_consistent(original, original, {"A", "B", "SPY", "QQQ"}))
        self.assertFalse(adjustment_consistent(original, revised, {"A"}))
        self.assertFalse(adjustment_consistent(original, original.iloc[:1], {"SPY"}))

    def test_fetch_pagination_without_client_import(self):
        pages = [{"bars": {"A": [{"t": "2026-11-11T05:00:00Z", "o": 10, "h": 12, "l": 9,
                                  "c": 11, "v": 1000, "n": 100, "vw": 10.5}]}, "next_page_token": "p2"},
                 {"bars": {"A": [{"t": "2026-11-12T05:00:00Z", "o": 10, "h": 12, "l": 9,
                                  "c": 11, "v": 1000, "n": 100, "vw": 10.5}]}}]
        calls = []

        class Response:
            def __init__(self, value):
                self.value = value

            def raise_for_status(self):
                pass

            def json(self):
                return self.value

        def get(url, params):
            calls.append(copy.deepcopy(params))
            return Response(pages[len(calls) - 1])

        fetched = _fetch_bars(["A"], "2026-11-11", "2026-11-12T21:00:00Z", "all", getter=get)
        self.assertEqual(len(fetched), 2)
        self.assertEqual(calls[1]["page_token"], "p2")
        pages[0]["next_page_token"] = "p2"
        with self.assertRaises(Hold):
            _fetch_bars(["A"], "2026-11-11", "2026-11-12T21:00:00Z", "all", getter=lambda u, params: Response(pages[0]))

    def test_incremental_refresh_reaches_gaps_missing_from_both_sources(self):
        dates = sessions("2026-11-12", HISTORY)
        original = pd.concat([bars(str(d.date())) for d in dates], ignore_index=True)
        gap = dates[HISTORY - 20]
        missing = original.loc[original.date != gap]
        self.assertEqual(incremental_start(missing, missing, "2026-11-12", CONFIG), str(gap.date()))
        self.assertEqual(incremental_start(original, original, "2026-11-12", CONFIG), str(dates[-5].date()))

    def test_make_targets_never_scores_a_stale_panel(self):
        panel = {"p": {"close": pd.DataFrame(index=[pd.Timestamp("2026-11-11")])},
                 "U": pd.DataFrame(index=[pd.Timestamp("2026-11-11")]), "industry": pd.Series(dtype=float)}
        with patch("live.daily_scores.score_panel") as score:
            result = make_targets(CONFIG, snapshot(), timestamp("2026-11-12T22:00:00Z"), panel)
        score.assert_not_called()
        self.assertEqual(result["status"], "HOLD")
        self.assertIn("Stale", result["hold_reason"])


class FixtureBookTests(unittest.TestCase):
    """One deterministic synthetic run; the assertions cover the shipped READY contract."""

    @classmethod
    def setUpClass(cls):
        cls.day = "2026-11-12"
        cls.now = exchange().session_close(cls.day).to_pydatetime()
        panel, snap = fixture_panel(cls.day)
        with tempfile.TemporaryDirectory() as temp:
            cls.result = make_targets(CONFIG, snap, cls.now, panel=panel, fixture=True,
                                      cache_dir=Path(temp), state_dir=Path(temp))
        cls.risk = cls.result.get("risk_overlay", {})

    def test_ready_book_matches_the_frozen_strategy(self):
        result = self.result
        self.assertEqual(result["status"], "READY", result.get("hold_reason"))
        weights = result["weights"]
        self.assertEqual(len(weights), 20)
        self.assertTrue(all(w < 0 for w in weights.values()))
        self.assertLessEqual(-sum(weights.values()), CONFIG["gross_short"] * result["vol_multiplier"] + 1e-9)
        self.assertNotIn("SPY", weights)
        self.assertNotIn("QQQ", weights)
        self.assertGreaterEqual(result["spy_hedge_weight"], 0.)
        self.assertGreaterEqual(result["qqq_hedge_weight"], 0.)
        self.assertLessEqual(max(result["spy_hedge_weight"], result["qqq_hedge_weight"]),
                             CONFIG["hedge_cap_of_initial_equity"] + 1e-9)
        self.assertLessEqual(self.risk["margin_used"], CONFIG["margin_cap"])
        self.assertLessEqual(abs(self.risk["book_beta_after_hedge"]), CONFIG["max_abs_beta"])
        self.assertLessEqual(result["coverage"]["target_longs"], 0)

    def test_ready_book_carries_full_provenance(self):
        result = self.result
        self.assertTrue(verify(result))
        validate_targets(result, CONFIG)
        self.assertEqual(result["factor_names"], NAMES)
        self.assertEqual(set(result["model_coefficients"]), set(NAMES))
        self.assertGreater(result["model_nonzero_factors"], 0)
        self.assertIsInstance(result["model_carried_forward"], bool)
        self.assertIsInstance(result["model_retrained_today"], bool)
        self.assertTrue(result["signal_guard"]["passed"])
        self.assertGreaterEqual(result["signal_guard"]["distinct_values"],
                                CONFIG["signal_guard_min_distinct"])
        self.assertEqual(result["train_end"], str(sessions(self.day, 8)[0].date()))
        self.assertEqual(result["fixture"], True)
        self.assertEqual(result["mode"], "test")

    def test_source_definitions_are_hashed(self):
        from live.daily_scores import SOURCE_HASHES
        self.assertEqual(set(SOURCE_HASHES), {"research/factors.py", "research/lab_factors.py",
                                              "research/pipeline.py", "research/pipeline_v3.py",
                                              "live/daily_scores.py"})
        self.assertTrue(all(len(v) == 64 for v in SOURCE_HASHES.values()))
        self.assertIn("holdout", " ".join(self.result["limitations"]).lower())


if __name__ == "__main__":
    unittest.main()
