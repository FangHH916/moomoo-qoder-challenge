"""Tests for the packaged risk check: `skills/pstar-risk-check/scripts/risk_check.py`.

The script is the Risk Auditor's whole computation, and it ships in a zip to a machine that cannot import this
repository, so it is loaded by path and driven only with raw MCP envelopes. What these tests pin down is the
claim the skill makes: the numbers it posts to Slack are the ones `live/risk_monitor.py` computes - the module
`golive.py plan --risk-action` trusts - and every way a check can fail produces a report that says so instead
of silence.

Books price at $1,000 a share against $100,000 of initial assets, so a share count reads directly as a
percentage and every frozen threshold is visible: 39 shares is the trim line, 38 the cap, the margin factors
are 0.5 long and 0.6 short, and the halt is -8% of the peak. Symbols come from the real target book, because a
name outside it is itself an anomaly and would muddy what each case is there to prove.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live import golive
from live.contracts import load_strategy_config
from live.pause_switch import load_ops
from live.risk_monitor import actions, measure, warn_lines
from rules.gate import ACCOUNTS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "pstar-risk-check" / "scripts" / "risk_check.py"
CONFIG = load_strategy_config()
OPS = load_ops()
TARGETS = ROOT / "live" / "targets" / "competition" / "2026-11-19-pstar.json"
EQUITY = 100_000.0
PRICE = 1_000.0
DAY_ONE, DAY_TWO = "2026-11-23T14:35:00+00:00", "2026-11-24T14:35:00+00:00"

_spec = importlib.util.spec_from_file_location("pstar_risk_check", SCRIPT)
risk_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(risk_check)

# `golive.load_risk_report` refuses a report older than the freshness window, so the round trip below runs on
# the real clock; every other case pins --as-of and stays reproducible.
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat(timespec="seconds")
LONGS = ["SPY", "QQQ", "IWM", "USO", "UUP"]
SHORTS = ["AAOI", "ABSI", "ACHR", "AMSC", "AMZN", "APLD", "AUR", "AVAV"]


def envelope(data: dict) -> dict:
    return {"ret_code": 0, "ret_msg": "Ok", "data": data}


def position(symbol: str, quantity: int, price: float = PRICE) -> dict:
    """A raw position row: the broker reports a magnitude and a direction, never a signed quantity."""
    return {"symbol": f"US.{symbol}", "qty": abs(quantity), "price": price,
            "direction": 1 if quantity > 0 else 2, "mv": quantity * price}


def order(symbol: str, side: str = "BUY", qty=10, filled=0, status=2, tag="pstar-2026-11-19",
          created=None, order_id="1") -> dict:
    return {"order_id": str(order_id), "symbol": f"US.{symbol}", "side": 1 if side == "BUY" else 2,
            "qty": qty, "cum_qty": filled, "status": status, "text": tag, "create_time": created}


def monitor_rows(rows) -> list[dict]:
    """The same book in the shape `live/risk_monitor.py` is called with."""
    return [{"symbol": row["symbol"], "quantity": row["qty"] * (1 if row["direction"] == 1 else -1),
             "price": row["price"]} for row in rows]


class CheckRun(unittest.TestCase):
    """A temp directory of raw reads, and the script run over them exactly as an agent would run it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="pstar-risk-check-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, name: str, payload) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return path

    def clock(self, iso: str, equity: float, name: str = "prior-peak.json") -> Path:
        """A peak file whose one row is the day before the run, so the peak under test is the stored one."""
        day = datetime.fromisoformat(iso).astimezone(risk_check.ET).date() - timedelta(days=1)
        return self.write(name, {"account_id": ACCOUNTS["competition"], "initial_equity": EQUITY,
                                 "peak_equity": equity,
                                 "sessions": [{"day": day.isoformat(), "as_of": day.isoformat(),
                                               "equity": equity}]})

    def book(self, label, book, equity):
        """The run for one fixture book, with the peak seeded only where the drawdown rule needs it."""
        seeded = self.clock(NOW_ISO, equity=100_000.0, name=f"{label}-peak.json") if label else None
        return self.check(positions=book, equity=equity, peak_file=str(seeded) if seeded else "peak.json")

    def check(self, positions=(), equity=EQUITY, orders=None, as_of=DAY_TWO, peak_file="peak.json",
              eod_dir="eod", targets=TARGETS, extra=(), opened=True):
        self.write("cash.json", envelope({"account_id": ACCOUNTS["competition"], "total_asset": equity,
                                          "balance": 50_000.0}))
        self.write("positions.json", envelope({"account_id": ACCOUNTS["competition"],
                                               "positions": list(positions)}))
        # By default every holding has a filled pstar- ticket behind it, which is what makes it a strategy
        # position; a test about protection from `live/pause_switch.py`-style attribution passes opened=False.
        rows = list(orders or ()) + ([order(row["symbol"].removeprefix("US."),
                                            "BUY" if row["direction"] == 1 else "SELL", row["qty"], row["qty"],
                                            4, created=self.minutes_before(30, as_of), order_id=f"open{n}")
                                      for n, row in enumerate(positions)] if opened else [])
        self.write("orders.json", envelope({"account_id": ACCOUNTS["competition"], "orders": rows}))
        argv = ["--cash", str(self.root / "cash.json"), "--positions", str(self.root / "positions.json"),
                "--orders", str(self.root / "orders.json"),
                "--account", ACCOUNTS["competition"], "--config", str(ROOT / "live" / "config.pstar.json"),
                "--ops", str(ROOT / "live" / "ops.json"), "--as-of", as_of,
                "--out", str(self.root / "risk.json")]
        argv += ["--peak-file", str(self.root / peak_file) if peak_file else ""]
        argv += ["--eod-dir", str(self.root / eod_dir) if eod_dir else ""]
        if targets:
            argv += ["--targets", str(targets)]
        argv += list(extra)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = risk_check.main(argv)
        report = json.loads((self.root / "risk.json").read_text(encoding="utf-8"))
        report["_exit"], report["_stdout"] = code, buffer.getvalue()
        return report

    def kinds(self, report):
        return {item["kind"] for item in report["anomalies"]}

    def minutes_before(self, minutes, iso=DAY_TWO):
        return int(datetime.fromisoformat(iso).timestamp()) - 60 * minutes


