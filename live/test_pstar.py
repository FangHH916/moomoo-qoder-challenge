"""Offline tests for the P* live entry point: the research command line, the run-file checks, the
conversion into the sealed targets contract and the discretionary split.

The two committed research runs (`reports/live/r10_targets_2026-11-18.json` and
`r14_targets_2026-11-18.json`) are the inputs, so nothing here starts a research run or touches a
broker. A test that needs the command line injects `runner`.
"""
from __future__ import annotations

import copy
import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live import pstar
from live.contracts import (load_strategy_config, pstar_variant_config, seal, source_checksum, verify,
                            validate_pstar_targets)

ROOT = Path(__file__).resolve().parents[1]
SESSION = "2026-11-18"
# During the 11-19 session the latest completed session is 11-18, which is what both run files record.
NOW = datetime(2026, 11, 19, 14, 35, tzinfo=timezone.utc)
VARIANTS = ("pstar", "pstar15")


def config_for(variant: str = "pstar15") -> dict:
    """The deployed config with one variant selected, pinned to the test account's mode.

    The live switch (`mode`) says which of the two paper accounts the deployment trades; these offline
    tests pair the config with `snapshot()` below (5322438, mode test), so following the live switch here
    would make every book a mode mismatch whenever the deployment points at the competition account.
    """
    return {**load_strategy_config("pstar"), "variant": variant, "mode": "test"}


def run_file(variant: str) -> dict:
    prefix = pstar_variant_config(config_for(variant))["targets_prefix"]
    return pstar.read_json(Path(__file__).resolve().parents[1] / "reports/live"
                           / f"{prefix}_targets_{SESSION}.json")


def snapshot(**updates) -> dict:
    base = {"mode": "test", "account_id": "5322438", "account_type": "SIMULATE", "equity": 100_000.0,
            "initial_equity": 100_000.0, "previous_close_equity": 100_000.0, "peak_equity": 100_000.0,
            "as_of": NOW.isoformat(), "positions": []}
    return {**base, **updates}


def target_book(config: dict, run: dict, **kwargs) -> dict:
    return pstar.make_targets(config, kwargs.pop("snapshot", snapshot()), NOW, run=run, **kwargs)


