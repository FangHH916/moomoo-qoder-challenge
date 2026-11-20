"""Replay of the deployed strategy against the committed research records, one test per variant.

The reference files (reports/lab/R10_reference_targets.csv and R14_reference_targets.csv, written by
`python r10_deploy.py reference`) are the frozen research output. The replay recomputes them through the same
functions `python r10_deploy.py live` runs, so a change that moves a weight is caught here rather than in production.

The recorded window stops at the last session whose bars were downloaded after the close; the newest research
bar was pulled intraday and is excluded from the reference, so this replay covers the same 30 sessions and
nothing newer.

The research run needs the factor cache and the round-10 ETF snapshot under data/, which are not committed.
Without them the replay classes skip with a reason; the config-versus-research agreement tests always run.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research"
if str(RESEARCH) not in sys.path:
    sys.path.insert(0, str(RESEARCH))

import r10_deploy as R  # noqa: E402  the single source of truth; imported for its functions, never re-implemented

from live.contracts import load_strategy_config, pstar_variant_config

CONFIG = load_strategy_config("pstar")
VARIANTS = tuple(R.VARIANTS)
EQUITY = float(CONFIG["initial_equity"])
NO_CACHE = "the research cache and snapshot under data/ are not committed; the replay needs them"

TOLERANCE = 1e-9           # the reference is produced by this exact code, so any drift is a real difference


def research_available() -> bool:
    return (R.DATA / "cache").is_dir() and (R.SNAPSHOT / "bars_daily.parquet").is_file()


def reference(variant: str) -> pd.DataFrame:
    tag = R.VARIANTS[variant][2]
    return pd.read_csv(ROOT / "reports" / "lab" / f"{tag}_reference_targets.csv", parse_dates=["date"])


def deploy_record(variant: str) -> dict:
    tag = R.VARIANTS[variant][2]
    return json.loads((ROOT / "reports" / "lab" / f"{tag}_DEPLOY.json").read_text(encoding="utf-8"))


_REPLAYS: dict[str, dict] = {}


def replay(variant: str) -> dict:
    """Capped weights and per-session cap measurements for every session in the reference record."""
    if variant not in _REPLAYS:
        L, F, P = R.research()
        res = R.run_pstar(L, F, P, F.prior_scores(R.VARIANTS[variant][0]), variant)
        beta = L.df("beta")[R.ROUTE]
        rows, sessions = [], []
        for stamp in sorted(reference(variant).date.unique()):
            t = pd.Timestamp(stamp)
            weights = R.ticker_weights(res, t)
            agg = weights.groupby("symbol")["weight"].sum()
            capped, diag = R.apply_caps(agg, EQUITY, beta.loc[t].to_dict())
            sleeve = weights.groupby("symbol")["sleeve"].agg(lambda s: "+".join(sorted(set(s))))
            for name in agg.index.union(capped.index):
                rows.append({"date": t, "symbol": name, "sleeve": sleeve.get(name, "route"),
                             "weight": float(capped.get(name, 0.0))})
            sessions.append({"date": t, **{key: diag[key] for key in
                          ("margin", "positions", "n_short", "long_gross", "short_gross", "scale",
                           "spy_weight_uncapped")},
                          "longs": sorted(name for name in capped.index if capped[name] > 0)})
        _REPLAYS[variant] = {"weights": pd.DataFrame(rows), "sessions": pd.DataFrame(sessions)}
    return _REPLAYS[variant]


class ConfigAgreesWithResearch(unittest.TestCase):
    """The frozen constants in live/config.pstar.json are the research run's, so the live book cannot be
    sized under different rules than the backtest that produced it."""

    def test_the_deployed_variant_is_the_configured_one(self):
        self.assertEqual(CONFIG["variant"], R.DEPLOYED)
        self.assertEqual(set(CONFIG["variants"]), set(VARIANTS))

    def test_each_variant_block_matches_the_research_definition(self):
        for variant in VARIANTS:
            names, k_scale, tag = R.VARIANTS[variant]
            block = pstar_variant_config(dict(CONFIG, variant=variant))
            with self.subTest(variant=variant):
                self.assertEqual(block["factors"], names)
                self.assertEqual(block["k_scale"], k_scale)
                self.assertEqual(block["research_tag"], tag)
                self.assertEqual(block["strategy_id"], f"{tag.lower()}-{variant}")
                self.assertEqual(deploy_record(variant)["factors"], names)
                self.assertEqual(deploy_record(variant)["k_scale"], k_scale)

    def test_the_caps_in_the_config_are_the_research_limits(self):
        pairs = {"n_short": R.N_SHORT, "ticker_cap_of_initial": R.TICKER_CAP, "trim_ticker_above_of_initial": R.TRIM_AT,
                 "margin_cap": R.MARGIN_CAP, "delever_to": R.MARGIN_CAP, "delever_margin_above": R.MARGIN_ALERT,
                 "margin_long_factor": R.MARGIN_LONG, "margin_short_factor": R.MARGIN_SHORT,
                 "trade_band_of_equity": R.TRADE_BAND, "max_positions": R.MAX_POSITIONS,
                 "competition_order_cap": R.MAX_ORDERS, "initial_equity": R.INITIAL_EQUITY,
                 "rebalance_every": R.REBAL_EVERY, "order_platform_fee": R.FEE,
                 "short_borrow_apr": R.BORROW_APR, "financing_apr": R.FIN_APR}
        for name, value in pairs.items():
            self.assertEqual(CONFIG[name], value, name)
        self.assertEqual(CONFIG["stock_slippage_bps"], R.STOCK_SLIP * 1e4)
        self.assertEqual(CONFIG["etf_slippage_bps"], R.ETF_SLIP * 1e4)
        self.assertEqual(CONFIG["rebalance_anchor"], str(R.REBAL_ANCHOR.date()))
        self.assertEqual(CONFIG["etf_sleeve"], R.ETF9)
        self.assertEqual(CONFIG["spy_route"], R.ROUTE)


@unittest.skipUnless(research_available(), NO_CACHE)
class ReplayMatchesReference(unittest.TestCase):
    def test_pstar_capped_weights_reproduce_the_reference_record(self):
        self._check("pstar")

    def test_pstar15_capped_weights_reproduce_the_reference_record(self):
        self._check("pstar15")

    def _check(self, variant: str) -> None:
        ref = reference(variant)
        got = replay(variant)["weights"]
        self.assertEqual(sorted(ref.date.unique().tolist()), sorted(got.date.unique().tolist()))
        merged = ref.merge(got, on=["date", "symbol"], how="outer", suffixes=("_ref", "_replay"), indicator=True)
        self.assertEqual(merged["_merge"].value_counts().get("left_only", 0)
                         + merged["_merge"].value_counts().get("right_only", 0), 0,
                         "the replay holds a different set of names than the reference record")
        self.assertEqual(merged["sleeve_ref"].tolist(), merged["sleeve_replay"].tolist(),
                         "sleeve attribution, cap routing included, differs from the reference record")
        diff = (merged["weight_ref"] - merged["weight_replay"]).abs()
        self.assertLess(float(diff.max()), TOLERANCE,
                        f"{variant}: max capped-weight difference {float(diff.max()):.3e} over {len(diff)} rows")

    def test_the_replay_ends_at_the_reference_record_session(self):
        for variant in VARIANTS:
            with self.subTest(variant=variant):
                self.assertEqual(str(reference(variant).date.max().date()),
                                 str(replay(variant)["weights"].date.max().date()))
                self.assertEqual(len(reference(variant).date.unique()), 30,
                                 "the record is the 30 complete sessions before the newest (intraday) research bar")


@unittest.skipUnless(research_available(), NO_CACHE)
class CompetitionCaps(unittest.TestCase):
    """Every recorded session of each variant sits inside the competition limits the caps are supposed to enforce."""

    def test_no_ticker_above_38_percent_of_initial_assets(self):
        for variant in VARIANTS:
            weights = replay(variant)["weights"]
            worst = float(weights.weight.abs().max())
            with self.subTest(variant=variant):
                self.assertLessEqual(worst, CONFIG["ticker_cap_of_initial"] + TOLERANCE)
                self.assertLessEqual(worst * EQUITY, CONFIG["ticker_cap_of_initial"] * EQUITY + 1e-6)

    def test_margin_stays_at_or_below_90_percent(self):
        for variant in VARIANTS:
            sessions = replay(variant)["sessions"]
            with self.subTest(variant=variant):
                self.assertLessEqual(float(sessions.margin.max()), CONFIG["margin_cap"] + 1e-9)
                self.assertLessEqual(float(sessions.scale.max()), 1.0 + 1e-12)

    def test_positions_stay_at_or_below_100(self):
        for variant in VARIANTS:
            with self.subTest(variant=variant):
                self.assertLessEqual(int(replay(variant)["sessions"].positions.max()), CONFIG["max_positions"])

    def test_the_short_book_never_exceeds_77_names(self):
        for variant in VARIANTS:
            sessions = replay(variant)["sessions"]
            with self.subTest(variant=variant):
                self.assertEqual(int(sessions.n_short.max()), CONFIG["n_short"])
                self.assertEqual(int(sessions.n_short.min()), CONFIG["n_short"])

    def test_long_side_is_etfs_only(self):
        for variant in VARIANTS:
            weights = replay(variant)["weights"]
            longs = weights.loc[weights.weight > 0, "symbol"].unique().tolist()
            with self.subTest(variant=variant):
                self.assertEqual(sorted(set(longs) - set(R.ETFS)), [], "a single name is long")

    def test_the_book_is_uncapped_long_exposure_plus_the_short_sleeve(self):
        for variant in VARIANTS:
            weights = replay(variant)["weights"]
            sessions = replay(variant)["sessions"]
            gross = weights.groupby("date").weight.apply(lambda s: float(s.abs().sum()))
            with self.subTest(variant=variant):
                self.assertLess(float((sessions.long_gross + sessions.short_gross - gross.values).abs().max()),
                                1e-9, "the measured grosses are not the book the caps published")
                self.assertLessEqual(float(sessions.long_gross.max()), 1.0 + TOLERANCE,
                                     "the ETF sleeve is levered before the caps, which the sizing_equity contract forbids")
                self.assertGreater(float(gross.min()), 0.5, "the recorded window holds no exposure at all")


@unittest.skipUnless(research_available(), NO_CACHE)
class DeployRecordMatchesReplay(unittest.TestCase):
    """The summary the research run published for each variant is what the replay produces for its last session."""

    def test_last_session_summary(self):
        for variant in VARIANTS:
            record = deploy_record(variant)
            weights = replay(variant)["weights"]
            sessions = replay(variant)["sessions"]
            last = str(pd.Timestamp(record["last_complete_session"]).date())
            with self.subTest(variant=variant):
                self.assertIn(last, weights.date.dt.strftime("%Y-%m-%d").tolist())
                day = weights[weights.date.dt.strftime("%Y-%m-%d") == last]
                self.assertEqual(int(sessions[sessions.date == pd.Timestamp(last)].n_short.iloc[0]),
                                 record["last_session"]["n_shorts"])
                self.assertAlmostEqual(float(day.loc[day.weight < 0, "weight"].sum()),
                                       -record["last_session"]["short_gross"], places=12)
                etfs = day[day.symbol.isin(R.ETFS)].set_index("symbol")["weight"]
                for name, weight in record["last_session"]["etfs"].items():
                    self.assertAlmostEqual(float(etfs.get(name, 0.0)), weight, places=4)
                self.assertAlmostEqual(float(sessions.spy_weight_uncapped.max()),
                                       float(record["max_spy_weight_uncapped_30d"]), places=12)


if __name__ == "__main__":
    unittest.main()