class AgreementTests(CheckRun):
    """The report must be the same arithmetic as the module the gate and the planner read."""

    BOOKS = {
        "flat": ((), EQUITY),
        "clean": ((position("SPY", 10), position("AAOI", -4, 50.0)), EQUITY),
        "trim line": ((position("SPY", 39),), EQUITY),
        "trim breach": ((position("SPY", 40), position("QQQ", 5)), EQUITY),
        "margin breach": (tuple(position(name, -20) for name in SHORTS), EQUITY),
        "margin and trim": (tuple(position(name, -20) for name in SHORTS)
                              + (position("SPY", 41),), EQUITY),
        "warn only": ((position("SPY", 38), position("AAOI", -4, 50.0)), EQUITY),
        "many names": (tuple(position(name, -1) for name in SHORTS + LONGS), EQUITY),
        "drawdown breach": ((position("SPY", 10), position("AAOI", -4, 50.0),
                            position("ABSI", -6, 50.0)), 92_000.0),
    }

    def test_the_shared_books_measure_identically(self):
        for label, (book, equity) in self.BOOKS.items():
            with self.subTest(book=label):
                report = self.book(label.startswith("drawdown"), book, equity)
                state = measure(CONFIG, monitor_rows(book), equity, EQUITY, report["peak_equity"])
                self.assertEqual(report["measured"]["margin"], round(state["margin"], 6))
                self.assertEqual(report["measured"]["drawdown"], round(state["drawdown"], 6))
                self.assertEqual(report["measured"]["max_ticker_vs_initial"],
                                 round(state["max_ticker_vs_initial"], 6))
                self.assertEqual(report["measured"]["max_ticker"], state["max_ticker"])
                self.assertEqual([row["symbol"] for row in report["positions"]],
                                 sorted(row["symbol"].removeprefix("US.") for row in book))
                self.assertEqual(report["warnings"], warn_lines(CONFIG, OPS, state))
                self.assertEqual(report["share_changes"], actions(CONFIG, state)["share_changes"])
                breached = bool(actions(CONFIG, state)["share_changes"]) or state["drawdown"] <= \
                    CONFIG["drawdown_limit"] + 1e-12
                self.assertEqual(bool(report["findings"]), breached, report["findings"])

    def test_the_level_is_the_same_three_actions_the_monitor_describes(self):
        for label, book, equity, want in (
                ("nothing breached", (position("SPY", 10),), EQUITY, "OK"),
                ("warn only", (position("SPY", 38),), EQUITY, "WARN"),
                ("alert", (position("SPY", 40),), EQUITY, "ALERT"),
                ("kill", (position("SPY", 10),), 92_000.0, "KILL")):
            with self.subTest(case=label):
                seeded = self.clock(NOW_ISO, equity=100_000.0) if want == "KILL" else None
                report = self.check(positions=book, equity=equity,
                                  peak_file=str(seeded) if seeded else "peak.json")
                state = measure(CONFIG, monitor_rows(book), equity, EQUITY, report["peak_equity"])
                fresh = actions(CONFIG, state)
                self.assertEqual(report["level"], "KILL" if fresh["halt"] else
                                 "ALERT" if fresh["share_changes"] else
                                 "WARN" if warn_lines(CONFIG, OPS, state) else "OK")
                self.assertEqual(report["level"], want)
                self.assertEqual(report["_exit"], {"OK": 0, "WARN": 1, "ALERT": 1, "KILL": 1}[want])