class Runner:
    """Records the argv and cwd of the injected research call instead of executing it."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls, self.returncode, self.stdout, self.stderr = [], returncode, stdout, stderr

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return type("Completed", (), {"returncode": self.returncode, "stdout": self.stdout,
                                      "stderr": self.stderr})()


class ResearchCommand(unittest.TestCase):
    def test_the_command_is_the_frozen_research_entry_point(self):
        command = pstar.build_command("pstar15", 123_456.75, {"TSLA": 5_000.0, "AAPL": 250.5}, config_for())
        self.assertEqual(command[1:3], ["r10_deploy.py", "live"])
        self.assertEqual(command[3:7], ["--variant", "pstar15", "--equity", "123456.75"])
        self.assertEqual(command[7:], ["--hold", "AAPL=250.50", "--hold", "TSLA=5000.00"])
        self.assertEqual(Path(command[0]).name, pstar.python_executable().name)

    def test_the_research_command_runs_from_the_research_directory(self):
        runner = Runner()
        pstar.run_research(pstar.build_command("pstar", 100_000.0, {}, config_for("pstar")), runner=runner)
        command, kwargs = runner.calls[0]
        self.assertEqual(Path(kwargs["cwd"]).name, "research")
        self.assertEqual(kwargs["timeout"], config_for("pstar")["live_run_timeout_seconds"])
        self.assertIn("--skip-events", pstar.build_command("pstar", 1e5, {}, config_for("pstar"), True))

    def test_a_failed_research_run_is_a_hold(self):
        runner = Runner(returncode=1, stderr="data refreshed failed\nboom")
        with self.assertRaises(pstar.Hold) as caught:
            pstar.run_research(["r10_deploy.py", "live"], runner=runner)
        self.assertIn("exit 1", str(caught.exception))
        self.assertIn("boom", str(caught.exception))

    def test_the_frozen_deployment_choice_is_recorded(self):
        self.assertEqual(pstar.deployed_variant(), config_for()["variant"])


class RunFileValidation(unittest.TestCase):
    def setUp(self):
        self.config = config_for()
        self.run = run_file("pstar15")

    def valid(self, run=None):
        pstar.validate_run(run or self.run, self.config, pstar_variant_config(self.config), SESSION, NOW)

    def test_the_committed_runs_are_accepted_for_their_own_variant(self):
        for variant in VARIANTS:
            with self.subTest(variant=variant):
                config = config_for(variant)
                pstar.validate_run(run_file(variant), config, pstar_variant_config(config), SESSION, NOW)

    def test_a_run_of_the_other_variant_is_refused(self):
        for variant in VARIANTS:
            with self.subTest(variant=variant):
                config = config_for(variant)
                foreign = run_file("pstar15" if variant == "pstar" else "pstar")
                with self.assertRaises(pstar.Hold):
                    pstar.validate_run(foreign, config, pstar_variant_config(config), SESSION, NOW)

    def test_the_round_ten_run_predates_the_variant_field_and_is_read_from_its_strategy_id(self):
        run = run_file("pstar")
        self.assertNotIn("variant", run)
        self.assertNotIn("factors", run)
        config = config_for("pstar")
        pstar.validate_run(run, config, pstar_variant_config(config), SESSION, NOW)

    def test_every_field_the_deployment_needs_is_required(self):
        for key in sorted(self.run):
            with self.subTest(key=key):
                run = copy.deepcopy(self.run)
                run.pop(key)
                if key in ("variant", "factors", "hold", "trade_at", "signal_guard_held_previous_book"):
                    continue        # optional in the older record, so its absence must not fail
                with self.assertRaises(pstar.Hold):
                    self.valid(run)

    def test_a_missing_field_is_a_hold(self):
        run = copy.deepcopy(self.run)
        run.pop("diagnostics")
        with self.assertRaises(pstar.Hold) as caught:
            self.valid(run)
        self.assertIn("diagnostics", str(caught.exception))

    def test_the_refusals_that_keep_a_stale_or_breaching_run_out(self):
        def mutate(run):
            run["schema"] = 2

        cases = {
            "unsupported run schema": mutate,
            "wrong variant": lambda run: run.update(variant="pstar"),
            "wrong strategy id": lambda run: run.update(strategy_id="r9-pstar"),
            "wrong scale": lambda run: run.update(k_scale=self.run["k_scale"] + 0.01),
            "wrong composite": lambda run: run.update(factors=["bm"]),
            "not the latest completed session": lambda run: run.update(session="2026-11-17"),
            "timestamped in the future": lambda run: run.update(
                generated_at_utc=(NOW + timedelta(hours=1)).isoformat()),
            "bars that stop short of the session": lambda run: run["data"].update(last_bar_session="2026-11-17"),
            "checksum of different targets": lambda run: run.update(checksum_sha256="0" * 64),
            "failing diagnostics": lambda run: run["diagnostics"].update(ok=False),
            "diagnostics on other equity": lambda run: run["diagnostics"].update(equity=90_000.0),
            "too many positions": lambda run: run["diagnostics"].update(positions=101),
            "too many shorts": lambda run: run["diagnostics"].update(n_short=78),
            "margin breach": lambda run: run["diagnostics"].update(margin=0.95),
            "no research sessions compared": lambda run: run["consistency_with_research"].update(
                sessions_compared=0),
            "sleeve weights off the research run": lambda run: run["consistency_with_research"].update(
                sleeve_weight_max_abs_diff=0.002),
            "daily returns off the research run": lambda run: run["consistency_with_research"].update(
                return_max_abs_diff=0.0002),
            "unexpected target row field": lambda run: run["targets"][0].update(shares=10),
            "target row side contradicting its weight": lambda run: run["targets"][0].update(
                side="short" if run["targets"][0]["weight"] > 0 else "long"),
            "target row dollars contradicting its weight": lambda run: run["targets"][0].update(
                dollars=self.run["targets"][0]["dollars"] * 2),
        }
        for label, change in cases.items():
            with self.subTest(case=label):
                run = copy.deepcopy(self.run)
                change(run)
                with self.assertRaises(pstar.Hold):
                    self.valid(run)

    def test_the_run_checksum_is_the_research_own_seal(self):
        self.assertEqual(self.run["checksum_sha256"], source_checksum(self.run["targets"]))

    def test_an_unreadable_run_file_is_a_hold_not_a_crash(self):
        with self.assertRaises(pstar.Hold):
            pstar.read_json(Path(__file__).with_name("does-not-exist.json"))
        path = Path(__file__).with_name("_duplicate_key.json")
        path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
        try:
            with self.assertRaises(pstar.Hold):
                pstar.read_json(path)
        finally:
            path.unlink()

    def test_the_newest_run_file_wins_and_an_empty_directory_holds(self):
        config = self.config
        block = pstar_variant_config(config)
        prefix = block["targets_prefix"]
        self.assertEqual(pstar.run_path(config, block, SESSION).name, f"{prefix}_targets_{SESSION}.json")
        # reports/live/ gains a new run file every session, so it cannot be the fixture for
        # "newest wins"; stage the committed files instead.
        stage = ROOT / ".cache" / "tmp" / "run-dir"
        stage.mkdir(parents=True, exist_ok=True)
        for path in sorted((ROOT / "reports/live").glob(f"{prefix}_targets_*.json")):
            shutil.copyfile(path, stage / path.name)
        staged = {**config, "targets_dir": str(stage)}
        expected = sorted(q.name for q in stage.glob(f"{prefix}_targets_*.json"))[-1]
        expected = expected[len(prefix) + 9:len(prefix) + 19]
        self.assertEqual(pstar.newest_run(staged, block).name, f"{prefix}_targets_{expected}.json")
        self.assertGreaterEqual(expected, SESSION)
        empty = Path(__file__).resolve().parents[1] / ".cache" / "empty-runs"
        empty.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(pstar.Hold):
            pstar.newest_run({**config, "targets_dir": str(empty)}, block)


class Conversion(unittest.TestCase):
    def setUp(self):
        self.config = config_for()
        self.run = run_file("pstar15")

    def test_a_verified_run_becomes_the_sealed_targets_contract(self):
        payload = target_book(self.config, self.run)
        self.assertEqual(payload["status"], "READY", payload.get("hold_reason"))
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual([payload["strategy"], payload["variant"], payload["strategy_id"]],
                         ["pstar", "pstar15", "r14-pstar15"])
        self.assertEqual([payload["signal_date"], payload["session"]], [SESSION, SESSION])
        self.assertEqual(len(payload["weights"]), len(self.run["targets"]))
        self.assertEqual(len([w for w in payload["weights"].values() if w < 0]), self.config["n_short"])
        self.assertFalse(payload["rebalance_due"])
        self.assertEqual([payload["last_rebalance_session"], payload["sessions_until_next_rebalance"]],
                         ["2026-11-12", 1])
        self.assertEqual(payload["sizing_equity"], self.run["equity_input"])
        self.assertEqual(payload["initial_equity"], self.run["initial_equity"])
        self.assertEqual(payload["source"]["checksum_sha256"], self.run["checksum_sha256"])
        self.assertEqual(payload["source"]["file"], f"reports/live/r14_targets_{SESSION}.json")
        self.assertEqual(payload["factors"], pstar_variant_config(self.config)["factors"])
        self.assertTrue(verify(payload) and payload["config_checksum"] == pstar.digest(self.config))
        validate_pstar_targets(payload, self.config)

    def test_the_conversion_carries_weights_dollars_and_sleeves_row_by_row(self):
        payload = target_book(self.config, self.run)
        for row in self.run["targets"]:
            name = row["symbol"]
            self.assertAlmostEqual(payload["weights"][name], row["weight"], places=12)
            self.assertAlmostEqual(payload["dollars"][name], row["dollars"], places=6)
            self.assertEqual(payload["sleeves"][name], row["sleeve"])
        self.assertEqual(payload["source_targets"], self.run["targets"])
        self.assertEqual(payload["sleeve_weights"], self.run["sleeve_weights"])
        self.assertEqual(payload["diagnostics"], self.run["diagnostics"])

    def test_the_run_is_never_rerun_when_the_book_is_reconverted(self):
        runner = Runner(returncode=1)
        payload = target_book(self.config, self.run, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(payload["status"], "READY")

    def test_a_hold_publishes_an_empty_but_valid_contract(self):
        cases = {
            "the wrong account for the mode": lambda: snapshot(mode="competition", account_id="1"),
            "no equity": lambda: snapshot(equity=None),
            "the same name twice": lambda: snapshot(positions=[{"symbol": "AAPL", "quantity": 1, "price": 100.0},
                                                              {"symbol": "US.AAPL", "quantity": 1, "price": 100.0}]),
        }
        for label, state in cases.items():
            with self.subTest(case=label):
                payload = target_book(self.config, self.run, snapshot=state())
                self.assertEqual(payload["status"], "HOLD")
                self.assertEqual(payload["weights"], {})
                self.assertFalse(payload["rebalance_due"])
                self.assertTrue(payload["hold_reason"], payload)
                validate_pstar_targets(payload, self.config)

    def test_a_run_that_fails_validation_downgrades_to_hold(self):
        run = copy.deepcopy(self.run)
        run["diagnostics"]["ok"] = False
        payload = target_book(self.config, run)
        self.assertEqual(payload["status"], "HOLD")
        self.assertIn("diagnostics", payload["hold_reason"])

    def test_a_legacy_configuration_has_no_pstar_variant_to_convert(self):
        with self.assertRaises(ValueError):
            target_book(load_strategy_config("legacy-a"), self.run)
        config = {**self.config, "strategy": "legacy-a"}
        payload = target_book(config, self.run)
        self.assertEqual(payload["status"], "HOLD")
        self.assertIn("not pstar", payload["hold_reason"])

    def test_the_hold_reason_survives_a_reseal_and_the_checksum_covers_it(self):
        payload = target_book(self.config, self.run, snapshot=snapshot(equity=-5))
        self.assertTrue(verify(payload))
        self.assertNotEqual(payload["checksum"],
                            target_book(self.config, self.run, snapshot=snapshot(equity=-6))["checksum"])


class DiscretionaryAttribution(unittest.TestCase):
    def setUp(self):
        self.config = config_for()
        self.strategy_id = pstar_variant_config(self.config)["strategy_id"]

    def test_a_position_is_only_the_strategy_s_when_the_journal_says_so(self):
        records = [{"symbol": "AAOI", "strategy_id": self.strategy_id, "action": "ALLOW"},
                   {"symbol": "OLD", "strategy_id": "r9-pstar", "action": "ALLOW"},
                   {"symbol": "MAYBE", "strategy_id": self.strategy_id, "action": "REJECT"}]
        book = pstar.classify(snapshot(positions=[{"symbol": "AAOI", "quantity": -10, "price": 100.0},
                                                  {"symbol": "OLD", "quantity": 10, "price": 100.0},
                                                  {"symbol": "MAYBE", "quantity": 4, "price": 250.0}]),
                              self.config, self.strategy_id, records)
        self.assertEqual(book["strategy_opened"], ["AAOI"])
        self.assertEqual(book["unclassified"], ["MAYBE", "OLD"])
        self.assertEqual(book["discretionary"], [])
        self.assertEqual(book["hold"], {"OLD": 1_000.0, "MAYBE": 1_000.0})
        self.assertEqual(book["positions"]["AAOI"], -1_000.0)

    def test_the_whitelist_labels_a_name_discretionary_instead_of_unclassified(self):
        config = {**self.config, "discretionary_symbols": ["OLD"]}
        book = pstar.classify(snapshot(positions=[{"symbol": "OLD", "quantity": 2, "price": 500.0}]),
                              config, self.strategy_id, [])
        self.assertEqual([book["discretionary"], book["unclassified"]], [["OLD"], []])

    def test_the_strategy_hands_its_foreign_holdings_to_the_research_caps(self):
        runner = Runner()
        state = snapshot(positions=[{"symbol": "TSLA", "quantity": 5, "price": 400.0}])
        payload = pstar.make_targets(self.config, state, NOW, journal_records=[], runner=runner)
        command = runner.calls[0][0]
        self.assertEqual(command[command.index("--hold") + 1], "TSLA=2000.00")
        self.assertEqual(payload["discretionary"]["hold"], {"TSLA": 2_000.0})
        self.assertEqual(payload["discretionary"]["unclassified"], ["TSLA"])
        self.assertEqual(payload["status"], "READY", payload.get("hold_reason"))

    def test_an_unreadable_journal_is_a_hold(self):
        path = Path(__file__).with_name("_journal.jsonl")
        path.write_text('{"symbol": "A"}\nnot json\n', encoding="utf-8")
        try:
            with self.assertRaises(pstar.Hold):
                pstar.read_journal(path)
        finally:
            path.unlink()
        self.assertEqual(pstar.read_journal(Path(__file__).with_name("absent.jsonl")), [])


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.workdir = ROOT / ".cache" / "tmp" / "pstar-cli"
        self.workdir.mkdir(parents=True, exist_ok=True)
        # Stage the two fixture runs: the live reports/live/ directory is not a test fixture.
        self.runs = self.workdir / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        for variant in VARIANTS:
            prefix = pstar_variant_config(config_for(variant))["targets_prefix"]
            source = ROOT / "reports/live" / f"{prefix}_targets_{SESSION}.json"
            shutil.copyfile(source, self.runs / source.name)
        self.config_path = self.workdir / "config.staged.json"
        self.config_path.write_text(json.dumps({**config_for(), "targets_dir": str(self.runs)}),
                                    encoding="utf-8")

    def invoke(self, state: dict, *extra: str) -> tuple[int, dict]:
        snap = self.workdir / "snapshot.json"
        snap.write_text(json.dumps(state), encoding="utf-8")
        out = self.workdir / "targets.json"
        out.unlink(missing_ok=True)
        journal = self.workdir / "decisions.jsonl"
        journal.write_text("", encoding="utf-8")
        code = pstar.main(["--snapshot", str(snap), "--as-of", NOW.isoformat(), "--journal", str(journal),
                           "--config", str(self.config_path), "--no-run", "--out", str(out), *extra])
        return code, json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}

    def test_no_run_verifies_the_latest_committed_run_and_publishes_it(self):
        code, payload = self.invoke(snapshot())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "READY")
        self.assertTrue(verify(payload))
        self.assertEqual(Path(payload["source"]["file"]).name, f"r14_targets_{SESSION}.json")

    def test_the_variant_switch_is_the_only_difference(self):
        code, payload = self.invoke(snapshot(), "--variant", "pstar")
        self.assertEqual(code, 0)
        self.assertEqual(payload["variant"], "pstar")
        self.assertEqual(payload["strategy_id"], "r10-pstar")

    def test_a_mismatching_snapshot_exits_two_with_a_hold(self):
        code, payload = self.invoke(snapshot(account_id="5832812"))
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "HOLD")
        self.assertIn("account/mode mismatch", payload["hold_reason"])



class CommittedRuns(unittest.TestCase):
    """Convert every run file that actually sits in reports/live/, for its own session and variant.

    reports/live/ is the directory the live path reads and it gains one file per session, so this test
    walks what is present instead of assuming a fixed date. It is the only coverage of a real research
    run newer than the 2026-11-18 fixtures, including the 2026-11-19 rebalance book traded at the
    2026-11-20 open.
    """

    def test_each_committed_run_verifies_and_publishes(self):
        seen = []
        for source in sorted((ROOT / "reports/live").glob("r1?_targets_????-??-??.json")):
            variant = "pstar" if source.name.startswith("r10_") else "pstar15"
            session = source.name.split("_targets_")[1][:10]
            run = pstar.read_json(source)
            config = config_for(variant)
            # Midday on the day after the run's session: that session is then the latest completed
            # one, and the research run, which is written overnight, is no longer in the future. Both
            # halves of the freshness rule are what this checks.
            as_of = (datetime.strptime(session, "%Y-%m-%d").replace(hour=13, tzinfo=timezone.utc)
                     + timedelta(days=1))
            payload = pstar.make_targets(config, snapshot(as_of=as_of.isoformat()), as_of, run=run)
            with self.subTest(file=source.name):
                self.assertEqual(run["session"], session)
                self.assertEqual(payload["status"], "READY", payload.get("hold_reason"))
                self.assertEqual([payload["strategy_id"], payload["variant"]], [run["strategy_id"], variant])
                self.assertEqual(payload["rebalance_due"], bool(run["rebalance_session"]))
                validate_pstar_targets(payload, config)
                self.assertTrue(verify(payload))
                self.assertLessEqual(len(payload["weights"]), config["max_positions"])
                self.assertLessEqual(sum(1 for w in payload["weights"].values() if w < 0), config["n_short"])
                self.assertLessEqual(
                    max(abs(w) * payload["sizing_equity"] for w in payload["weights"].values()),
                    config["ticker_cap_of_initial"] * payload["initial_equity"] + 1e-9)
                longs = {name for name, weight in payload["weights"].items() if weight > 0}
                self.assertTrue(longs <= set(config["etf_universe"]), f"non-ETF long in {source.name}: {longs}")
                self.assertLessEqual(payload["diagnostics"]["margin"], config["margin_cap"] + 1e-9)
                seen.append(source.name)
        self.assertGreaterEqual(len(seen), 3, seen)


if __name__ == "__main__":
    unittest.main()