class BreachTests(CheckRun):
    def test_a_name_above_39_percent_of_initial_assets_is_a_trim_to_the_cap(self):
        report = self.check(positions=[position("SPY", 40), position("QQQ", 5)])
        self.assertEqual(report["level"], "ALERT")
        self.assertEqual([item["rule"] for item in report["findings"]], ["ticker"])
        self.assertEqual(report["findings"][0]["value"], 0.4)
        self.assertEqual(report["findings"][0]["threshold"], CONFIG["trim_ticker_above_of_initial"])
        self.assertEqual(report["target_shares"], {"SPY": 38, "QQQ": 5})
        self.assertEqual(report["share_changes"], {"SPY": -2})
        self.assertEqual(report["_exit"], 1)
        self.assertIn("finding: SPY 40,000 USD is 40.0% of initial assets, trim to 38%", report["_stdout"])

    def test_margin_above_95_percent_scales_every_name_to_90(self):
        report = self.check(positions=tuple(position(name, -20) for name in SHORTS))  # 160,000 short = 96%
        self.assertAlmostEqual(report["measured"]["margin"], 0.96, places=9)
        self.assertEqual(report["level"], "ALERT")
        self.assertEqual([item["rule"] for item in report["findings"]], ["margin"])
        self.assertEqual(report["target_shares"], {name: -19 for name in SHORTS})
        self.assertEqual(report["share_changes"], {name: 1 for name in SHORTS})
        self.assertIn("= 96.0%, scale the book by 0.9375 to reach 90% margin", report["_stdout"])

    def test_eight_percent_off_the_peak_flattens_the_book_and_kills(self):
        report = self.check(positions=[position("SPY", 10), position("AAOI", -4, 50.0)],
                            equity=EQUITY, peak_file=str(self.clock(NOW_ISO, equity=120_000.0)))
        self.assertEqual(report["level"], "KILL")
        self.assertEqual([item["rule"] for item in report["findings"]], ["drawdown"])
        self.assertAlmostEqual(report["measured"]["drawdown"], round(100_000.0 / 120_000.0 - 1, 6), places=6)
        self.assertEqual(report["target_shares"], {"SPY": 0, "AAOI": 0})
        self.assertEqual(report["share_changes"], {"SPY": -10, "AAOI": 4})
        self.assertIn("- flatten and halt", report["_stdout"])
        self.assertIn("the pause switch is set by `golive.py plan --risk-action`", report["_stdout"])

    def test_a_position_with_no_pstar_order_is_reported_and_never_sized(self):
        filled = order("SPY", "BUY", 10, 10, 4, created=self.minutes_before(1))
        report = self.check(positions=[position("QQQ", 40)], orders=[filled], opened=False)
        self.assertEqual(report["protected"], ["QQQ"])
        self.assertEqual(report["share_changes"], {})
        self.assertEqual(report["level"], "WARN")               # 40% is past the 37% line, not the 39% trim
        self.assertEqual([item["rule"] for item in report["findings"]], ["ticker"])
        self.assertIn("not a strategy position: alert only", report["findings"][0]["detail"])
        self.assertIn("not opened by this strategy, alert only: QQQ", report["_stdout"])

    def test_a_protected_breach_still_produces_no_ticket_of_any_kind(self):
        report = self.check(positions=[position("QQQ", 40)], opened=False)   # no orders: QQQ is not ours
        self.assertEqual(report["protected"], ["QQQ"])
        self.assertEqual(report["share_changes"], {})
        self.assertEqual(report["level"], "WARN")
        self.assertEqual(report["_exit"], 1, "only a clean OK with no anomaly exits 0")


class AnomalyTests(CheckRun):
    def test_a_broker_rejection_is_an_anomaly_even_in_a_symbol_the_book_does_not_know(self):
        report = self.check(orders=[order("MOOO", status=6, tag="manual", created=self.minutes_before(5))])
        self.assertEqual(self.kinds(report), {"broker_rejection", "unexpected_symbol", "untagged_order"})
        self.assertIn("the broker rejected it (status 6)", report["_stdout"])

    def test_an_order_working_past_the_alert_line_is_an_anomaly(self):
        self.assertEqual(self.kinds(self.check(orders=[order("SPY", created=self.minutes_before(45))])),
                         {"order_working_too_long"})
        self.assertEqual(self.kinds(self.check(orders=[order("SPY", status=1,
                                                             created=self.minutes_before(29))])), set())

    def test_a_partial_fill_is_an_anomaly_because_the_residual_is_not_the_planned_ticket(self):
        self.assertEqual(self.kinds(self.check(orders=[order("SPY", qty=10, filled=4, status=3,
                                                             created=self.minutes_before(5))])), {"partial_fill"})

    def test_an_order_in_a_symbol_outside_the_book_is_an_anomaly(self):
        self.assertEqual(self.kinds(self.check(orders=[order("MOOO", created=self.minutes_before(5))])),
                         {"unexpected_symbol"})

    def test_a_pause_switch_sentinel_is_reported_as_the_position_that_it_is(self):
        sentinel = OPS["pause_switch"]["state"]["paused"].removeprefix("US.")
        report = self.check(positions=[position(sentinel, 10)], opened=False)
        item = [row for row in report["anomalies"] if row["kind"] == "unexpected_symbol"][0]
        self.assertIn("pause-switch sentinel", item["detail"])

    def test_an_untagged_order_that_is_not_discretionary_is_an_anomaly(self):
        report = self.check(orders=[order("SPY", tag="typed in the app", created=self.minutes_before(5))])
        self.assertEqual(self.kinds(report), {"untagged_order"})

    def test_a_discretionary_name_carries_no_tag_requirement_and_no_complaint(self):
        rows, rows_order = position("NVDA", 5), order("NVDA", tag="", created=self.minutes_before(5))
        strict = self.check(positions=[rows], orders=[rows_order], opened=False)
        self.assertEqual(self.kinds(strict), {"unexpected_symbol", "untagged_order"})
        self.write("config.json", {**CONFIG, "discretionary_symbols": ["NVDA"]})
        lenient = self.check(positions=[rows], orders=[rows_order], opened=False,
                             extra=["--config", str(self.root / "config.json")])
        self.assertEqual(self.kinds(lenient), set())
        self.assertEqual(lenient["level"], "OK")

    def test_the_order_budget_warns_on_both_the_daily_and_the_competition_limit(self):
        daily = self.check(orders=[order("SPY", created=self.minutes_before(1), order_id=n)
                                 for n in range(81)])
        self.assertEqual(self.kinds(daily), {"order_budget"})
        self.assertEqual(daily["measured"]["orders_today"], 81)
        self.assertIn("daily", daily["anomalies"][0]["detail"])
        self.assertIn("emergency reserve", daily["anomalies"][0]["detail"])
        # 161 orders on an earlier day of the window: none today, but the 200-order competition cap is in range.
        spread = self.check(orders=[order("SPY", created=int(datetime.fromisoformat(DAY_ONE).timestamp()) - n,
                                       order_id=n) for n in range(161)])
        self.assertEqual(spread["measured"]["orders_today"], 0)
        self.assertEqual(spread["measured"]["orders_since_start"], 161)
        budgets = [item for item in spread["anomalies"] if item["kind"] == "order_budget"]
        self.assertEqual(len(budgets), 1, spread["anomalies"])
        self.assertIn("competition", budgets[0]["detail"])

    def test_an_order_from_before_the_competition_opened_is_not_this_runs_spend(self):
        before = int(datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc).timestamp())
        report = self.check(orders=[order("MOOO", created=before)])
        self.assertEqual(report["anomalies"], [])
        self.assertEqual(report["measured"]["orders_since_start"], 0)
        self.assertEqual(report["measured"]["live_orders"], 1, "it is still a working order in the account")


class CheckFailedTests(CheckRun):
    """A check that could not read the account says so, exits 2, and hands nothing to the planner."""

    def setUp(self):
        super().setUp()
        self.write("cash.json", envelope({"account_id": ACCOUNTS["competition"], "total_asset": EQUITY}))
        self.write("positions.json", envelope({"positions": []}))
        self.write("orders.json", envelope({"orders": []}))

    def main(self):
        argv = ["--cash", str(self.root / "cash.json"), "--positions", str(self.root / "positions.json"),
                "--orders", str(self.root / "orders.json"), "--account", ACCOUNTS["competition"],
                "--config", str(ROOT / "live" / "config.pstar.json"), "--ops", str(ROOT / "live" / "ops.json"),
                "--as-of", DAY_TWO, "--peak-file", str(self.root / "peak.json"),
                "--out", str(self.root / "failed.json")]
        with redirect_stdout(io.StringIO()):
            code = risk_check.main(argv)
        return code, json.loads((self.root / "failed.json").read_text(encoding="utf-8"))

    def test_a_nonzero_ret_code_from_a_read_is_a_report_not_silence(self):
        self.write("cash.json", {"ret_code": 300400, "ret_msg": "risk fail", "data": {}})
        code, report = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(report["level"], "CHECK_FAILED")
        self.assertEqual([item["kind"] for item in report["anomalies"]], ["check_failed"])
        self.assertIn("sim_trade_cash_info failed with ret_code=300400", report["anomalies"][0]["detail"])
        self.assertIn("CHECK FAILED", report["slack_text"])
        self.assertIn("A failed check is never silent", report["slack_text"])
        self.assertIn("golive.py plan --risk-action", report["note"])

    def test_a_read_naming_the_other_paper_account_fails_the_check(self):
        self.write("cash.json", envelope({"account_id": ACCOUNTS["test"], "total_asset": EQUITY}))
        code, report = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(report["level"], "CHECK_FAILED")
        self.assertIn(f"not {ACCOUNTS['competition']}", report["anomalies"][0]["detail"])

    def test_a_file_that_is_not_json_fails_the_check(self):
        (self.root / "positions.json").write_text("{not json", encoding="utf-8")
        code, report = self.main()
        self.assertEqual(code, 2)
        self.assertIn("not readable JSON", report["anomalies"][0]["detail"])

    def test_a_holding_that_cannot_be_valued_fails_the_check(self):
        for row, needle in (({"symbol": "US.SPY", "qty": 1.5, "price": 100.0}, "fractional"),
                            ({"symbol": "US.SPY", "qty": 1}, "no usable price")):
            with self.subTest(row=row):
                self.write("positions.json", envelope({"positions": [row]}))
                code, report = self.main()
                self.assertEqual(code, 2)
                self.assertIn("SPY", report["anomalies"][0]["detail"])
                self.assertIn(needle, report["anomalies"][0]["detail"])

    def test_a_missing_read_fails_the_check(self):
        (self.root / "orders.json").unlink()
        code, report = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(report["level"], "CHECK_FAILED")


class PeakTests(CheckRun):
    def test_the_peak_survives_between_sessions_and_turns_a_good_day_into_a_kill(self):
        first = self.check(positions=[position("SPY", 10)], equity=120_000.0, as_of=DAY_ONE)
        self.assertEqual(first["level"], "OK")
        self.assertEqual(first["peak_equity"], 120_000.0)
        self.assertIn("running peak from", first["peak_basis"])
        self.assertIn("the paper cash API reports no peak", first["peak_basis"])
        # A separate process, a day later: only the file carries the 120,000 high-water mark into the -8% rule.
        second = self.check(positions=[position("SPY", 10)], equity=100_000.0, as_of=DAY_TWO)
        self.assertEqual(second["peak_equity"], 120_000.0)
        self.assertEqual(second["level"], "KILL")
        self.assertAlmostEqual(second["measured"]["drawdown"], round(100_000.0 / 120_000.0 - 1, 6), places=6)
        self.assertEqual([row["day"] for row in second["peak_history"]], ["2026-11-23", "2026-11-24"])
        self.assertEqual(second["previous_close_equity"], 120_000.0)
        self.assertEqual(json.loads((self.root / "peak.json").read_text(encoding="utf-8"))["peak_equity"],
                         120_000.0)
        self.assertIn("peak basis: running peak from", second["_stdout"])

    def test_a_second_run_the_same_day_replaces_its_own_row_instead_of_stacking(self):
        self.check(positions=[position("SPY", 10)], equity=100_000.0, as_of=DAY_ONE)
        again = self.check(positions=[position("SPY", 10)], equity=110_000.0, as_of=DAY_ONE)
        self.assertEqual(again["peak_equity"], 110_000.0)
        self.assertEqual([row["day"] for row in again["peak_history"]], ["2026-11-23"])

    def test_an_earlier_peak_is_never_lowered_by_a_bad_day(self):
        self.check(positions=[position("SPY", 10)], equity=120_000.0, as_of=DAY_ONE)
        after = self.check(positions=[position("SPY", 10)], equity=80_000.0, as_of=DAY_TWO)
        self.assertEqual(after["peak_equity"], 120_000.0)
        self.assertEqual(after["level"], "KILL")

    def test_without_a_writable_peak_the_previous_end_of_day_report_is_the_basis(self):
        self.write("eod/golive/2026-11-23/5832812/summary-1610.json", {"account_id": ACCOUNTS["competition"],
                                                                "equity": 118_000.0})
        report = self.check(positions=[position("SPY", 10)], equity=100_000.0, peak_file=None,
                          extra=["--no-write-peak"])
        self.assertEqual(report["peak_equity"], 118_000.0)
        self.assertEqual(report["previous_close_equity"], 118_000.0)
        self.assertIn("fallback", report["peak_basis"])
        self.assertIn("end-of-day reports", report["peak_basis"])
        self.assertIn("newest 2026-11-23", report["peak_basis"])
        self.assertIn("peak basis: fallback", report["_stdout"])
        self.assertEqual(report["level"], "KILL")

    def test_a_report_for_the_other_account_is_not_evidence_of_our_peak(self):
        self.write("eod/golive/2026-11-23/5322438/summary-1610.json", {"account_id": ACCOUNTS["test"],
                                                                "equity": 118_000.0})
        report = self.check(positions=[position("SPY", 10)], equity=100_000.0, peak_file=None,
                          extra=["--no-write-peak"])
        self.assertEqual(report["peak_equity"], EQUITY)
        self.assertIn("no peak file and no previous end-of-day report", report["peak_basis"])

    def test_with_neither_the_basis_is_the_initial_assets_and_it_says_so(self):
        report = self.check(positions=[position("SPY", 10)], equity=100_000.0, peak_file=None, eod_dir=None)
        self.assertEqual(report["peak_equity"], EQUITY)
        self.assertIn("no peak file and no previous end-of-day report", report["peak_basis"])
        self.assertIsNone(report["previous_close_equity"])


class RoundTripTests(CheckRun):
    """The file the auditor writes is the file the executor's planner reads, with the same verdict."""

    def snapshot(self, report):
        return {"account_id": ACCOUNTS["competition"], "mode": CONFIG["mode"], "equity": EQUITY,
                "initial_equity": EQUITY, "peak_equity": EQUITY,
                "positions": [{"symbol": row["symbol"], "quantity": row["quantity"], "price": row["price"]}
                              for row in report["positions"]]}

    def test_the_reports_level_and_peak_are_what_the_planner_acts_on(self):
        report = self.check(positions=[position("SPY", 300, 100.0), position("AAOI", -200, 100.0)],
                          orders=[order("SPY", "BUY", 300, 300, 4, created=self.minutes_before(30, NOW_ISO)),
                                  order("AAOI", "SELL", 200, 200, 4, created=self.minutes_before(30, NOW_ISO))],
                          equity=EQUITY, as_of=NOW_ISO, peak_file=str(self.clock(NOW_ISO, equity=110_000.0)))
        self.assertEqual(self.kinds(report), set(), report["anomalies"])
        self.assertEqual(report["protected"], [])
        self.assertEqual(report["level"], "KILL")
        read = golive.load_risk_report(self.root / "risk.json", CONFIG, OPS, ACCOUNTS["competition"],
                                       CONFIG["mode"], NOW, self.snapshot(report), [])
        self.assertEqual(read["level"], "KILL")
        self.assertEqual(read["fresh_level"], "KILL")
        self.assertEqual(read["peak_used"], 110_000.0)          # adopted: this snapshot never saw that high
        self.assertEqual(read["snapshot_peak"], EQUITY)
        self.assertIn("peak history", read["peak_source"])
        self.assertEqual(read["book_drift"], [])
        self.assertEqual(read["findings"], report["findings"])
        self.assertEqual(read["risk"]["share_changes"], {"SPY": -300, "AAOI": 200})

    def test_protection_in_the_report_does_not_hide_the_breach_the_planner_re_measures(self):
        report = self.check(positions=[position("QQQ", 40)], equity=EQUITY, as_of=NOW_ISO,
                            peak_file=str(self.clock(NOW_ISO, equity=110_000.0)), opened=False)
        self.assertEqual(report["level"], "KILL")
        self.assertEqual(report["protected"], ["QQQ"])
        self.assertEqual(report["share_changes"], {}, "the script sizes nothing in a protected name")
        read = golive.load_risk_report(self.root / "risk.json", CONFIG, OPS, ACCOUNTS["competition"],
                                       CONFIG["mode"], NOW, self.snapshot(report), [])
        self.assertEqual(read["fresh_level"], "KILL")
        self.assertEqual(read["risk"]["share_changes"], {"QQQ": -40})


class PackagingTests(unittest.TestCase):
    def test_the_script_imports_nothing_outside_the_standard_library(self):
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        top = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                top |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top.add(node.module.split(".")[0])
        self.assertTrue(top)
        self.assertEqual(sorted(top - set(sys.stdlib_module_names)), [],
                         "the packaged skill runs from a zip on a machine without this repository")

    def test_the_skill_folder_is_exactly_what_the_packager_will_zip(self):
        folder = SCRIPT.parents[1]
        self.assertTrue((folder / "SKILL.md").is_file(), "SKILL.md is the agent-facing half of the skill")
        self.assertRegex(folder.name, r"^[a-z0-9][a-z0-9_-]*$")
        entries = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
        self.assertEqual(entries, ["SKILL.md", "scripts/risk_check.py"])


if __name__ == "__main__":
    unittest.main()
