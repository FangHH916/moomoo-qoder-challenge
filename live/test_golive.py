"""Fixtures and tests for the go-live tooling: real MCP response shapes, deterministic Python only.

Nothing here places an order or touches the broker. The raw response files are shaped exactly like the
payloads captured from the paper API on 2026-11-20: `ret_code`/`ret_msg`/`data` envelopes, numbers as
strings, order times in microseconds and quote times in milliseconds, and `sim_trade_max_buy_sell`
echoing no symbol. The target book comes through the production conversion path
(`live.pstar.make_targets(no_run=True)` on the newest committed research run), and prices are synthetic
but deterministic: a name is priced at `|weight| x equity`, so the frozen rounding rule sizes every
ticket at one whole share, and a price override is what moves a name into or out of that rule.

The fixture clock is the first tradeable moment of the newest committed session, and the raw files'
mtimes are stamped to it because `sim_trade_cash_info` carries no timestamp of its own.
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from journal.store import Journal
from live import golive, pause_switch
from live.contracts import digest, load_strategy_config, seal, timestamp
from live.order_planner import plan as planner_plan
from live.order_planner import target_shares
from live.pstar import make_targets
from live.relay import paper_payload
from rules.gate import ACCOUNTS
from rules.gate_pstar import evaluate_pstar_order
from rules.pstar_harness import committed_session, tradeable_clock

EQUITY = 100_000.0
SESSION = committed_session("pstar15")
CLOCK = tradeable_clock(SESSION)
STATUS_WORKING = 0        # a code moomoo documents nowhere: live only when the operator says --working 0
OPS = pause_switch.load_ops()
REG = pause_switch.registry(OPS)     # the deployed sentinel table, so a fixture cannot rename the switch


def envelope(data: dict) -> dict:
    return {"ret_code": 0, "ret_msg": "Ok", "data": data}


def paused_flag(fixture, extra=()) -> Path:
    """A raw PSTAR-PAUSE read that says paused, with the cloud executor holding the lease."""
    return fixture.write("flag.json", envelope({
        "group_name": "PSTAR-PAUSE",
        "security_list": [{"code": "US.SQQQ"}, {"code": "US.XLF"}] + [{"code": c} for c in extra]}))


class Fixture:
    """One scenario: the raw response files, a sealed target book, and an operator evidence file."""

    def __init__(self, root: Path, account: str = ACCOUNTS["test"], mode: str = "test", positions=(),
                 blocked=(), capacity=1000, capacities=None, fund_size=5_000_000_000.0, prices=None,
                 price_floor=5.0, borrow_proxy=True):
        self.root = Path(root)
        self.account, self.mode = account, mode
        self.positions, self.blocked = list(positions), list(blocked)
        self.capacity, self.capacities, self.fund_size = capacity, dict(capacities or {}), fund_size
        self.price_floor, self.price_overrides = price_floor, dict(prices or {})
        self.now = timestamp(CLOCK)
        self.config = {**load_strategy_config(), "mode": mode}
        self.targets = make_targets(self.config, {"mode": mode, "account_id": account,
                                                  "equity": EQUITY, "positions": []},
                                    self.now, no_run=True)
        if self.targets.get("rebalance_due") is not True:      # tonight is a rebalance session
            body = {k: v for k, v in self.targets.items() if k != "checksum"}
            self.targets = seal({**body, "rebalance_due": True})
        self.weights = self.targets["weights"]
        self.prices = {name: self.price_overrides.get(name,
                                                      max(round(abs(weight) * EQUITY, 2), self.price_floor))
                       for name, weight in self.weights.items()}
        whole = {name: int(round(weight * EQUITY / self.prices[name]))
                 for name, weight in self.weights.items()}
        # The book the tooling is expected to trade: every name the frozen rounding rule can size, minus
        # any name the fixture deliberately leaves without borrow capacity.
        self.expected_shares = {name: value for name, value in whole.items()
                                if value and name not in set(self.blocked)}
        self.orders: list[dict] = []
        self.evidence = self.full_evidence(borrow_proxy)
        self.paths: dict[str, Path] = {}
        self.write("config.json", self.config)
        self.write("accounts.json", self.account_list())
        self.write("cash.json", self.cash())
        self.write("positions.json", envelope({"positions": self.position_rows()}))
        self.write("orders.json", envelope({"orders": self.orders,
                                            "pagination": {"has_more": False, "next_key": ""}}))
        self.write("quotes.json", self.quotes())
        self.write("borrow.json", self.borrow_rows())
        self.write("targets.json", self.targets)
        self.write("evidence.json", self.evidence)

    # ------------------------------------------------------------------ clock
    def advance(self, seconds: int, refresh=("cash", "positions", "quotes", "evidence")) -> str:
        """Move the session clock and re-capture the files whose freshness the rules measure."""
        self.now = self.now + timedelta(seconds=seconds)
        payloads = {"cash": self.cash, "positions": lambda: envelope({"positions": self.position_rows()}),
                    "orders": lambda: envelope({"orders": self.orders, "pagination":
                                                {"has_more": False, "next_key": ""}}),
                    "quotes": self.quotes, "evidence": lambda: self.evidence,
                    "borrow": self.borrow_rows, "targets": lambda: self.targets}
        if "evidence" in refresh:
            self.evidence = {**self.evidence, "approval": {
                **self.evidence["approval"], "issued_at": self.iso,
                "expires_at": (self.now + timedelta(seconds=240)).isoformat()}}
        for key in refresh:
            self.write(f"{key}.json", payloads[key]())
        return self.iso

    @property
    def iso(self) -> str:
        return self.now.isoformat()

    def to_utc(self, iso: str) -> str:
        """Jump the session clock to an absolute instant, re-capturing everything that ages with it."""
        return self.advance(int((timestamp(iso) - self.now).total_seconds()))

    # ------------------------------------------------------------------ raw payloads
    def account_list(self) -> dict:
        return envelope({"total_count": 2, "accounts": [
            {"account_id": ACCOUNTS["test"], "account_name": "US Margin Account [Paper Trading]",
             "market_id": 100, "account_type": 1, "currency": "USD", "dp_opened": True},
            {"account_id": ACCOUNTS["competition"],
             "account_name": "Moomoo x Qoder AI Paper Trading Agent Challenge", "market_id": 100,
             "account_type": 2, "currency": "USD", "dp_opened": True}]})

    def cash(self) -> dict:
        long_gross = sum(shares * self.price_of(symbol) for symbol, shares in self.positions if shares > 0)
        short_gross = sum(-shares * self.price_of(symbol) for symbol, shares in self.positions if shares < 0)
        return envelope({
            "balance": f"{EQUITY - long_gross + short_gross:.2f}", "hold": "0.0",
            "long_mv": f"{long_gross:.2f}", "max_power_long": f"{EQUITY * 2:.2f}",
            "mv": f"{long_gross - short_gross:.2f}", "realized_profit": None,
            "short_mv": f"{short_gross:.2f}", "total_asset": f"{EQUITY:.2f}",
            "unrealized_profit": None})

    def position_rows(self) -> list:
        return [{"symbol": f"US.{symbol}", "qty": str(quantity),
                 "cost_price": f"{self.price_of(symbol) * 0.97:.2f}",
                 "mv": round(quantity * self.price_of(symbol), 2),
                 "last_price": f"{self.price_of(symbol):.2f}", "pl_ratio": "3.09",
                 "stock_name": symbol} for symbol, quantity in self.positions]

    def price_of(self, symbol: str) -> float:
        return self.prices.get(symbol, 50.0)

    def order_row(self, symbol: str, side: str, quantity: int, filled: int, client_id: str,
                  status: int = 4, at=None, order_id: str | None = None) -> dict:
        """One order-history row in the shape captured from 5322438 on 2026-11-20."""
        price = self.price_of(symbol)
        when = self.now if at is None else (at if hasattr(at, "timestamp") else timestamp(at))
        microseconds = int(when.timestamp() * 1_000_000)
        return {"aux_price": None, "avg_fill_price": f"{price:.2f}" if filled else "0.0",
                "create_time": str(microseconds), "cum_qty": str(filled), "market": 2,
                "order_id": order_id or str(int(golive.digest(client_id)[:6], 16) % 9_000_000 + 1_000_000),
                "order_trade_time_type": 1, "order_type": 1, "price": f"{price:.2f}",
                "qty": str(quantity), "side": 1 if side == "BUY" else 2, "status": status,
                "stock_name": symbol, "symbol": f"US.{symbol}", "text": client_id,
                "time_in_force": 0, "update_time": str(microseconds + 30 * 1_000_000)}

    def quotes(self) -> dict:
        etfs = set(self.config["etf_universe"])
        rows = []
        for name in sorted(set(self.weights) | etfs):
            price = self.price_of(name)
            rows.append({"code": f"US.{name}", "name": name, "last_price": f"{price:.2f}",
                         "bid_price": f"{price * 0.9999:.2f}", "ask_price": f"{price * 1.0001:.2f}",
                         "bid_vol": "100", "ask_vol": "120", "avg_price": f"{price:.2f}",
                         "prev_close_price": f"{price * 0.99:.2f}",
                         "update_time": int(self.now.timestamp() * 1000), "sec_status": "NORMAL",
                         "suspension": False, "lot_size": 1, "trust_valid": name in etfs,
                         "equity_valid": name not in etfs, "total_market_val": f"{self.fund_size:.2f}",
                         "trust_aum": f"{self.fund_size:.2f}" if name in etfs else None,
                         "net_asset": f"{self.fund_size:.2f}"})
        return envelope({"snapshot_list": rows})

    def borrow_rows(self) -> list:
        """max_buy_sell responses wrapped with their symbol: the API echoes no symbol of its own."""
        rows = []
        for name, weight in sorted(self.weights.items()):
            if weight >= 0:
                continue
            capacity = 0 if name in set(self.blocked) else self.capacities.get(name, self.capacity)
            rows.append({"symbol": f"US.{name}", "response": envelope({
                "max_buy_back_qty": str(capacity), "max_cash_buy_qty_round_lot": "0",
                "max_margin_buy_qty_round_lot": "0", "max_sell_qty_round_lot": "0",
                "max_sell_short_qty": str(capacity), "required_im_long": None,
                "required_im_short": None})})
        return rows

    def full_evidence(self, borrow_proxy: bool = True) -> dict:
        evidence = {"assertions": {"release_verified": True, "lease_verified": True,
                                   "permission_policy_verified": True, "reconciled": True,
                                   "snapshot_verified_by_relay": True},
                    "lease_owner": "desktop-chat-fixture",
                    "approval": {"approval_id": "fixture-confirm", "verified_by_relay": True,
                                 "auditor_session_id": "independent-auditor",
                                 "guard_session_id": "guard-chat", "reply": "CONFIRM",
                                 "confirmed_by": "user", "issued_at": self.iso,
                                 "expires_at": (self.now + timedelta(seconds=240)).isoformat(),
                                 "note": "fixture approval bound to the fixture plan, not a live "
                                         "confirmation"}}
        if borrow_proxy:
            evidence["borrow"] = {"easy_to_borrow_proxy":
                                  "sim_trade_max_buy_sell.max_sell_short_qty"}
        return evidence

    def write(self, name: str, payload) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        seconds = self.now.timestamp()
        os.utime(path, (seconds, seconds))
        self.paths[path.stem] = path
        return path

    def file(self, name: str) -> str:
        return str(self.paths[name])

    # ------------------------------------------------------------------ cli
    def args(self, *argv: str, as_of: str | None = None) -> list[str]:
        return ["--journal", str(self.root / "journal"), "--config", self.file("config"),
                "--as-of", as_of or self.iso, *argv]

    def invoke(self, *argv: str, as_of: str | None = None) -> int:
        """Run the command handler itself, so a refusal surfaces as the GoliveError the operator sees."""
        args = golive.parser().parse_args(self.args(*argv, as_of=as_of))
        return args.handler(args)

    def call(self, *argv: str, as_of: str | None = None) -> int:
        """Run the whole CLI: refusals are reported as `GOLIVE REFUSED` on stdout with exit code 2."""
        return golive.main(self.args(*argv, as_of=as_of))

    def snapshot(self) -> Path:
        self.invoke("snapshot", "--account", self.account, "--cash", self.file("cash"),
                    "--positions", self.file("positions"), "--orders", self.file("orders"),
                    "--accounts", self.file("accounts"))
        day = golive.GO_DIR / self.now.astimezone(golive.ET).date().isoformat() / self.account
        return max(sorted(day.glob("snapshot-*.json")))

    def resume(self, source: str = "user in chat", account=None) -> int:
        """Put the account in run through the real command: no typed reply is part of the flow any more."""
        return self.invoke("resume", "--account", account or self.account, "--source", source)

    def pause(self, source: str = "fixture owner pause", account=None, orders=None,
              out=None, working=()) -> int:
        argv = ["pause", "--account", account or self.account, "--source", source]
        if orders is not None:
            argv += ["--orders", str(orders)]
        if out is not None:
            argv += ["--out", str(out)]
        for code in working:
            argv += ["--working", str(code)]
        return self.invoke(*argv)

    def control(self, account: str | None = None) -> dict:
        journal = Journal(self.root / "journal")
        try:
            return journal.control(account or self.account)
        finally:
            journal.close()

    def generate_evidence(self, snapshot: Path | None = None, orders="orders",
                          trigger="user pasted 07 Part 2", out="evidence-live", as_of=None,
                          working=()) -> tuple[int, Path, dict]:
        """Run `golive.py evidence` for real and hand back the file the plan is supposed to read."""
        target = self.root / f"{out}.json"
        argv = ["evidence", "--account", self.account, "--snapshot", str(snapshot or self.snapshot()),
                "--orders", str(self.paths[orders]), "--trigger", trigger, "--out", str(target)]
        for code in working:
            argv += ["--working", str(code)]
        status = self.invoke(*argv, as_of=as_of)
        return status, target, json.loads(target.read_text(encoding="utf-8"))

    def plan(self, smoke=False, out="plan", working=(), evidence=None, snapshot=None, final_exit=False,
             flag=None, risk_action=None) -> tuple[Path, dict]:
        target = self.root / f"{out}.json"
        argv = ["plan", "--account", self.account, "--snapshot", str(snapshot or self.snapshot()),
                "--targets", self.file("targets"), "--quotes", self.file("quotes"),
                "--borrow", self.file("borrow"), "--orders", self.file("orders"),
                "--evidence", str(evidence or self.file("evidence")), "--out", str(target)]
        if smoke:
            argv.append("--smoke")
        if final_exit:
            argv.append("--final-exit")
        if risk_action is not None:
            argv += ["--risk-action", str(risk_action)]
        if flag is not None:
            argv += ["--flag", str(flag)]
        for code in working:
            argv += ["--working", str(code)]
        self.invoke(*argv)
        return target, json.loads(target.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ risk fixtures
    def attribute(self, names) -> None:
        """Journal the strategy opens that put these names on the book.

        `live.pstar.classify` only calls a position a strategy position when a decision row carries this
        strategy id, so without these rows every fixture holding would read as discretionary and the risk
        rules would refuse to trade it - which is the right behaviour, but not the one under test.
        """
        for name in names:
            golive.journal_append(self.root / "journal", "ORDER_CHECK", self.account, self.mode, self.now,
                                  "fixture: the strategy opened this name", "ALLOW", symbol=name,
                                  intent="ENTRY", config=self.config,
                                  strategy_id=self.targets["strategy_id"])

    def seed_peak(self, equity: float, days_ago: int = 1) -> Path:
        """Give the account an earlier, higher equity day: the paper API reports no peak, this file is it."""
        path = golive.STATE_DIR / f"{self.account}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        when = self.now - timedelta(days=days_ago)
        path.write_text(json.dumps({"account_id": self.account, "initial_equity": EQUITY,
                                    "peak_equity": equity,
                                    "sessions": [{"day": when.astimezone(golive.ET).date().isoformat(),
                                                  "as_of": when.isoformat(), "equity": equity}]}),
                        encoding="utf-8")
        return path

    def risk_report(self, level="ALERT", name="risk.json", findings=(), anomalies=(), account=None,
                    mode=None, generated_at=None, peak_basis=None, measured=None, positions=None,
                    peak=None, history=None) -> Path:
        """A report in the shape the `pstar-risk-check` skill writes, for `plan --risk-action` to re-measure."""
        return self.write(name, {
            "schema_version": golive.RISK_SCHEMA, "level": level,
            "account_id": str(account or self.account), "mode": mode or self.mode,
            "generated_at": generated_at or self.iso,
            "source": "fixture: the same raw reads the skill is given",
            "peak_basis": peak_basis or "fixture peak",
            **({"peak_equity": peak} if peak is not None else {}),
            **({"peak_history": list(history)} if history is not None else {}),
            "measured": measured or {}, "findings": list(findings), "anomalies": list(anomalies),
            "positions": [{"symbol": symbol, "quantity": quantity, "price": self.price_of(symbol)}
                          for symbol, quantity in (positions if positions is not None else self.positions)],
            "slack_text": f"risk {level} on {self.account}"})

    def broker_id(self, client_id: str) -> str:
        """The order_id the fixture history carries for a client order id: what the submit response says."""
        return str(int(golive.digest(client_id)[:6], 16) % 9_000_000 + 1_000_000)

    def record(self, plan: Path, ticket: dict, order_id=None, as_of=None, orders=None) -> int:
        order_id = order_id or self.broker_id(ticket["client_order_id"])
        response = self.write_response(f"resp-{ticket['n']}-{order_id}.json", order_id)
        argv = ["record", "--plan", str(plan), "--ticket", str(ticket["n"]), "--response", str(response)]
        if orders:
            argv += ["--orders", str(orders)]
        return self.invoke(*argv, as_of=as_of or self.iso)

    def write_response(self, name: str, order_id: str) -> Path:
        return self.write(name, envelope({"order_id": order_id, "status": STATUS_WORKING}))

    def history(self, name: str, rows: list) -> Path:
        return self.write(name, envelope({"orders": rows,
                                          "pagination": {"has_more": False, "next_key": ""}}))

    def set_orders(self, rows: list) -> Path:
        """Replace the raw history before the snapshot reads it.

        The snapshot records this file's hash and `evidence` re-hashes it, so editing it afterwards would
        fail `snapshot_verified_by_relay` for the wrong reason.
        """
        self.orders = list(rows)
        return self.write("orders.json", envelope({"orders": self.orders,
                                                   "pagination": {"has_more": False, "next_key": ""}}))

    def reprice(self, plan: Path, rows: list, name="reprice", working=(STATUS_WORKING,)) -> tuple[int, dict]:
        history = self.history(f"{name}_orders.json", rows)
        argv = ["reprice", "--plan", str(plan), "--orders", str(history),
                "--quotes", self.file("quotes"), "--snapshot", str(self.snapshot()),
                "--evidence", self.file("evidence")]
        for code in working:
            argv += ["--working", str(code)]
        code = self.invoke(*argv)
        return code, json.loads(plan.read_text(encoding="utf-8"))

    def reconcile(self, plan: Path, rows: list, working=()) -> tuple[int, dict]:
        history = self.history("reconcile_orders.json", rows)
        argv = ["reconcile", "--plan", str(plan), "--snapshot", str(self.snapshot()),
                "--orders", str(history)]
        for code in working:
            argv += ["--working", str(code)]
        status = self.invoke(*argv)
        return status, json.loads((plan.parent / "reconcile.json").read_text(encoding="utf-8"))

    def rows(self, stream: str) -> list[dict]:
        path = self.root / "journal" / f"{stream}.jsonl"
        return [] if not path.exists() else [json.loads(line) for line in
                                             path.read_text(encoding="utf-8").splitlines()]


class GoliveTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="golive-test-"))
        self.real = (golive.GO_DIR, golive.ROOT, golive.STATE_DIR)
        golive.GO_DIR = self.root / "reports/live/golive"
        golive.STATE_DIR = golive.GO_DIR / "state"

    def tearDown(self):
        golive.GO_DIR, golive.ROOT, golive.STATE_DIR = self.real
        shutil.rmtree(self.root, ignore_errors=True)

    def fixture(self, resume=True, **kwargs) -> Fixture:
        """`resume=False` leaves the account exactly as a fresh store has it: halted, and R16 says so."""
        fixture = Fixture(self.root, **kwargs)
        if resume:
            fixture.resume()
        return fixture

    def sizes(self, plan: dict) -> dict[str, int]:
        """The signed share count of every name the plan actually puts in the submit list."""
        return {ticket["symbol"]: ticket["quantity"] * (1 if ticket["side"] == "BUY" else -1)
                for ticket in plan["tickets"] if ticket["n"] in plan["submit"]}


class TestSnapshot(GoliveTest):
    def test_a_flat_account_normalises_and_binds_the_account(self):
        fixture = self.fixture()
        snapshot = json.loads(fixture.snapshot().read_text(encoding="utf-8"))
        self.assertEqual(snapshot["account_id"], ACCOUNTS["test"])
        self.assertEqual(snapshot["mode"], "test")
        self.assertEqual(snapshot["market_id"], 100)
        self.assertEqual(snapshot["account_type"], "SIMULATE")
        self.assertEqual(snapshot["initial_equity"], fixture.config["initial_equity"])
        self.assertEqual(snapshot["equity"], EQUITY)
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(snapshot["long_gross"], 0.0)
        self.assertEqual(snapshot["short_gross"], 0.0)
        self.assertEqual(snapshot["margin"], 0.0)
        self.assertEqual(snapshot["as_of"], CLOCK)
        self.assertIn("mtime", snapshot["as_of_source"])
        self.assertEqual(snapshot["account_binding"]["binding"], "api-asserted")
        self.assertEqual(snapshot["account_binding"]["asserted_by_api"], [ACCOUNTS["test"]])
        self.assertEqual(sorted(snapshot["account_binding"]["accounts_in_list"]),
                         sorted([ACCOUNTS["test"], ACCOUNTS["competition"]]))
        self.assertEqual(snapshot["previous_close_equity"], EQUITY)
        self.assertEqual(snapshot["peak_equity"], EQUITY)
        self.assertIn("realized_profit", snapshot["unverified_fields"])
        self.assertIn("unrealized_profit", snapshot["unverified_fields"])
        self.assertEqual(snapshot["relay_assertion"], golive.RELAY_NOTE)
        decision = fixture.rows("decisions")[-1]
        self.assertEqual((decision["kind"], decision["action"]), ("GOLIVE_SNAPSHOT", "SNAPSHOT"))
        self.assertEqual(decision["account_id"], ACCOUNTS["test"])
        self.assertEqual(decision["config_checksum"], digest(fixture.config))

    def test_short_holdings_keep_their_sign_and_the_gross_follows(self):
        book = [("SPY", 10), ("NVDA", -5)]
        fixture = self.fixture(positions=book)
        snapshot = json.loads(fixture.snapshot().read_text(encoding="utf-8"))
        self.assertEqual([(row["symbol"], row["quantity"]) for row in snapshot["positions"]], book)
        long_value = 10 * fixture.price_of("SPY")
        short_value = 5 * fixture.price_of("NVDA")
        self.assertEqual(snapshot["long_gross"], long_value)
        self.assertEqual(snapshot["short_gross"], short_value)
        self.assertEqual(snapshot["market_value"], long_value - short_value)
        self.assertEqual(snapshot["margin_dollars"], 0.5 * long_value + 0.6 * short_value)
        self.assertAlmostEqual(snapshot["margin"], snapshot["margin_dollars"] / EQUITY, places=9)

    def test_an_unknown_field_stays_null_instead_of_becoming_zero(self):
        fixture = self.fixture()
        # sim_trade_cash_info returns nothing but total_asset: every other cash field is unknown, null,
        # and named in unverified_fields. The gross exposure is the one thing still derivable, from the
        # signed position rows, and the snapshot says which source it used.
        fixture.paths["cash"] = fixture.write("sparse_cash.json", envelope({"total_asset": f"{EQUITY:.2f}"}))
        snapshot = json.loads(fixture.snapshot().read_text(encoding="utf-8"))
        self.assertEqual(snapshot["equity"], EQUITY)
        for field in ("cash", "frozen_cash", "buy_power_long", "realized_profit", "unrealized_profit"):
            self.assertIsNone(snapshot[field], f"{field} became a number the API never reported")
            self.assertIn(field, snapshot["unverified_fields"])
        self.assertEqual(snapshot["long_gross"], 0.0)
        self.assertEqual(snapshot["short_gross"], 0.0)
        self.assertNotIn("long_gross", snapshot["unverified_fields"])
        self.assertEqual(snapshot["gross_source"], "derived from the signed position market values")

    def test_a_response_from_another_account_is_refused(self):
        fixture = self.fixture()
        payload = fixture.cash()
        payload["data"]["acc_id"] = ACCOUNTS["competition"]
        path = fixture.write("wrong_cash.json", payload)
        with self.assertRaises(golive.GoliveError) as refused:
            fixture.invoke("snapshot", "--account", ACCOUNTS["test"], "--cash", str(path),
                           "--positions", fixture.file("positions"))
        self.assertIn("account", str(refused.exception).lower())

    def test_a_fractional_or_ambiguous_position_is_refused(self):
        fixture = self.fixture()
        for quantity, extra in (("1.5", {}), ("-3", {"direction": "LONG"})):
            row = {"symbol": "US.NVDA", "qty": quantity, "mv": "100.0", "cost_price": "50.0"}
            row.update(extra)
            path = fixture.write(f"positions_{quantity}.json", envelope({"positions": [row]}))
            with self.assertRaises(golive.GoliveError, msg=f"qty {quantity} {extra} was accepted"):
                fixture.invoke("snapshot", "--account", ACCOUNTS["test"], "--cash", fixture.file("cash"),
                               "--positions", str(path))
        # A zero-share broker row is neither a holding nor an error: kept out of the book, but shown.
        path = fixture.write("positions_zero.json",
                             envelope({"positions": [{"symbol": "US.NVDA", "qty": "0", "mv": "0.0",
                                                     "cost_price": "50.0"}]}))
        fixture.paths["positions"] = path
        snapshot = json.loads(fixture.snapshot().read_text(encoding="utf-8"))
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual([row["symbol"] for row in snapshot["zero_quantity_rows"]], ["NVDA"])
        self.assertEqual(snapshot["long_gross"], 0.0)

    def test_a_failed_api_envelope_is_refused(self):
        fixture = self.fixture()
        path = fixture.write("failed_cash.json",
                             {"ret_code": 100002, "ret_msg": "invalid param", "data": None})
        with self.assertRaises(golive.GoliveError):
            fixture.invoke("snapshot", "--account", ACCOUNTS["test"], "--cash", str(path),
                           "--positions", fixture.file("positions"))

    def test_the_cli_reports_a_refusal_as_exit_code_two(self):
        fixture = self.fixture()
        path = fixture.write("unusable_cash.json", {"ret_code": 100002, "ret_msg": "invalid param",
                                                    "data": None})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = fixture.call("snapshot", "--account", ACCOUNTS["test"], "--cash", str(path),
                                "--positions", fixture.file("positions"))
        self.assertEqual(code, 2)
        self.assertIn("GOLIVE REFUSED", buffer.getvalue())
        self.assertIn("invalid param", buffer.getvalue())


class TestPlan(GoliveTest):
    def test_the_entry_plan_sizes_every_name_with_the_prompt_06_planner(self):
        fixture = self.fixture()
        _, plan = fixture.plan()
        self.assertEqual(plan["mode"], "test")
        self.assertEqual(plan["account_id"], ACCOUNTS["test"])
        self.assertEqual(plan["market_id"], 100)
        self.assertFalse(plan["smoke"])
        self.assertEqual(plan["targets"]["signal_date"], SESSION)
        self.assertTrue(plan["summary"]["entry_batch"])
        self.assertEqual(self.sizes(plan), fixture.expected_shares)
        self.assertEqual(plan["summary"]["submit"], len(fixture.expected_shares))
        self.assertEqual(plan["summary"]["order_budget"], fixture.config["competition_order_cap"]
                         - fixture.config["emergency_flatten_reserve"])
        self.assertEqual(plan["summary"]["budget_left_today"], plan["summary"]["order_budget"])
        self.assertEqual(plan["summary"]["positions_after"], len(fixture.expected_shares))
        self.assertEqual(plan["summary"]["executed_today"], 0)
        self.assertEqual(plan["summary"]["buy"] + plan["summary"]["sell"], len(plan["tickets"]))
        self.assertEqual(len(fixture.weights), 82)  # the sealed P* book: five ETF longs, 77 shorts
        self.assertEqual(plan["targets"]["checksum"], fixture.targets["checksum"])
        self.assertTrue(plan["targets"]["rebalance_due"])
        self.assertEqual(plan["summary"]["estimated_fees_usd"],
                         round(plan["summary"]["submit"] * fixture.config["order_platform_fee"], 2))
        self.assertEqual([t["symbol"] for t in plan["tickets"] if t["side"] == "BUY"],
                         sorted(name for name, weight in fixture.weights.items() if weight > 0))
        self.assertEqual(plan["submit"], [ticket["n"] for ticket in
                                          sorted(plan["tickets"], key=lambda t: (t["weight"] < 0,
                                                                                 t["symbol"]))])

    def test_the_plan_agrees_with_the_planner_on_the_same_inputs(self):
        fixture = self.fixture()
        snapshot = json.loads(fixture.snapshot().read_text(encoding="utf-8"))
        quotes = {name: {"price": price, "verified_by_relay": True}
                  for name, price in fixture.prices.items()}
        baseline = planner_plan(fixture.config, fixture.targets, snapshot, quotes)
        _, plan = fixture.plan()
        self.assertEqual({order["symbol"]: order["quantity"] * (1 if order["side"] == "BUY" else -1)
                          for order in baseline["orders"]}, self.sizes(plan))
        self.assertEqual(sorted(row["symbol"] for row in baseline["skipped"]),
                         sorted(row["symbol"] for row in plan["summary"]["skipped"]))
        _, skipped = target_shares(fixture.targets["weights"], EQUITY, quotes)
        self.assertEqual(len(skipped), len(plan["summary"]["skipped"]))

    def test_a_name_that_rounds_to_zero_shares_is_skipped_and_never_redistributed(self):
        reference = Fixture(self.root)
        thin = min(reference.weights, key=lambda name: (abs(reference.weights[name]), name))
        fixture = self.fixture(prices={thin: 3 * reference.prices[thin]})
        self.assertNotIn(thin, fixture.expected_shares)
        _, plan = fixture.plan()
        self.assertNotIn(thin, self.sizes(plan))
        rows = [row for row in plan["summary"]["skipped"] if row["symbol"] == thin]
        self.assertEqual(len(rows), 1)
        self.assertIn("rounds to zero shares", rows[0]["reason"])
        self.assertEqual(rows[0]["weight"], fixture.weights[thin])
        self.assertEqual(self.sizes(plan), fixture.expected_shares)
        self.assertEqual(plan["summary"]["positions_after"], len(fixture.expected_shares))

    def test_every_ticket_carries_the_exact_paper_payload_and_passes_the_rules(self):
        fixture = self.fixture()
        _, plan = fixture.plan()
        self.assertTrue(plan["tickets"])
        for ticket in plan["tickets"]:
            self.assertEqual(ticket["payload"], paper_payload(ticket["order"], 100))
            self.assertEqual(ticket["payload"]["acc_id"], ACCOUNTS["test"])
            self.assertEqual(ticket["payload"]["order_type"], 1)
            self.assertEqual(ticket["payload"]["order_trade_time_type"], 1)
            self.assertEqual(ticket["payload"]["market"], 100)
            self.assertEqual(ticket["payload"]["symbol"], f"US.{ticket['symbol']}")
            self.assertAlmostEqual(float(ticket["payload"]["price"]), ticket["limit_price"], places=2)
            self.assertEqual(ticket["payload"]["qty"], str(ticket["quantity"]))
            self.assertEqual(ticket["payload"]["text"], ticket["client_order_id"])
            self.assertLessEqual(len(ticket["payload"]["text"].encode()), 100)
            self.assertEqual(ticket["tool"], "sim_trade_input_order")
            self.assertEqual(ticket["gate"]["action"], "ALLOW")
            self.assertEqual(ticket["gate"]["failed_rules"], [])
            self.assertEqual(ticket["gate"]["execution_allowed"],
                             fixture.config["execution_enabled"] is True,
                             "execution_allowed only mirrors the config switch; this command never sends")
            self.assertEqual(len(ticket["gate"]["order_fingerprint"]), 64)
            self.assertTrue(ticket["submit"])
            self.assertEqual(ticket["status"], "PLANNED")
            self.assertEqual(ticket["order"]["order_type"], "LIMIT")
            self.assertEqual(ticket["order"]["time_in_force"], "DAY")
            self.assertEqual(ticket["order"]["session"], "RTH")

    def test_the_limit_prices_through_the_touch_on_the_right_side(self):
        fixture = self.fixture()
        _, plan = fixture.plan()
        for ticket in plan["tickets"]:
            touch = plan["quotes"][ticket["symbol"]]
            if ticket["side"] == "BUY":
                self.assertGreater(ticket["limit_price"], touch["ask"])
                self.assertGreaterEqual(ticket["limit_price"],
                                        math.ceil(touch["ask"] * (1 + golive.PRICE_OFFSET) * 100) / 100)
                self.assertGreaterEqual(ticket["limit_price"], round(touch["ask"] + golive.CENT, 2))
            else:
                self.assertLess(ticket["limit_price"], touch["bid"])
                self.assertLessEqual(ticket["limit_price"],
                                     math.floor(touch["bid"] * (1 - golive.PRICE_OFFSET) * 100) / 100)
                self.assertLessEqual(ticket["limit_price"], round(touch["bid"] - golive.CENT, 2))
        self.assertIn("golive marketable limit", plan["price_policy"]["used"])
        self.assertIn("mid", plan["price_policy"]["planner_policy"])
        self.assertIn("0.05%", plan["price_policy"]["rule"])
        self.assertIn("R7", plan["price_policy"]["known_conflict"])

    def test_a_rejected_ticket_stays_out_of_the_submit_list_and_is_logged_with_its_reason(self):
        reference = Fixture(self.root)
        penny = min((name for name, weight in reference.weights.items() if weight < 0),
                    key=lambda name: (abs(reference.weights[name]), name))
        fixture = self.fixture(prices={penny: 3.0})
        _, plan = fixture.plan()
        ticket = next(item for item in plan["tickets"] if item["symbol"] == penny)
        self.assertFalse(ticket["submit"])
        self.assertEqual(ticket["gate"]["action"], "HOLD")
        self.assertIn("R7", ticket["gate"]["failed_rules"])
        self.assertNotIn(ticket["n"], plan["submit"])
        self.assertEqual(plan["summary"]["reject_tally"]["R7"], 1)
        self.assertEqual(plan["summary"]["price_band_rejections"], 1)
        self.assertIn("R7", plan["summary"]["rules_failed"])
        self.assertEqual(plan["summary"]["submit"], len(plan["tickets"]) - 1)
        decision = next(row for row in reversed(fixture.rows("decisions"))
                        if row["kind"] == "ORDER_CHECK" and row["symbol"] == penny)
        self.assertEqual(decision["action"], "HOLD")
        self.assertIn("R7", decision["reason"])
        self.assertTrue([check for check in decision["rule_checks"]
                         if check["id"] == "R7" and not check["passed"]])

    def test_r7_rejects_a_penny_ticket_the_one_cent_rule_cannot_price(self):
        # The price policy must sit at least a cent through the touch; R7 allows 25 bps.
        # Below roughly $4 those two are mutually exclusive, and the frozen gate wins:
        # the ticket is rejected, stays out of the submit list, and is journaled as HOLD.
        reference = Fixture(self.root)
        thin = min((name for name, weight in reference.weights.items() if weight < 0),
                   key=lambda name: (abs(reference.weights[name]), name))
        fixture = self.fixture(prices={thin: 2.0})
        _, plan = fixture.plan()
        pennies = [t for t in plan["tickets"]
                   if t["gate"]["eligible"] is False and "R7" in t["gate"]["failed_rules"]]
        self.assertEqual([t["symbol"] for t in pennies], [thin],
                         "a $2.00 ticket is the cent-rounding case; nothing else should trip R7")
        for ticket in pennies:
            touch = ticket["arrival_price"]
            self.assertGreater(abs(ticket["limit_price"] - touch) / touch, 0.0025)
            self.assertNotIn(ticket["n"], plan["submit"])
        self.assertEqual(len(pennies), plan["summary"]["price_band_rejections"])
        self.assertEqual(len(pennies), plan["summary"]["reject_tally"]["R7"])
        self.assertEqual(len(pennies), sum(1 for row in fixture.rows("decisions")
                                          if row.get("action") == "HOLD" and "R7" in (row.get("reason") or "")))
        self.assertTrue(all(t["gate"]["eligible"] for t in plan["tickets"] if t["sleeve"] == "long"),
                        "the rejection is about cent rounding, not about the longs")
        self.assertTrue(plan["submit"], "the affordable book is still planned")

    def test_a_thin_fund_is_ineligible_and_is_never_replaced(self):
        fixture = self.fixture(fund_size=1_000_000.0)
        _, plan = fixture.plan()
        self.assertEqual([t for t in plan["tickets"] if fixture.weights[t["symbol"]] > 0], [])
        self.assertTrue(set(plan["summary"]["blocked"]) &
                        {name for name, weight in fixture.weights.items() if weight > 0})

    def test_a_short_without_borrow_evidence_is_blocked_and_never_replaced(self):
        fixture = self.fixture(borrow_proxy=False)
        _, plan = fixture.plan()
        self.assertEqual([t["symbol"] for t in plan["tickets"] if t["side"] == "SELL"], [])
        shorts = {name for name, weight in fixture.weights.items() if weight < 0}
        self.assertTrue(shorts & set(plan["summary"]["blocked"]))
        self.assertEqual(self.sizes(plan), {name: shares for name, shares in fixture.expected_shares.items()
                                           if fixture.weights[name] > 0})
        self.assertIn("borrow", json.dumps(plan["summary"]["blocked_detail"]).lower())

    def test_an_unborrowable_name_is_blocked_while_the_rest_of_the_book_still_trades(self):
        reference = Fixture(self.root)
        blocked = sorted(name for name, weight in reference.weights.items() if weight < 0)[:3]
        fixture = self.fixture(blocked=blocked)
        _, plan = fixture.plan()
        self.assertFalse(set(blocked) & set(self.sizes(plan)))
        self.assertTrue(set(blocked) <= set(plan["summary"]["blocked"]))
        self.assertEqual(self.sizes(plan), fixture.expected_shares)
        self.assertIn(json.dumps(blocked[0]), json.dumps(plan["summary"]["blocked_detail"]))

    def test_the_plan_refuses_an_account_that_is_not_the_config_mode(self):
        fixture = self.fixture()
        with self.assertRaises(golive.GoliveError) as refused:
            fixture.invoke("plan", "--account", ACCOUNTS["competition"], "--snapshot",
                           str(fixture.snapshot()), "--targets", fixture.file("targets"),
                           "--quotes", fixture.file("quotes"), "--borrow", fixture.file("borrow"),
                           "--evidence", fixture.file("evidence"), "--out", str(self.root / "nope.json"))
        self.assertIn("competition", str(refused.exception))
        self.assertFalse((self.root / "nope.json").exists())

    def test_the_smoke_plan_is_four_one_share_tickets_on_the_test_account(self):
        fixture = self.fixture()
        _, plan = fixture.plan(smoke=True)
        self.assertTrue(plan["smoke"])
        self.assertEqual(plan["account_id"], ACCOUNTS["test"])
        self.assertEqual(len(plan["tickets"]), 4)
        opens = [t for t in plan["tickets"] if t["intent"] == "ENTRY"]
        closes = [t for t in plan["tickets"] if t["intent"] == "EXIT"]
        self.assertEqual(len(opens), 2)
        self.assertEqual(len(closes), 2)
        self.assertEqual({t["quantity"] for t in plan["tickets"]}, {1})
        self.assertEqual(opens[0]["symbol"], golive.SMOKE_ETF)
        self.assertEqual(opens[0]["side"], "BUY")
        self.assertEqual(opens[1]["side"], "SELL")
        self.assertLess(opens[1]["arrival_price"], golive.SMOKE_SHORT_MAX_PRICE)
        self.assertEqual({c["symbol"] for c in closes}, {opens[0]["symbol"], opens[1]["symbol"]})
        self.assertEqual({c["side"] for c in closes}, {"BUY", "SELL"})
        self.assertEqual([t["payload"]["acc_id"] for t in plan["tickets"]], [ACCOUNTS["test"]] * 4)
        self.assertEqual([t["n"] for t in plan["tickets"]], [1, 2, 3, 4])
        self.assertTrue(all(t["gate"]["action"] == "ALLOW" for t in plan["tickets"]))
        self.assertEqual(plan["submit"], [1, 2, 3, 4])

    def test_the_smoke_plan_is_refused_on_the_competition_account(self):
        fixture = self.fixture(mode="competition", account=ACCOUNTS["competition"])
        with self.assertRaises(golive.GoliveError) as refused:
            fixture.invoke("plan", "--account", ACCOUNTS["competition"], "--snapshot",
                           str(fixture.snapshot()), "--targets", fixture.file("targets"),
                           "--quotes", fixture.file("quotes"), "--borrow", fixture.file("borrow"),
                           "--evidence", fixture.file("evidence"), "--smoke",
                           "--out", str(self.root / "smoke_b.json"))
        self.assertIn("Stage A", str(refused.exception))


FLATTEN_START = "2026-11-27T17:20:00+00:00"     # 12:20 ET, inside the exit window, before the deadline
FLATTEN_LATE = "2026-11-27T17:50:00+00:00"      # 12:50 ET, past final_flatten_at
FLATTEN_BOOK = (("SPY", 3), ("QQQ", -2), ("AAOI", -4), ("DIA", 5))


class TestFinalExit(GoliveTest):
    """`plan --final-exit`: the scheduled 2026-11-27 flatten of everything on the competition account.

    DIA is an ETF of the sleeve universe that the current book does not hold a weight in, so it is the
    name that proves the flatten closes the account rather than only the strategy book.
    """

    def flatten_book(self, at=FLATTEN_START, positions=FLATTEN_BOOK):
        fixture = self.fixture(mode="competition", account=ACCOUNTS["competition"],
                               positions=list(positions),
                               prices={"SPY": 620.0, "QQQ": 500.0, "AAOI": 30.0})
        fixture.to_utc(at)
        return fixture

    def test_the_flatten_closes_every_position_of_the_fresh_snapshot(self):
        fixture = self.flatten_book()
        _, plan = fixture.plan(final_exit=True)
        self.assertEqual(plan["intent"], "FINAL_EXIT")
        self.assertEqual(plan["mode"], "competition")
        self.assertEqual({(t["symbol"], t["side"], t["quantity"]) for t in plan["tickets"]},
                         {("SPY", "SELL", 3), ("QQQ", "BUY", 2), ("AAOI", "BUY", 4), ("DIA", "SELL", 5)})
        self.assertEqual({t["intent"] for t in plan["tickets"]}, {"FINAL_EXIT"})
        self.assertEqual({t["order"]["order_type"] for t in plan["tickets"]}, {"LIMIT"})
        self.assertEqual(plan["submit"], [t["n"] for t in plan["tickets"]])
        self.assertTrue(all(t["gate"]["action"] == "ALLOW" for t in plan["tickets"]), plan["summary"])
        self.assertEqual(plan["target_shares"], {name: 0 for name, _ in FLATTEN_BOOK})
        self.assertEqual(plan["shares_after"], {name: 0 for name, _ in FLATTEN_BOOK})
        self.assertEqual(plan["summary"]["margin_after"], 0.0)
        self.assertEqual([t["payload"]["symbol"] for t in plan["tickets"]],
                         ["US.DIA", "US.SPY", "US.AAOI", "US.QQQ"])   # every sell, then every cover
        self.assertTrue(all(t["payload"]["text"].startswith("pstar-") for t in plan["tickets"]))
        self.assertEqual(plan["final_exit"]["outside_target_book"], ["DIA long 5 share(s)"])
        self.assertIn("DIA", json.dumps(plan["tickets"]))

    def test_the_flatten_becomes_a_market_order_at_the_deadline(self):
        fixture = self.flatten_book(at=FLATTEN_LATE)
        _, plan = fixture.plan(final_exit=True)
        self.assertTrue(plan["final_exit"]["flatten_deadline_passed"])
        for ticket in plan["tickets"]:
            with self.subTest(symbol=ticket["symbol"]):
                self.assertEqual(ticket["order"]["order_type"], "MARKET")
                self.assertIsNone(ticket["limit_price"])
                self.assertEqual(ticket["payload"]["order_type"], 3)
                self.assertNotIn("price", ticket["payload"])
                self.assertEqual(ticket["gate"]["action"], "ALLOW")
                self.assertIn("MARKET", ticket["order"]["price_basis"])

    def test_the_flatten_survives_a_paused_switch_because_it_only_reduces(self):
        fixture = self.flatten_book()
        _, plan = fixture.plan(final_exit=True, flag=paused_flag(fixture))
        self.assertEqual(plan["switch"]["state"]["state"], "paused")
        self.assertTrue(plan["switch"]["decision"]["allow"])
        self.assertTrue(plan["submit"])

    def test_the_same_paused_flag_stops_an_entry_batch(self):
        fixture = self.fixture()
        code = fixture.invoke("plan", "--account", ACCOUNTS["test"], "--snapshot", str(fixture.snapshot()),
                              "--targets", fixture.file("targets"), "--quotes", fixture.file("quotes"),
                              "--borrow", fixture.file("borrow"), "--orders", fixture.file("orders"),
                              "--evidence", fixture.file("evidence"), "--flag", str(paused_flag(fixture)),
                              "--out", str(self.root / "blocked.json"))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "blocked.json").exists())
        holds = list((golive.GO_DIR / fixture.now.astimezone(golive.ET).date().isoformat()
                      / ACCOUNTS["test"]).glob("switch-hold-*.json"))
        self.assertEqual(len(holds), 1)
        record = json.loads(holds[0].read_text(encoding="utf-8"))
        self.assertEqual(record["switch"]["state"]["state"], "paused")
        self.assertEqual(record["cancel"], [])
        self.assertIn("live.golive pause", record["do"])
        self.assertIn("FINAL_EXIT", record["note"])

    def test_the_order_budget_is_printed_before_the_first_ticket(self):
        fixture = self.flatten_book()
        golive.journal_append(self.root / "journal", "GOLIVE_RECORD", ACCOUNTS["competition"],
                              "competition", fixture.now, "order_id 4455", "SUBMITTED", symbol="SPY",
                              config=fixture.config)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _, plan = fixture.plan(final_exit=True)
        printed = buffer.getvalue()
        self.assertIn("ORDER BUDGET before the first ticket", printed)
        self.assertLess(printed.index("ORDER BUDGET"), printed.index("tickets:"))
        budget = plan["order_budget"]
        cap = fixture.config["competition_order_cap"]
        self.assertEqual((budget["cap"], budget["journal_used"], budget["exit_tickets"]), (cap, 1, 4))
        self.assertEqual(budget["used"], 1)
        self.assertEqual(budget["left_after_exit"], cap - 1 - 4)
        self.assertEqual(budget["rounds_affordable"], (cap - 5) // 4)
        self.assertEqual(budget["rounds_needed"], 6)
        self.assertFalse(budget["over_cap"])
        self.assertIn("journal GOLIVE_RECORD/SUBMITTED rows", budget["basis"])

    def test_a_flatten_that_does_not_fit_the_cap_says_so(self):
        fixture = self.flatten_book()
        for index in range(197):
            golive.journal_append(self.root / "journal", "GOLIVE_RECORD", ACCOUNTS["competition"],
                                  "competition", fixture.now, f"order_id {index}", "SUBMITTED",
                                  symbol="SPY", config=fixture.config)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _, plan = fixture.plan(final_exit=True)
        self.assertIn("OVER THE CAP", buffer.getvalue())
        self.assertIn("OVER THE CAP by 1 order(s)", buffer.getvalue())
        self.assertTrue(plan["order_budget"]["over_cap"])
        self.assertEqual(plan["order_budget"]["left_after_exit"], -1)
        self.assertEqual(plan["order_budget"]["rounds_affordable"], 0)

    def test_the_flatten_reprices_the_whole_exit_window_and_then_goes_market(self):
        self.assertEqual(golive.reprice_round_cap(load_strategy_config(), "FINAL_EXIT"), 6)
        self.assertEqual(golive.reprice_round_cap(load_strategy_config()), golive.MAX_REPRICE_ROUNDS)
        fixture = self.flatten_book(at=FLATTEN_START)
        path, plan = fixture.plan(final_exit=True)
        ticket = plan["tickets"][0]
        fixture.record(path, ticket, as_of=fixture.iso)
        live = ticket
        for rounds in range(1, 6):
            at = fixture.advance(golive.REPRICE_AFTER_SECONDS + 60)
            row = fixture.order_row(live["symbol"], live["side"], live["quantity"], 0,
                                    live["client_order_id"], status=STATUS_WORKING, at=at)
            code, plan = fixture.reprice(path, [row], name=f"flat{rounds}")
            self.assertEqual(code, 0)
            child = plan["tickets"][-1]
            self.assertEqual(child["reprice_round"], rounds)
            self.assertEqual(child["intent"], "FINAL_EXIT")
            self.assertEqual(child["gate"]["action"], "ALLOW")
            self.assertEqual(next(t for t in plan["tickets"] if t["n"] == live["n"])["status"], "REPRICING")
            self.assertEqual(child["order"]["order_type"],
                             "MARKET" if at >= "2026-11-27T17:45:00+00:00" else "LIMIT")
            if child["order"]["order_type"] == "MARKET":
                self.assertEqual(child["payload"]["order_type"], 3)
                self.assertNotIn("price", child["payload"])
            else:
                self.assertEqual(child["payload"]["order_type"], 1)
            self.assertIn(live["symbol"], child["client_order_id"])
            live = child
            fixture.record(path, live, as_of=at)
        self.assertGreater(rounds, golive.MAX_REPRICE_ROUNDS)   # an entry batch would have stopped at 3

    def test_reconcile_after_the_flatten_lists_every_share_still_open(self):
        fixture = self.flatten_book()
        path, plan = fixture.plan(final_exit=True)
        fixture.positions = [("DIA", 5)]          # the three strategy names closed, the extra one did not
        fixture.write("positions.json", envelope({"positions": fixture.position_rows()}))
        status, result = fixture.reconcile(path, [])
        self.assertEqual(result["intent"], "FINAL_EXIT")
        self.assertEqual([row["symbol"] for row in result["deviations"]], ["DIA"])
        self.assertEqual(result["deviations"][0]["dollars"], 250.0)   # exactly the entry-plan band
        self.assertIn("still open after the scheduled final exit", result["deviations"][0]["note"])
        self.assertFalse(result["complete_reconciliation"])
        self.assertEqual(status, 2)
        summary = (path.parent / "summary.md").read_text(encoding="utf-8")
        self.assertIn("positions still open after the scheduled final exit", summary)
        self.assertIn("1 position(s) still open", summary)
        fixture.positions = []
        fixture.write("positions.json", envelope({"positions": []}))
        status, flat = fixture.reconcile(path, [])
        self.assertEqual(flat["positions"], 0)
        self.assertEqual(flat["deviations"], [])
        self.assertTrue(flat["complete_reconciliation"])
        self.assertEqual(status, 0)
        self.assertIn("FLAT - no position remains on the account",
                      (path.parent / "summary.md").read_text(encoding="utf-8"))


class TestRiskAction(GoliveTest):
    """`plan --risk-action risk.json`: the reduce-only batch the cloud monitor runs between rebalances.

    The books are priced at $100 a share so every threshold is readable straight off the numbers: the trim
    line is 39% of $100,000 = $39,000 = 390 shares, the cap it trims back to is $38,000 = 380 shares, the
    margin factors are 0.5 long and 0.6 short, and the drawdown limit is -8% of the peak this tool keeps.
    """

    FLAT = {"SPY": 100.0, "QQQ": 100.0, "IWM": 100.0, "USO": 100.0, "UUP": 100.0, "AAOI": 100.0,
            "ABSI": 100.0}

    def risk_book(self, positions, peak=None, attribute=True, at=None):
        fixture = self.fixture(mode="competition", account=ACCOUNTS["competition"], positions=list(positions),
                               prices=self.FLAT)
        if at:
            fixture.to_utc(at)
        if peak is not None:
            fixture.seed_peak(peak)
        if attribute:
            fixture.attribute([name for name, _ in positions])
        return fixture

    def test_an_alert_trims_the_name_above_the_line_back_to_the_cap(self):
        fixture = self.risk_book([("SPY", 400)])          # $40,000 = 40% of initial assets
        report = fixture.risk_report("ALERT", findings=[{"rule": "ticker", "value": 0.40, "threshold": 0.39,
                                                        "detail": "SPY 40,000 USD"}])
        _, plan = fixture.plan(risk_action=report)
        self.assertEqual(plan["intent"], "EXIT")
        self.assertEqual([(t["symbol"], t["side"], t["quantity"]) for t in plan["tickets"]],
                         [("SPY", "SELL", 20)])          # 400 - 380, and 380 x 100 = the $38,000 cap
        self.assertEqual(plan["target_shares"], {"SPY": 380})
        self.assertTrue(all(t["gate"]["action"] == "ALLOW" for t in plan["tickets"]), plan["summary"])
        self.assertEqual(plan["risk_action"]["level"], "ALERT")
        self.assertEqual(plan["risk_action"]["delever_scale"], 1.0)
        self.assertEqual([t["payload"]["order_type"] for t in plan["tickets"]], [1])
        self.assertIn("RISK ALERT from", self.captured(fixture, plan))

    def test_an_alert_scales_the_whole_book_when_margin_passes_95(self):
        positions = [("SPY", 380), ("QQQ", 380), ("IWM", 380), ("USO", 380), ("UUP", 380), ("AAOI", -200)]
        fixture = self.risk_book(positions)
        # margin = (0.5 x 190,000 + 0.6 x 20,000) / 100,000 = 107%, and no single name reaches 39%.
        report = fixture.risk_report("ALERT", measured={"margin": 1.07})
        path, plan = fixture.plan(risk_action=report)
        scale = 0.9 / 1.07
        want = {name: int(round(quantity * scale)) for name, quantity in positions}
        self.assertEqual(plan["target_shares"], want)
        self.assertAlmostEqual(plan["risk_action"]["delever_scale"], scale, places=9)
        self.assertEqual({(t["symbol"], t["side"]) for t in plan["tickets"]},
                         {(name, "SELL") for name, held in positions if held > 0} | {("AAOI", "BUY")})
        # Whole-share rounding leaves the book a hair above the 90% target; what matters is that it is
        # clearly under the 95% line that triggered the cut, which is what `actions` reports as residual.
        self.assertLess(plan["risk_action"]["margin_after"], fixture.config["delever_margin_above"])
        self.assertAlmostEqual(plan["risk_action"]["margin_after"], fixture.config["delever_to"], delta=0.002)
        # golive sizes exactly what the frozen monitor prescribes for the same book.
        state = golive.measure(fixture.config, [{"symbol": n, "quantity": q, "price": 100.0}
                                                for n, q in positions], EQUITY, EQUITY, EQUITY)
        self.assertEqual(golive.actions(fixture.config, state, [])["share_changes"],
                         {name: want[name] - held for name, held in state["quantities"].items()})
        for ticket in plan["tickets"]:
            with self.subTest(symbol=ticket["symbol"]):
                self.assertEqual(ticket["intent"], "EXIT")
                self.assertLess(abs(plan["target_shares"][ticket["symbol"]]),
                                abs(state["quantities"][ticket["symbol"]]))   # reduce-only, never an entry

    def test_a_kill_flattens_every_strategy_position_and_latches_the_halt(self):
        fixture = self.risk_book([("SPY", 300), ("AAOI", -200)], peak=110_000.0)
        seen = json.loads(fixture.snapshot().read_text(encoding="utf-8"))
        self.assertEqual((seen["peak_equity"], seen["equity"]), (110_000.0, EQUITY))
        self.assertEqual(seen["initial_equity"], EQUITY)
        report = fixture.risk_report("KILL", findings=[{"rule": "drawdown", "value": -0.0909,
                                                        "threshold": -0.08, "detail": "equity 100,000 of a "
                                                                                       "110,000 peak"}])
        _, plan = fixture.plan(risk_action=report)
        self.assertEqual(plan["intent"], "EMERGENCY_EXIT")
        self.assertEqual({(t["symbol"], t["side"], t["quantity"]) for t in plan["tickets"]},
                         {("SPY", "SELL", 300), ("AAOI", "BUY", 200)})
        self.assertTrue(all(t["intent"] == "EMERGENCY_EXIT" for t in plan["tickets"]))
        self.assertTrue(all(t["gate"]["action"] == "ALLOW" for t in plan["tickets"]),
                        [t["gate"] for t in plan["tickets"]])
        self.assertEqual(plan["target_shares"], {"SPY": 0, "AAOI": 0})
        self.assertEqual(plan["risk_action"]["level"], "KILL")
        self.assertTrue(plan["risk_action"]["halt"])
        self.assertAlmostEqual(plan["risk_action"]["drawdown"], EQUITY / 110_000.0 - 1, places=9)
        # The halt is the point of a KILL: latched before the tickets, and the shared flag carries it.
        self.assertTrue(fixture.control()["halted"])
        adds = [code for row in plan["risk_action"]["switch_mirror"]["writes"]
                for code in row["payload"]["code_list"]]
        self.assertIn("US.SQQQ", adds)
        self.assertIn("RISK_KILL", [row["action"] for row in fixture.rows("decisions")])
        kill_row = [row for row in fixture.rows("decisions") if row["action"] == "RISK_KILL"]
        self.assertIn(plan["risk_action"]["source"], kill_row[0]["reason"])
        # What an ordinary plan does next on the killed account is the proof the latch holds: the drawdown is
        # still measured from the snapshot, so the batch is the same two closes and nothing from the target
        # book opens. A batch that merely hits the pause switch, with no drawdown behind it, is refused
        # outright by R16 in TestOperatorControlAndEvidence.test_pause_latches_the_switch_again_and_the_plan_stops.
        after = self.root / "after-kill.json"
        code = fixture.invoke("plan", "--account", ACCOUNTS["competition"], "--snapshot",
                              str(fixture.snapshot()), "--targets", fixture.file("targets"),
                              "--quotes", fixture.file("quotes"), "--borrow", fixture.file("borrow"),
                              "--orders", fixture.file("orders"), "--evidence", fixture.file("evidence"),
                              "--out", str(after))
        entry = json.loads(after.read_text(encoding="utf-8"))
        self.assertEqual(code, 0, entry["summary"])
        self.assertIs(entry["kill_switch"]["halted"], True)
        self.assertIn("R9 peak drawdown", entry["kill_switch"]["reason"])
        self.assertEqual({(t["symbol"], t["side"], t["quantity"]) for t in entry["tickets"]},
                         {("SPY", "SELL", 300), ("AAOI", "BUY", 200)})
        self.assertEqual({t["intent"] for t in entry["tickets"]}, {"EMERGENCY_EXIT"})
        # and in the same cash-first send order the KILL batch used: the sell before the cover.
        self.assertEqual([t["side"] for n in entry["submit"] for t in entry["tickets"] if t["n"] == n],
                         ["SELL", "BUY"])

    def test_a_kill_still_flattens_while_the_shared_switch_is_paused(self):
        fixture = self.risk_book([("SPY", 300)], peak=110_000.0)
        flag = paused_flag(fixture)
        _, plan = fixture.plan(risk_action=fixture.risk_report("KILL"), flag=flag)
        self.assertEqual(plan["switch"]["state"]["state"], "paused")
        self.assertTrue(plan["switch"]["decision"]["allow"])
        self.assertEqual({t["intent"] for t in plan["tickets"]}, {"EMERGENCY_EXIT"})
        self.assertTrue(plan["submit"])

    def test_a_pause_stops_an_alert_trim_because_a_trim_is_not_a_reduction_the_switch_waives(self):
        fixture = self.risk_book([("SPY", 400)])
        flag = paused_flag(fixture)
        code = fixture.invoke("plan", "--account", ACCOUNTS["competition"], "--snapshot",
                             str(fixture.snapshot()), "--targets", fixture.file("targets"),
                             "--quotes", fixture.file("quotes"), "--borrow", fixture.file("borrow"),
                             "--orders", fixture.file("orders"), "--evidence", fixture.file("evidence"),
                             "--risk-action", str(fixture.risk_report("ALERT")), "--flag", str(flag),
                             "--out", str(self.root / "risk-blocked.json"))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "risk-blocked.json").exists())
        self.assertFalse(fixture.control()["halted"], "a blocked batch must not latch anything")
        holds = list((golive.GO_DIR / fixture.now.astimezone(golive.ET).date().isoformat()
                      / ACCOUNTS["competition"]).glob("switch-hold-*.json"))
        self.assertEqual(len(holds), 1)
        self.assertIn("paused", json.loads(holds[0].read_text(encoding="utf-8"))["switch"]["state"]["state"])

    def test_a_report_the_fresh_book_does_not_confirm_places_no_ticket(self):
        for level in ("KILL", "ALERT"):
            with self.subTest(level=level):
                fixture = self.risk_book([("SPY", 100)])          # 10,000 USD: no rule is breached
                with self.assertRaises(golive.GoliveError) as raised:
                    fixture.plan(risk_action=fixture.risk_report(level))
                self.assertIn(f"the report says {level} but the fresh snapshot says", str(raised.exception))

    def test_a_warn_report_places_nothing_because_warn_has_no_ticket(self):
        fixture = self.risk_book([("SPY", 100)])
        with self.assertRaises(golive.GoliveError) as raised:
            fixture.plan(risk_action=fixture.risk_report("WARN"))
        self.assertIn("the fresh snapshot says OK", str(raised.exception))

    def test_a_stale_report_is_refused_before_it_sizes_an_order(self):
        fixture = self.risk_book([("SPY", 400)])
        stale = (fixture.now - timedelta(seconds=600)).isoformat()
        with self.assertRaises(golive.GoliveError) as raised:
            fixture.plan(risk_action=fixture.risk_report("ALERT", name="stale.json", generated_at=stale))
        self.assertIn("re-run the check", str(raised.exception))

    def test_a_report_from_the_other_account_or_an_unknown_shape_is_refused(self):
        fixture = self.risk_book([("SPY", 400)])
        with self.assertRaises(golive.GoliveError) as raised:
            fixture.plan(risk_action=fixture.risk_report("ALERT", name="wrong.json",
                                                         account=ACCOUNTS["test"], mode="test"))
        self.assertIn("risk action on the wrong book", str(raised.exception))
        broken = fixture.risk_report("ALERT", name="broken.json")
        payload = json.loads(broken.read_text(encoding="utf-8"))
        payload["schema_version"] = "pstar-risk/0"
        broken.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(golive.GoliveError) as raised:
            fixture.plan(risk_action=broken)
        self.assertIn("unsupported risk schema", str(raised.exception))

    def test_a_protected_name_above_the_line_is_reported_and_never_sold(self):
        fixture = self.risk_book([("SPY", 400), ("ABSI", -400)], attribute=False)
        fixture.attribute(["SPY"])                    # ABSI has no strategy open: it belongs to the user
        _, plan = fixture.plan(risk_action=fixture.risk_report("ALERT"))
        self.assertEqual([(t["symbol"], t["quantity"]) for t in plan["tickets"]], [("SPY", 20)])
        self.assertEqual(plan["risk_action"]["protected_skipped"], ["ABSI"])
        self.assertIn("ABSI is above 39% of initial assets", " ".join(plan["summary"]["slack_report"]))
        self.assertEqual(plan["target_shares"]["ABSI"], -400)   # counted, never traded
        # An ALERT whose only breach is a name the strategy may not trade has no ticket to build at all.
        other = self.risk_book([("ABSI", -400)], attribute=False)
        with self.assertRaises(golive.GoliveError) as raised:
            other.plan(risk_action=other.risk_report("ALERT", name="protected-only.json"))
        self.assertIn("the fresh snapshot says", str(raised.exception))

    def test_the_reports_peak_carries_the_kill_when_this_snapshot_has_never_seen_the_high_water_mark(self):
        # The auditor runs on another machine and keeps the peak in its own state file; a fresh executor
        # snapshot starts at today's equity, so the report is the only place the 110,000 peak still exists.
        fixture = self.risk_book([("SPY", 300), ("AAOI", -200)])          # no seeded peak: drawdown reads 0%
        self.assertEqual(json.loads(fixture.snapshot().read_text(encoding="utf-8"))["peak_equity"], EQUITY)
        report = fixture.risk_report("KILL", name="peak.json",
                                     findings=[{"rule": "drawdown", "value": -0.0909, "threshold": -0.08,
                                                "detail": "equity 100,000 of a 110,000 peak"}],
                                     peak=110_000.0, history=[{"day": "2026-11-20", "equity": 110_000.0}])
        path, plan = fixture.plan(risk_action=report)
        self.assertEqual(plan["intent"], "EMERGENCY_EXIT")
        self.assertEqual({(t["symbol"], t["side"], t["quantity"]) for t in plan["tickets"]},
                         {("SPY", "SELL", 300), ("AAOI", "BUY", 200)})
        self.assertAlmostEqual(plan["risk_action"]["drawdown"], EQUITY / 110_000.0 - 1, places=9)
        self.assertEqual((plan["risk_action"]["peak_used"], plan["risk_action"]["snapshot_peak"]),
                         (110_000.0, EQUITY))
        self.assertIn("day-keyed peak history", plan["risk_action"]["peak_source"])
        self.assertTrue(fixture.control()["halted"], "the adopted peak is what latched the halt")
        self.assertIn("the report's peak 110,000.00", self.captured(fixture, plan))

    def test_a_claimed_peak_the_report_cannot_show_history_for_is_refused(self):
        cases = {
            "no history at all": (110_000.0, [], "carries no day-by-day peak history"),
            "a history that never reached it": (110_000.0, [{"day": "2026-11-20", "equity": 105_000.0}],
                                                "its own history tops out"),
            "an implausible magnitude": (5_000_000.0, [{"day": "2026-11-20", "equity": 5_000_000.0}],
                                         "more than 10x"),
            "a row before the competition": (110_000.0, [{"day": "2026-11-13", "equity": 110_000.0}],
                                            "before the competition opened"),
            "a row dated in the future": (110_000.0, [{"day": "2026-11-27", "equity": 110_000.0}],
                                          "in the future"),
            "the same day twice": (110_000.0, [{"day": "2026-11-20", "equity": 110_000.0},
                                               {"day": "2026-11-20", "equity": 109_000.0}],
                                   "repeats a day"),
        }
        for why, (peak, history, needle) in cases.items():
            with self.subTest(case=why):
                fixture = self.risk_book([("SPY", 300)])
                with self.assertRaises(golive.GoliveError) as raised:
                    fixture.plan(risk_action=fixture.risk_report(
                        "KILL", name="peak-" + why.replace(" ", "-") + ".json", peak=peak, history=history))
                self.assertIn(needle, str(raised.exception))
                self.assertFalse(fixture.control()["halted"], f"{why}: a refused report latches nothing")

    def captured(self, fixture, plan) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            golive.print_plan(self.root / "plan.json", plan, plan["summary"]["rejected_tickets"])
        return buffer.getvalue()


class TestOperatorControlAndEvidence(GoliveTest):
    """The two blockers prompt 07 part 2 removes: R16 has to be arming-able, and the flags R11/R12/R14/R17
    spend have to be computed by Python rather than typed into a chat message.

    `release_check` asks git about the real repository, so a test that needs the flag to read true
    substitutes an answer for it and says so; reading git output is tested on its own further down.
    """

    def clean_release(self) -> dict:
        return {"verified": True, "commit": "f" * 40, "uncommitted": [],
                "reason": "fixture: every runtime .py matches the checked-out commit"}

    def dirty_release(self) -> dict:
        return {"verified": False, "commit": "f" * 40, "uncommitted": ["live/golive.py"],
                "reason": "1 uncommitted .py file(s) under the runtime directories: live/golive.py"}

    def test_resume_needs_no_typed_reply_and_records_where_it_came_from(self):
        fixture = self.fixture(resume=False)
        self.assertEqual((fixture.control()["halted"], fixture.control()["reason"]),
                         (True, "Never resumed"))
        with self.assertRaises(SystemExit):
            fixture.invoke("resume", "--account", fixture.account)     # no --source: argparse refuses
        with self.assertRaises(golive.GoliveError):
            fixture.resume(source="   ")
        self.assertTrue(fixture.control()["halted"])
        self.assertEqual(fixture.rows("decisions"), [], "a refused resume journals nothing")
        self.assertEqual(fixture.resume(source="slack RESUME 1763700000.000100"), 0)
        control = fixture.control()
        self.assertFalse(control["halted"])
        self.assertIs(control["verified_by_relay"], True)
        self.assertEqual(control["reason"], f"resumed at {fixture.iso}: slack RESUME 1763700000.000100")
        decision = fixture.rows("decisions")[-1]
        self.assertEqual((decision["kind"], decision["action"], decision["account_id"], decision["mode"]),
                         ("GOLIVE_RESUME", "RESUME", ACCOUNTS["test"], "test"))
        self.assertEqual(decision["reason"], control["reason"])

    def test_pause_needs_no_typed_reply_and_latches_the_same_switch_as_halt(self):
        fixture = self.fixture()
        self.assertFalse(fixture.control()["halted"], "a fresh automatic run starts from a running account")
        with self.assertRaises(golive.GoliveError):
            fixture.pause(source="")
        self.assertFalse(fixture.control()["halted"], "the refusal changes nothing")
        self.assertEqual(fixture.pause(source="slack PAUSE 1763700000.000100"), 0)
        control = fixture.control()
        self.assertIs(control["halted"], True)
        self.assertEqual(control["reason"], f"paused at {fixture.iso}: slack PAUSE 1763700000.000100")
        decision = fixture.rows("decisions")[-1]
        self.assertEqual((decision["kind"], decision["action"], decision["account_id"]),
                         ("GOLIVE_PAUSE", "PAUSE", ACCOUNTS["test"]))

    def test_pause_and_resume_refuse_an_account_of_the_other_mode(self):
        fixture = self.fixture(resume=False)
        with self.assertRaises(golive.GoliveError) as refused:
            fixture.resume(account=ACCOUNTS["competition"], source="user in chat")
        self.assertIn("config mode is 'test'", str(refused.exception))
        self.assertTrue(fixture.control(ACCOUNTS["competition"])["halted"])
        for account in ("5322439", "5322438 ", "0000000"):
            with self.assertRaises(golive.GoliveError) as refused:
                fixture.resume(account=account, source="user in chat")
            self.assertIn("authorised paper account", str(refused.exception))
        self.assertEqual(fixture.rows("decisions"), [], "a refusal writes no control and no journal row")
        other = Fixture(self.root, mode="competition", account=ACCOUNTS["competition"])
        with self.assertRaises(golive.GoliveError) as refused:
            other.pause(account=ACCOUNTS["test"], source="user in chat")
        self.assertIn("config mode is 'competition'", str(refused.exception))
        self.assertTrue(fixture.control()["halted"], "the test account is not paused by a competition call")
        self.assertEqual(other.resume(source="user in chat"), 0)
        self.assertFalse(other.control()["halted"])

    def test_resume_then_computed_evidence_clears_r12_r14_r16_and_r17(self):
        fixture = self.fixture(resume=False)
        self.assertEqual(fixture.resume(), 0)
        snapshot = fixture.snapshot()
        with mock.patch.object(golive, "release_check", return_value=self.clean_release()):
            status, path, evidence = fixture.generate_evidence(snapshot)
        self.assertEqual(status, 0, "every computed flag is true")
        self.assertEqual(evidence["assertions"], {"release_verified": True, "lease_verified": True,
                                                  "permission_policy_verified": True, "reconciled": True,
                                                  "snapshot_verified_by_relay": True})
        self.assertEqual(fixture.rows("decisions")[-1]["kind"], "GOLIVE_EVIDENCE")
        _, plan = fixture.plan(smoke=True, evidence=path, snapshot=snapshot)
        self.assertEqual(plan["evidence"]["assertions"], evidence["assertions"])
        self.assertTrue(plan["evidence"]["approval_present"])
        self.assertEqual(plan["submit"], [1, 2, 3, 4])
        for ticket in plan["tickets"]:
            self.assertEqual(ticket["gate"]["action"], "ALLOW")
            self.assertEqual(ticket["gate"]["failed_rules"], [])
            checks = {row["id"]: row for row in ticket["gate"]["checks"]}
            for rule in ("R12", "R14", "R16", "R17"):
                self.assertTrue(checks[rule]["passed"], f"{rule}: {checks[rule]}")
            self.assertIs(checks["R16"]["observed"], False, "R16 spent the resume, not an assumption")

    def test_r16_still_rejects_every_ticket_while_the_account_is_paused(self):
        fixture = self.fixture(resume=False)
        snapshot = fixture.snapshot()
        with mock.patch.object(golive, "release_check", return_value=self.clean_release()):
            status, path, evidence = fixture.generate_evidence(snapshot)
        self.assertEqual(status, 0, "the evidence is complete: the latch is a separate thing")
        self.assertTrue(all(evidence["assertions"].values()))
        _, plan = fixture.plan(smoke=True, evidence=path, snapshot=snapshot)
        self.assertEqual(plan["submit"], [])
        self.assertIs(plan["kill_switch"]["halted"], True)
        self.assertEqual(plan["kill_switch"]["reason"], "Never resumed")
        for ticket in plan["tickets"]:
            # The same latch is what R9 measures against the peak, so a paused account fails both rules.
            self.assertEqual(ticket["gate"]["failed_rules"], ["R9", "R16"])
        fixture.resume()
        _, rearmed = fixture.plan(smoke=True, evidence=path, snapshot=snapshot, out="rearmed")
        self.assertEqual(rearmed["submit"], [1, 2, 3, 4])

    def test_an_approval_older_than_the_five_minute_ttl_fails_r14(self):
        fixture = self.fixture()
        snapshot = fixture.snapshot()
        stale = (fixture.now - timedelta(seconds=300)).isoformat()
        with mock.patch.object(golive, "release_check", return_value=self.clean_release()):
            _, path, evidence = fixture.generate_evidence(snapshot, as_of=stale)
        self.assertEqual(evidence["approval"]["window_seconds"], golive.APPROVAL_WINDOW_SECONDS)
        self.assertEqual(evidence["approval"]["issued_at"], stale)
        self.assertEqual(timestamp(evidence["approval"]["expires_at"]) - fixture.now,
                         timedelta(seconds=-60), "the 240s window expired a minute before this plan")
        _, plan = fixture.plan(smoke=True, evidence=path, snapshot=snapshot)
        self.assertEqual(plan["submit"], [])
        for ticket in plan["tickets"]:
            self.assertIn("R14", ticket["gate"]["failed_rules"])

    def test_a_short_beyond_its_borrow_capacity_fails_r5(self):
        fixture = self.fixture(capacity=1)
        _, plan = fixture.plan(out="capacity")
        entry = next(ticket for ticket in plan["tickets"]
                     if ticket["side"] == "SELL" and ticket["intent"] == "ENTRY")
        self.assertEqual(entry["quantity"], 1, "one borrowable share is enough for the book as planned")
        order = dict(entry["order"])
        order["quantity"] = 2                       # the same name asked for more than the broker lends
        quotes = golive.parse_quotes(golive.read_json(fixture.paths["quotes"]))
        book = golive.sizing_quotes(quotes)
        borrow = golive.parse_borrow(fixture.paths["borrow"])
        records = golive.eligibility_records(fixture.config, book, borrow, {order["symbol"]: -2},
                                             fixture.evidence["borrow"]["easy_to_borrow_proxy"])
        flags = records[order["symbol"]]
        self.assertEqual(flags["short_capacity_shares"], 1)
        self.assertIs(flags["shortable"], False)
        self.assertIs(flags["easy_to_borrow"], False)
        context = golive.build_context(fixture.config, json.loads(fixture.snapshot().read_text("utf-8")),
                                       fixture.targets, book, records, [order], [], [], [],
                                       fixture.evidence, fixture.now)
        result = evaluate_pstar_order(order, context, fixture.config)
        self.assertIn("R5", result["failed_rules"])
        self.assertFalse(result["eligible"])

    def test_reconciled_is_false_while_the_history_still_has_a_live_order(self):
        fixture = self.fixture(resume=False)
        fixture.set_orders([fixture.order_row(golive.SMOKE_ETF, "BUY", 1, 0, "pstar-live-1",
                                              status=STATUS_WORKING)])
        snapshot = fixture.snapshot()
        with mock.patch.object(golive, "release_check", return_value=self.clean_release()):
            status, path, evidence = fixture.generate_evidence(snapshot, working=(STATUS_WORKING,))
        self.assertEqual(status, 2, "an unfinished book is not evidence that the tooling may plan a batch")
        self.assertIs(evidence["assertions"]["reconciled"], False)
        self.assertEqual([(row["state"], row["symbol"], row["status_code"])
                          for row in evidence["reconcile"]["unresolved"]],
                         [("WORKING", golive.SMOKE_ETF, STATUS_WORKING)])
        self.assertIn("still open or unreadable", evidence["bases"]["reconciled"])
        self.assertTrue(evidence["assertions"]["snapshot_verified_by_relay"],
                        "the live order is the only thing that failed here")
        fixture.resume()
        _, plan = fixture.plan(smoke=True, evidence=path, snapshot=snapshot)
        self.assertEqual(plan["submit"], [])
        for ticket in plan["tickets"]:
            self.assertIn("R12", ticket["gate"]["failed_rules"])
        fixture.set_orders([])
        with mock.patch.object(golive, "release_check", return_value=self.clean_release()):
            settled, _, after = fixture.generate_evidence(fixture.snapshot(), working=(STATUS_WORKING,))
        self.assertEqual(settled, 0, "a clear history reconciles: the live order was the whole reason")
        self.assertTrue(after["assertions"]["reconciled"])

    def test_the_release_flag_follows_what_git_says_about_the_runtime_directories(self):
        fixture = self.fixture()
        snapshot = fixture.snapshot()
        with mock.patch.object(golive, "release_check", return_value=self.dirty_release()):
            status, path, evidence = fixture.generate_evidence(snapshot)
        self.assertEqual(status, 2)
        self.assertIs(evidence["assertions"]["release_verified"], False)
        self.assertEqual(evidence["release"]["uncommitted"], ["live/golive.py"])
        self.assertTrue(evidence["assertions"]["snapshot_verified_by_relay"])
        _, plan = fixture.plan(smoke=True, evidence=path, snapshot=snapshot)
        self.assertEqual(plan["submit"], [])
        for ticket in plan["tickets"]:
            self.assertIn("R17", ticket["gate"]["failed_rules"])

    def test_release_state_reads_the_paths_git_reports_not_the_lines_it_happens_to_have(self):
        clean = golive.release_state(" M README.md\n?? reports/live/golive/plan.json\n", "abc123")
        self.assertTrue(clean["verified"])
        self.assertEqual(clean["commit"], "abc123")
        self.assertEqual(clean["uncommitted"], [])
        dirty = golive.release_state(" M live/golive.py\nR  live/old.py -> live/new.py\n"
                                     "?? journal/store.py\n M docs/GOLIVE.md\n", "abc123")
        self.assertFalse(dirty["verified"])
        self.assertEqual(dirty["uncommitted"], ["journal/store.py", "live/golive.py", "live/new.py"])
        for porcelain, head in ((None, "abc123"), ("", None), (None, None)):
            unavailable = golive.release_state(porcelain, head)
            self.assertFalse(unavailable["verified"], "no git answer is never a pass")
            self.assertIsNone(unavailable["commit"])
            self.assertIn("git", unavailable["reason"])

    def test_the_snapshot_attestation_refuses_a_file_that_is_not_the_tools_own(self):
        fixture = self.fixture()
        path = fixture.snapshot()
        snapshot = json.loads(path.read_text(encoding="utf-8"))

        def check(body, account=fixture.account, mode=fixture.mode):
            return golive.snapshot_attestation(body, path, account, mode)

        self.assertTrue(check(snapshot)["verified"], "the file this command wrote passes with no reasons")
        self.assertEqual(check(snapshot)["reasons"], [])
        self.assertFalse(check(snapshot, ACCOUNTS["competition"], "competition")["verified"])
        self.assertTrue(any("sim_trade_account_list" in line for line in
                            check({key: value for key, value in snapshot.items()
                                   if key != "account_binding"})["reasons"]))
        self.assertTrue(any("not a file" in line for line in
                            check({key: value for key, value in snapshot.items()
                                   if key != "source"})["reasons"]))
        self.assertTrue(any("relay assertion" in line for line in
                            check({**snapshot, "relay_assertion": None})["reasons"]))
        fixture.paths["cash"].write_text(fixture.paths["cash"].read_text(encoding="utf-8") + " ",
                                         encoding="utf-8")
        edited = check(snapshot)
        self.assertFalse(edited["verified"], "a raw response edited after the snapshot breaks the hash")
        self.assertTrue(any("no longer hashes" in line for line in edited["reasons"]))

    def test_pause_latches_the_switch_again_and_the_plan_stops(self):
        fixture = self.fixture()
        self.assertFalse(fixture.control()["halted"])
        self.assertEqual(fixture.pause(source="user in chat: stop for the night"), 0)
        control = fixture.control()
        self.assertIs(control["halted"], True)
        self.assertEqual(control["reason"], f"paused at {fixture.iso}: user in chat: stop for the night")
        decision = fixture.rows("decisions")[-1]
        self.assertEqual((decision["kind"], decision["action"]), ("GOLIVE_PAUSE", "PAUSE"))
        _, plan = fixture.plan(smoke=True)
        self.assertEqual(plan["submit"], [])
        self.assertIs(plan["kill_switch"]["halted"], True)
        for ticket in plan["tickets"]:
            self.assertIn("R16", ticket["gate"]["failed_rules"])
        self.assertEqual(fixture.resume(), 0)
        rearmed = fixture.plan(smoke=True, out="rearmed")
        self.assertEqual(rearmed[1]["submit"], [1, 2, 3, 4])
        self.assertEqual(fixture.pause(), 0)
        self.assertTrue(fixture.control()["halted"])

    def test_halt_is_an_alias_of_pause(self):
        fixture = self.fixture()
        self.assertEqual(fixture.invoke("halt", "--account", fixture.account,
                                        "--source", "user in chat"), 0)
        self.assertTrue(fixture.control()["halted"])
        self.assertEqual(fixture.rows("decisions")[-1]["kind"], "GOLIVE_PAUSE")

    def test_a_paused_account_still_allows_the_final_exit_reduction(self):
        """The scheduled final exit has to be able to close a book the user paused: R9 and R16 let it."""
        running_fixture = self.fixture()
        _, entry_plan = running_fixture.plan()
        buy = next(ticket for ticket in entry_plan["tickets"]
                   if ticket["intent"] == "ENTRY" and ticket["side"] == "BUY")
        name, held = buy["symbol"], buy["quantity"]
        fixture = Fixture(self.root, positions=[(name, held)])
        snapshot = json.loads(fixture.snapshot().read_text(encoding="utf-8"))
        quotes = golive.parse_quotes(golive.read_json(fixture.paths["quotes"]))
        book = golive.sizing_quotes(quotes)
        records = golive.eligibility_records(fixture.config, quotes,
                                             golive.parse_borrow(fixture.paths["borrow"]),
                                             entry_plan["target_shares"],
                                             fixture.evidence["borrow"]["easy_to_borrow_proxy"])
        touch = quotes[name]["bid"]
        reduce = {**buy["order"], "side": "SELL", "intent": "FINAL_EXIT", "quantity": held,
                  "limit_price": touch, "arrival_price": touch, "reason": "scheduled final exit",
                  "client_order_id": "pstar-final-exit"}
        add = {**buy["order"], "intent": "ENTRY"}

        def judge(order):
            """The production path: what `plan` does, so the durable pause is the one being tested."""
            context = golive.build_context(fixture.config, snapshot, fixture.targets, book, records,
                                           [order], [], [], [], fixture.evidence, fixture.now)
            return golive.gate_tickets([order], context, fixture.config, fixture.evidence, quotes,
                                       fixture.root / "journal")[0]["gate"]

        self.assertTrue(judge(reduce)["eligible"], "the exit is allowed while running")
        self.assertEqual(fixture.pause(source="slack PAUSE 1763700000.000100"), 0)
        self.assertIs(fixture.control()["halted"], True)
        closed = judge(reduce)
        self.assertTrue(closed["eligible"], f"a pause must not strand the book: "
                                            f"{closed['failed_rules']}")
        entry = judge(add)
        self.assertFalse(entry["eligible"], "the same paused account still rejects an entry")
        self.assertIn("R9", entry["failed_rules"])
        self.assertIn("R16", entry["failed_rules"], entry["failed_rules"])
        self.assertEqual(entry["action"], "FLATTEN_HALT")

    def test_pause_lists_exactly_the_working_strategy_orders(self):
        fixture = self.fixture()
        _, plan = fixture.plan()
        live = [ticket for ticket in plan["tickets"] if ticket["side"] == "BUY"][:2]
        rows = [fixture.order_row(ticket["symbol"], "BUY", ticket["quantity"], 0,
                                  ticket["client_order_id"], status=STATUS_WORKING)
                for ticket in live]
        # Everything else in that history is not this command's business: a filled strategy order, a
        # cancelled one, and a working order under another client's ids.
        rows += [fixture.order_row("IWM", "BUY", 5, 5, "pstar-other-filled", status=4),
                 fixture.order_row("QQQ", "SELL", 5, 0, "pstar-other-cancelled", status=5),
                 fixture.order_row("DIA", "BUY", 5, 0, "someone-elses-order", status=STATUS_WORKING)]
        history = fixture.history("pause_orders.json", rows)
        out = self.root / "cancel.json"
        self.assertEqual(fixture.pause(source="slack PAUSE 1763700000.000100", orders=history,
                                       out=out, working=(STATUS_WORKING,)), 0)
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(sorted(row["client_order_id"] for row in written["cancel"]),
                         sorted(ticket["client_order_id"] for ticket in live))
        for row in written["cancel"]:
            self.assertEqual(row["tool"], "sim_trade_cancel_order")
            self.assertEqual(row["payload"], {"acc_id": fixture.account, "market": golive.MARKET_ID,
                                              "order_id": row["order_id"]})
        self.assertEqual(written["account_id"], fixture.account)
        self.assertEqual(written["source"], "slack PAUSE 1763700000.000100")
        self.assertTrue(fixture.control()["halted"], "the cancel list is written next to the latch")

    def test_pause_refuses_an_order_history_from_the_other_account(self):
        fixture = self.fixture()
        rows = [fixture.order_row("IWM", "BUY", 5, 0, "pstar-elsewhere", status=STATUS_WORKING)]
        history = fixture.history("other_orders.json", envelope({"orders": rows, "account_id":
                                                                ACCOUNTS["competition"]}))
        with self.assertRaises(golive.GoliveError) as refused:
            fixture.pause(source="user in chat", orders=history)
        self.assertIn(ACCOUNTS["competition"], str(refused.exception))
        self.assertFalse(fixture.control()["halted"], "a refusal must not pause the wrong book either")


class TestCloudEvidence(GoliveTest):
    """`evidence --side cloud`: the three flags a scheduled Executor run has to earn from cloud evidence.

    A cloud session has no git checkout, nobody typing GO, and no claim on the local lease, so
    `release_verified` comes from the skill pins in `agents/templates.json` matched against
    `agents/release-lock.json`, `lease_verified` from the shared `PSTAR-PAUSE` read, and
    `permission_policy_verified` from a refusal somebody actually observed. Every fixture here is a file in
    one of those shapes; none of them is the repository's real answer, and the flag files are the exact
    payloads `pause_switch.write_plan` would have written.
    """

    ORDER_TOOLS = ["sim_trade_input_order", "sim_trade_cancel_order"]
    SKILLS = {"paper-trading-gate": ("skill_gate", "7"), "pstar-risk-check": ("skill_risk", "2"),
              "report-trading-day": ("skill_report", "4")}
    ROLES = {"coordinator": {"skills": ["report-trading-day"], "desired_order_tools": []},
             "risk-auditor": {"skills": ["pstar-risk-check"], "desired_order_tools": []},
             "executor": {"skills": ["pstar-risk-check", "paper-trading-gate"],
                          "desired_order_tools": ORDER_TOOLS},
             "scout": {"skills": [], "desired_order_tools": []}}

    def templates(self, fixture, *, roles=None, pinned=True, policy="observed", skill_versions=None,
                  name="templates.json") -> Path:
        """The role table as the cloud platform would hold it, with the pins the roles were deployed with."""
        denial = (None if policy == "absent" else
                  {"order_role": "executor", "platform_denial_verified": policy == "observed",
                   **({"observed_at": fixture.iso} if policy != "no_time" else {})})
        return fixture.write(name, {
            "schema_version": 1, "skills_are_pinned": pinned,
            "skill_versions": skill_versions if skill_versions is not None else
            {skill: {"cloud_skill_id": cloud_id, "version": version}
             for skill, (cloud_id, version) in self.SKILLS.items()},
            "roles": dict(roles or self.ROLES), "tool_policy": denial})

    def lock(self, fixture, *, skills=None, name="release-lock.json") -> Path:
        return fixture.write(name, {
            "release_name": "pstar-cloud-1", "local_revision": "0.3.5",
            "skills": {skill: {"cloud_skill_id": cloud_id, "expected_cloud_version": version}
                       for skill, (cloud_id, version) in (skills or self.SKILLS).items()}})

    def flag(self, fixture, desired, taken_at=None, name="flag.json") -> Path:
        """A raw `quote_user_security` read of exactly what writing that switch state would produce."""
        writes = pause_switch.write_plan(desired, OPS, taken_at or fixture.now, set())
        return fixture.write(name, envelope({"group_name": REG["group"], "read_at": fixture.iso,
                                             "security_list": [{"code": code}
                                                               for code in writes["expected_codes"]]}))

    def flatten(self, at=FLATTEN_START) -> Fixture:
        fixture = self.fixture(mode="competition", account=ACCOUNTS["competition"],
                               positions=list(FLATTEN_BOOK),
                               prices={"SPY": 620.0, "QQQ": 500.0, "AAOI": 30.0})
        fixture.to_utc(at)
        return fixture

    def cloud_evidence(self, fixture, *, trigger="scheduled run 12:15 final-exit", flag="default",
                       risk=None, templates=None, lock=None, auditor="session-auditor-1",
                       executor="session-executor-1", side="cloud", out="evidence-cloud"):
        argv = ["evidence", "--account", fixture.account, "--snapshot", str(fixture.snapshot()),
                "--orders", fixture.file("orders"), "--side", side, "--trigger", trigger,
                "--out", str(fixture.root / f"{out}.json"),
                "--templates", str(templates or self.templates(fixture)),
                "--release-lock", str(lock or self.lock(fixture)),
                "--auditor-session", auditor, "--executor-session", executor]
        if flag == "default":
            flag = self.flag(fixture, {"state": "running", "lease": "cloud-executor"})
        if flag is not None:
            argv += ["--flag", str(flag)]
        if risk is not None:
            argv += ["--risk-report", str(risk)]
        status = fixture.invoke(*argv)
        path = Path(argv[argv.index("--out") + 1])
        return status, path, json.loads(path.read_text(encoding="utf-8"))

    def test_the_cloud_flags_come_from_pins_the_flag_and_an_observed_refusal(self):
        fixture = self.flatten()
        # git is the local release proof; a cloud session has no checkout to ask, so it must not be called.
        with mock.patch.object(golive, "release_check",
                               side_effect=AssertionError("cloud evidence asked git")):
            status, path, evidence = self.cloud_evidence(fixture)
        self.assertEqual(status, 0)
        self.assertEqual(evidence["assertions"], {"release_verified": True, "lease_verified": True,
                                                 "permission_policy_verified": True, "reconciled": True,
                                                 "snapshot_verified_by_relay": True})
        self.assertEqual(evidence["side"], "cloud")
        self.assertEqual(sorted(evidence["release"]["pinned"]),
                         sorted(["executor/paper-trading-gate", "executor/pstar-risk-check",
                                 "risk-auditor/pstar-risk-check", "coordinator/report-trading-day"]))
        self.assertEqual(evidence["release"]["pinned"]["executor/pstar-risk-check"], "skill_risk@2")
        self.assertEqual(evidence["release"]["commit"], "pstar-cloud-1")
        self.assertIn("skill pins", evidence["bases"]["release_verified"])
        self.assertIn("cloud-executor lease is held", evidence["bases"]["lease_verified"])
        self.assertIn("platform policy observed", evidence["bases"]["permission_policy_verified"])
        self.assertNotIn("limitation:", evidence["bases"]["permission_policy_verified"])
        self.assertEqual((evidence["approval"]["auditor_session_id"], evidence["approval"]["guard_session_id"]),
                         ("session-auditor-1", "session-executor-1"))
        self.assertIn("deterministic gate", evidence["approval"]["confirmed_by"])
        self.assertEqual(fixture.rows("decisions")[-1]["kind"], "GOLIVE_EVIDENCE")
        self.assertTrue(fixture.rows("decisions")[-1]["reason"].startswith("cloud: release "),
                        "the journal says which executor the evidence was written for")
        _, plan = fixture.plan(final_exit=True, evidence=path,
                               flag=self.flag(fixture, {"state": "running", "lease": "cloud-executor"}))
        self.assertEqual(plan["switch"]["holder"], "cloud-executor")
        self.assertEqual(plan["submit"], [ticket["n"] for ticket in plan["tickets"]])
        for ticket in plan["tickets"]:
            checks = {row["id"]: row for row in ticket["gate"]["checks"]}
            self.assertTrue(checks["R17"]["passed"], f"R17: {checks['R17']}")

    def test_a_pin_that_is_not_the_sealed_version_is_not_a_release(self):
        fixture = self.flatten()
        templates = self.templates(fixture, skill_versions={
            **{skill: {"cloud_skill_id": cloud_id, "version": version}
               for skill, (cloud_id, version) in self.SKILLS.items()},
            "pstar-risk-check": {"cloud_skill_id": "skill_risk", "version": "3"}})
        _, path, evidence = self.cloud_evidence(fixture, templates=templates)
        self.assertIs(evidence["assertions"]["release_verified"], False)
        self.assertIn("pins skill_risk@3 but the release lock sealed skill_risk@2",
                      evidence["release"]["reason"])
        _, plan = fixture.plan(final_exit=True, evidence=path,
                               flag=self.flag(fixture, {"state": "running", "lease": "cloud-executor"}))
        self.assertEqual(plan["submit"], [])
        for ticket in plan["tickets"]:
            self.assertIn("R17", ticket["gate"]["failed_rules"])

    def test_latest_and_an_unsealed_skill_are_both_refused(self):
        fixture = self.flatten()
        loose = self.templates(fixture, skill_versions={
            "pstar-risk-check": {"version": "2"},                       # no cloud skill id
            "paper-trading-gate": {},                                   # neither id nor version
            "report-trading-day": {"cloud_skill_id": "skill_report", "version": "4"}})
        _, _, evidence = self.cloud_evidence(fixture, templates=loose)
        self.assertIs(evidence["assertions"]["release_verified"], False)
        self.assertIn("executor: skill paper-trading-gate names no cloud skill id and version, which is "
                      "`latest`", evidence["release"]["reason"])
        self.assertIn("risk-auditor: skill pstar-risk-check names no cloud skill id and version",
                      evidence["release"]["reason"])
        unsealed = self.lock(fixture, skills={skill: value for skill, value in self.SKILLS.items()
                                             if skill != "report-trading-day"})
        _, _, second = self.cloud_evidence(fixture, lock=unsealed, out="evidence-unsealed")
        self.assertIs(second["assertions"]["release_verified"], False)
        self.assertIn("report-trading-day: agents/release-lock.json seals no cloud skill id/version",
                      second["release"]["reason"])
        unpinned = self.templates(fixture, pinned=False)
        _, _, third = self.cloud_evidence(fixture, templates=unpinned, out="evidence-unpinned")
        self.assertIn("does not declare skills_are_pinned true", third["release"]["reason"])

    def test_the_lease_is_whatever_the_shared_flag_said_when_the_cloud_read_it(self):
        fixture = self.flatten()
        held = self.flag(fixture, {"state": "running", "lease": "local-entry"})
        _, _, evidence = self.cloud_evidence(fixture, flag=held, out="evidence-local-lease")
        self.assertIs(evidence["assertions"]["lease_verified"], False)
        self.assertIn("the live lease belongs to local-entry", evidence["bases"]["lease_verified"])
        free = self.flag(fixture, {"state": "running", "lease": None})
        _, _, takeable = self.cloud_evidence(fixture, flag=free, out="evidence-free")
        self.assertIs(takeable["assertions"]["lease_verified"], True)
        self.assertIn("may take it", takeable["bases"]["lease_verified"])
        expired = self.flag(fixture, {"state": "running", "lease": "local-entry"},
                            taken_at=fixture.now - timedelta(days=1), name="flag-old.json")
        _, _, aged = self.cloud_evidence(fixture, flag=expired, out="evidence-expired")
        self.assertIs(aged["assertions"]["lease_verified"], True)
        self.assertIn("expired", aged["bases"]["lease_verified"])
        _, _, noflag = self.cloud_evidence(fixture, flag=None, out="evidence-noflag")
        self.assertIs(noflag["assertions"]["lease_verified"], False)
        self.assertIn("cloud mode needs --flag", noflag["bases"]["lease_verified"])
        self.assertIn(REG["group"], noflag["bases"]["lease_verified"])
        self.assertIn("read at", evidence["bases"]["lease_verified"],
                      "a refusal names the flag read it came from, so the operator can re-check it")

    def test_a_pause_cancel_and_a_kill_batch_never_wait_for_the_lease(self):
        """Both actions only take risk off, so the local lease cannot strand them; the flag still gets read."""
        fixture = self.flatten()
        held = self.flag(fixture, {"state": "paused", "lease": "local-entry"})
        _, _, paused = self.cloud_evidence(fixture, trigger="slack PAUSE 1763700000.000100", flag=held,
                                           out="evidence-pause")
        self.assertIs(paused["assertions"]["lease_verified"], True)
        self.assertIn("only cancels live orders", paused["bases"]["lease_verified"])
        self.assertEqual(paused["lease"]["verified"], True, "the exemption is stated, not left blank")
        _, _, killed = self.cloud_evidence(fixture, trigger="risk KILL risk-5832812.json", flag=held,
                                           risk=fixture.risk_report("KILL", name="risk-5832812.json"),
                                           out="evidence-kill")
        self.assertIs(killed["assertions"]["lease_verified"], True)
        self.assertIn("flattens the book", killed["bases"]["lease_verified"])
        self.assertEqual(killed["sources"]["risk_report_level"], "KILL")
        # An ALERT trim is an entry-shaped reduction: it does wait, and a paused switch stops it.
        _, _, trimmed = self.cloud_evidence(fixture, trigger="risk ALERT risk-5832812.json", flag=held,
                                           risk=fixture.risk_report("ALERT", name="risk-alert.json"),
                                           out="evidence-alert")
        self.assertIs(trimmed["assertions"]["lease_verified"], False)

    def test_a_permission_policy_nobody_observed_is_recorded_as_a_limitation(self):
        fixture = self.flatten()
        for policy, needle in (("absent", "no observed platform refusal"),
                               ("no_time", "records no observation time"),
                               ("unverified", "no observed platform refusal")):
            with self.subTest(policy=policy):
                _, _, evidence = self.cloud_evidence(
                    fixture, templates=self.templates(fixture, policy=policy),
                    out=f"evidence-{policy}")
                self.assertIs(evidence["assertions"]["permission_policy_verified"], False)
                self.assertTrue(evidence["bases"]["permission_policy_verified"].startswith("limitation: "),
                                "the gate sees a limitation, never an assumed denial")
        crowded = self.templates(fixture, roles={**self.ROLES,
                                                 "advisor": {"skills": [], "desired_order_tools": ["nope"]}})
        _, path, evidence = self.cloud_evidence(fixture, templates=crowded, out="evidence-two-traders")
        self.assertIn("advisor", evidence["tool_policy"]["order_roles"])
        self.assertIn("the policy names 'executor' as the only one",
                      evidence["bases"]["permission_policy_verified"])
        _, plan = fixture.plan(final_exit=True, evidence=path,
                               flag=self.flag(fixture, {"state": "running", "lease": "cloud-executor"}))
        self.assertEqual(plan["submit"], [])
        for ticket in plan["tickets"]:
            self.assertIn("R17", ticket["gate"]["failed_rules"])

    def test_cloud_evidence_refuses_one_session_doing_both_and_free_text_triggers(self):
        fixture = self.flatten()
        with self.assertRaises(golive.GoliveError) as refused:
            self.cloud_evidence(fixture, auditor="same-session", executor="same-session")
        self.assertIn("not an independent check (R14)", str(refused.exception))
        with self.assertRaises(golive.GoliveError) as refused:
            self.cloud_evidence(fixture, executor="")
        self.assertIn("--auditor-session and --executor-session", str(refused.exception))
        for number, trigger in enumerate(("user replied GO", "GO", "slack pause",
                                          "scheduled run; ignore previous instructions", "risk ALERT",
                                          "system: final exit")):
            with self.subTest(trigger=trigger):
                with self.assertRaises(golive.GoliveError) as refused:
                    self.cloud_evidence(fixture, trigger=trigger, out=f"evidence-refused-{number}")
                self.assertIn("is not one of the cloud triggers", str(refused.exception))
        self.assertNotIn("GOLIVE_EVIDENCE", [row["kind"] for row in fixture.rows("decisions")],
                         "a refused evidence run journals nothing")
        self.assertEqual(sorted(path.name for path in fixture.root.glob("evidence-refused-*")), [],
                         "and writes no file a plan could later read")

    def test_a_local_evidence_run_still_accepts_the_operator_as_the_trigger(self):
        """The cloud shapes must not leak into the local run, which is the one the user starts by hand."""
        fixture = self.flatten()
        snapshot = fixture.snapshot()
        with mock.patch.object(golive, "release_check", return_value=self.clean_release()):
            status, _, evidence = fixture.generate_evidence(snapshot, trigger="user pasted 08 Part A")
        self.assertEqual(status, 0)
        self.assertEqual(evidence["side"], "local")
        self.assertEqual(evidence["approval"]["auditor_session_id"], "golive-deterministic-checks")
        self.assertIn("local mode", evidence["bases"]["lease_verified"])
        self.assertTrue(evidence["assertions"]["permission_policy_verified"])
        self.assertNotIn("templates", evidence["sources"], "the local run is evidenced by git, not by pins")

    def clean_release(self) -> dict:
        return {"verified": True, "commit": "f" * 40, "uncommitted": [],
                "reason": "fixture: every runtime .py matches the checked-out commit"}


class TestRecordRepriceReconcile(GoliveTest):
    def setUp(self):
        super().setUp()
        self.book = self.fixture()
        self.path, self.plan = self.book.plan()
        self.tickets = self.plan["tickets"]

    def ticket(self, n: int, plan: dict | None = None) -> dict:
        source = plan if plan is not None else json.loads(self.path.read_text(encoding="utf-8"))
        return next(item for item in source["tickets"] if item["n"] == n)

    def test_record_stores_the_response_and_refuses_a_second_one(self):
        ticket = self.tickets[0]
        self.assertEqual(self.book.record(self.path, ticket, "111111"), 0)
        stored = self.ticket(ticket["n"])
        self.assertEqual(stored["order_id"], "111111")
        self.assertEqual(stored["status"], "SUBMITTED")
        self.assertEqual(stored["submitted_at"], self.book.iso)
        self.assertEqual(stored["attempts"], 1)
        self.assertEqual(stored["reprice_round"], 0)
        self.assertEqual(stored["submissions"][0]["order_id"], "111111")
        decisions = [row for row in self.book.rows("decisions") if row["kind"] == "GOLIVE_RECORD"]
        self.assertEqual([(row["action"], row["symbol"]) for row in decisions],
                         [("SUBMITTED", ticket["symbol"])])
        with self.assertRaises(golive.GoliveError) as refused:
            self.book.record(self.path, ticket, "222222")
        self.assertIn("never sent again", str(refused.exception))
        after = self.ticket(ticket["n"])
        self.assertEqual(after["order_id"], "111111")
        self.assertEqual(len(after["submissions"]), 1)

    def test_a_submitted_ticket_is_never_planned_again(self):
        ticket = self.tickets[0]
        self.book.record(self.path, ticket, "111111")
        with self.assertRaises(golive.GoliveError) as refused:
            self.book.plan(out="plan")
        self.assertIn("never planned or sent again", str(refused.exception))
        # A new plan file is allowed, but the order that is already at the broker may not be re-submitted:
        # R12 sees the live row in the fresh history and holds that ticket out of the submit list.
        self.book.advance(1, refresh=("orders",))
        self.book.orders = [self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"], 0,
                                               ticket["client_order_id"], status=STATUS_WORKING)]
        self.book.write("orders.json", envelope({"orders": self.book.orders,
                                                "pagination": {"has_more": False, "next_key": ""}}))
        # --working proves the unmapped code means a live order; without it R12 holds the whole batch.
        _, again = self.book.plan(out="again", working=(STATUS_WORKING,))
        duplicate = next(t for t in again["tickets"] if t["symbol"] == ticket["symbol"])
        self.assertIn("R12", duplicate["gate"]["failed_rules"])
        self.assertNotIn(duplicate["n"], again["submit"])
        self.assertEqual(len(again["submit"]), len(self.tickets) - 1)

    def test_a_resend_needs_a_fresh_history_and_refuses_a_matching_order(self):
        ticket = self.tickets[0]
        response = self.book.write("empty_response.json", envelope({"order_id": "",
                                                                    "status": None}))
        resend = ["record", "--plan", str(self.path), "--ticket", str(ticket["n"]),
                  "--response", str(response)]
        self.assertEqual(self.book.invoke(*resend), 2)
        first = self.ticket(ticket["n"])
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["status"], "ERROR")
        self.assertIn("ret_code=0", first["error"])
        self.assertIsNone(first.get("order_id"))
        with self.assertRaises(golive.GoliveError) as refused:
            self.book.invoke(*resend)
        self.assertIn("sim_trade_history_order_list", str(refused.exception))
        row = self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"], 0,
                                  ticket["client_order_id"], status=STATUS_WORKING)
        history = self.book.history("dup_orders.json", [row])
        with self.assertRaises(golive.GoliveError) as refused:
            self.book.invoke(*resend, "--orders", str(history))
        self.assertIn("already in the broker history", str(refused.exception))
        clean = self.book.history("clean_orders.json", [])
        self.assertEqual(self.book.invoke(*resend, "--orders", str(clean)), 2)
        self.assertEqual(self.ticket(ticket["n"])["attempts"], 2)

    def test_reprice_waits_the_five_minutes_first(self):
        ticket = self.tickets[0]
        submitted = self.book.advance(1)
        self.book.record(self.path, ticket, as_of=submitted)
        self.book.advance(120)
        row = self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"], 0,
                                  ticket["client_order_id"], status=STATUS_WORKING, at=submitted)
        code, plan = self.book.reprice(self.path, [row], name="too_early")
        self.assertEqual(code, 0)
        self.assertEqual(len(plan["tickets"]), len(self.tickets))
        parent = self.ticket(ticket["n"], plan)
        self.assertEqual(parent["status"], "SUBMITTED")
        self.assertNotIn("cancel", parent)
        self.assertEqual(plan["blockers"], [])

    def test_reprice_cancels_and_reissues_up_to_three_rounds_then_stops(self):
        ticket = self.tickets[0]
        submitted = self.book.advance(1)
        self.book.record(self.path, ticket, as_of=submitted)
        live, previous_n = ticket, ticket["n"]
        for rounds in (1, 2, 3):
            at = self.book.advance(golive.REPRICE_AFTER_SECONDS + 100)
            row = self.book.order_row(live["symbol"], live["side"], live["quantity"], 0,
                                      live["client_order_id"], status=STATUS_WORKING, at=at)
            code, plan = self.book.reprice(self.path, [row], name=f"round{rounds}")
            self.assertEqual(code, 0, f"round {rounds} refused")
            self.assertEqual(len(plan["tickets"]), len(self.tickets) + rounds)
            child = plan["tickets"][-1]
            parent = self.ticket(previous_n, plan)
            self.assertEqual(child["reprice_round"], rounds)
            self.assertEqual(child["parent"], parent["n"])
            self.assertEqual(child["symbol"], ticket["symbol"])
            self.assertEqual(child["quantity"], live["quantity"])
            self.assertTrue(child["client_order_id"].endswith(f"-r{rounds}"))
            self.assertEqual(child["payload"]["text"], child["client_order_id"])
            self.assertEqual(child["gate"]["action"], "ALLOW")
            self.assertEqual(child["status"], "PLANNED")
            self.assertEqual(parent["status"], "REPRICING")
            self.assertEqual(parent["cancel"]["tool"], "sim_trade_cancel_order")
            self.assertEqual(parent["cancel"]["payload"]["order_id"], parent["order_id"])
            self.assertEqual(parent["cancel"]["payload"]["acc_id"], ACCOUNTS["test"])
            self.assertEqual(parent["child"], child["n"])
            live, previous_n = child, child["n"]
            self.book.record(self.path, live, as_of=at)
        at = self.book.advance(golive.REPRICE_AFTER_SECONDS + 100)
        row = self.book.order_row(live["symbol"], live["side"], live["quantity"], 0,
                                  live["client_order_id"], status=STATUS_WORKING, at=at)
        code, plan = self.book.reprice(self.path, [row], name="round4")
        self.assertEqual(code, 0)
        self.assertEqual(len(plan["tickets"]), len(self.tickets) + 3)
        capped = self.ticket(previous_n, plan)
        self.assertEqual(capped["status"], "skipped_unfilled")
        self.assertIn("cancel only", capped["note"])

    def test_reprice_refuses_a_ticket_whose_status_code_is_not_mapped(self):
        ticket = self.tickets[0]
        submitted = self.book.advance(1)
        self.book.record(self.path, ticket, as_of=submitted)
        at = self.book.advance(golive.REPRICE_AFTER_SECONDS + 100)
        row = self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"], 0,
                                  ticket["client_order_id"], status=9, at=at)
        _, plan = self.book.reprice(self.path, [row], name="odd_code")
        self.assertEqual([block["symbol"] for block in plan["blockers"]], [ticket["symbol"]])
        self.assertIn("--working", plan["blockers"][0]["reason"])
        self.assertEqual(len(plan["tickets"]), len(self.tickets))

    def test_reconcile_flags_a_missing_fill_and_journals_the_rest(self):
        # The largest-notional ticket: leaving it unfilled is both a missing fill and a deviation above
        # the 0.25%-of-equity band, which is what the reconcile report has to flag.
        missing = max(self.tickets, key=lambda t: t["quantity"] * self.book.price_of(t["symbol"]))
        filled = [t for t in self.tickets if t is not missing]
        rows = []
        for index, ticket in enumerate(filled):
            self.book.record(self.path, ticket)
            rows.append(self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"],
                                            ticket["quantity"], ticket["client_order_id"]))
        self.book.record(self.path, missing)
        rows.append(self.book.order_row(missing["symbol"], missing["side"], missing["quantity"], 0,
                                        missing["client_order_id"], status=5))
        self.set_book(filled)
        code, report = self.book.reconcile(self.path, rows)
        self.assertEqual(code, 2)
        self.assertFalse(report["complete_reconciliation"])
        self.assertEqual({row["symbol"] for row in report["missing_fills"]}, {missing["symbol"]})
        self.assertEqual(report["missing_fills"][0]["filled"], 0)
        self.assertEqual({row["symbol"] for row in report["deviations"]}, {missing["symbol"]})
        self.assertEqual(report["tickets"], len(self.tickets))
        self.assertEqual(report["orders_at_broker"], len(self.tickets))
        self.assertEqual(report["filled_complete"], len(filled))
        self.assertEqual(report["executed_orders_today"], len(filled))
        self.assertEqual(report["order_budget"], self.book.config["competition_order_cap"]
                         - self.book.config["emergency_flatten_reserve"])
        self.assertEqual(report["budget_left"], report["order_budget"] - len(filled))
        self.assertEqual(len(self.book.rows("fills")), len(filled))
        self.assertEqual({row["symbol"] for row in self.book.rows("fills")},
                         {ticket["symbol"] for ticket in filled})
        self.assertIn(missing["symbol"], (self.path.parent / "summary.md").read_text(encoding="utf-8"))

    def test_reconcile_reports_the_filled_book_against_the_targets(self):
        rows = []
        for index, ticket in enumerate(self.tickets):
            self.book.record(self.path, ticket, f"4{index:05d}")
            rows.append(self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"],
                                            ticket["quantity"], ticket["client_order_id"]))
        self.set_book(self.tickets)
        code, report = self.book.reconcile(self.path, rows)
        self.assertEqual(code, 0)
        self.assertTrue(report["complete_reconciliation"])
        self.assertEqual(report["deviations"], [])
        self.assertEqual(report["missing_fills"], [])
        self.assertEqual(report["positions"], len(self.tickets))
        self.assertAlmostEqual(report["margin"], self.plan["summary"]["margin_after"], places=4)
        self.assertEqual(report["max_ticker"], self.plan["summary"]["largest_ticker"])
        self.assertLessEqual(report["max_ticker_vs_initial"],
                             self.book.config["ticker_cap_of_initial"])
        self.assertEqual(report["executed_orders_today"], len(self.tickets))
        self.assertIsNotNone(report["average_slippage_bps"])

    def test_an_unmapped_status_is_settled_by_quantity_or_by_the_operator(self):
        ticket = self.tickets[0]
        self.book.record(self.path, ticket)
        # cum_qty equals qty: the quantity columns settle the fill, whatever the unmapped code says.
        complete = self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"],
                                       ticket["quantity"], ticket["client_order_id"],
                                       status=STATUS_WORKING)
        _, report = self.book.reconcile(self.path, [complete])
        self.assertEqual(report["unresolved_status_codes"], [])
        self.assertEqual(report["filled_complete"], 1)
        # Nothing filled: golive will not call an unmapped code live or finished without proof.
        self.book.advance(1, refresh=("cash", "positions"))
        open_row = self.book.order_row(ticket["symbol"], ticket["side"], ticket["quantity"], 0,
                                       ticket["client_order_id"], status=STATUS_WORKING)
        _, unresolved = self.book.reconcile(self.path, [open_row])
        self.assertEqual([item["status_code"] for item in unresolved["unresolved_status_codes"]],
                         [STATUS_WORKING])
        self.assertEqual(unresolved["filled_complete"], 0)
        self.assertFalse(unresolved["complete_reconciliation"])
        _, proven = self.book.reconcile(self.path, [open_row], working=(STATUS_WORKING,))
        self.assertEqual(proven["unresolved_status_codes"], [])
        self.assertEqual(proven["executed_orders_today"], 0)
        self.assertEqual(proven["orders_at_broker"], 1)
        self.assertFalse(proven["complete_reconciliation"])
        self.assertEqual([(row["state"], row["filled"], row["quantity"]) for row in
                          proven["open_orders"]], [("WORKING", 0, ticket["quantity"])])
        # An operator-declared live order is not a fill: the fills journal still holds only the row
        # from the order whose quantity columns proved it complete.
        self.assertEqual([row["quantity"] for row in self.book.rows("fills")], [ticket["quantity"]])

    def test_every_status_code_moomoo_documents_maps_to_one_state(self):
        """The simulated order list documents codes 1-7; each one has to land in exactly one of our states.

        A code the documentation does not carry is a different matter: `order_state` leaves it UNKNOWN
        until the operator says what it means with `--working`, which is what the test above proves.
        """
        documented = {1: "WORKING", 2: "WORKING", 3: "PARTIAL", 4: "FILLED", 5: "CLOSED_NO_FILL",
                      6: "CLOSED_NO_FILL", 7: "CANCELLED_PARTIAL"}
        filled_by_code = {3: 1, 4: 5, 7: 1}
        for code, state in documented.items():
            with self.subTest(code=code):
                self.assertEqual(golive.order_state(5, filled_by_code.get(code, 0), code), state)
        self.assertEqual({code for group in (golive.STATUS_LIVE, golive.STATUS_FILLED,
                                             golive.STATUS_CLOSED_NO_FILL,
                                             golive.STATUS_CANCELLED_PARTIAL) for code in group},
                         set(documented))
        self.assertEqual(golive.order_state(5, 5, 3), "FILLED", "the quantity columns outrank the status")

    def test_a_cancelled_partial_fill_is_resolved_and_does_not_hold_the_batch(self):
        """Status 7 (filled, then cancelled) is a finished fact: no unknown, no R12 block, no re-cancel."""
        symbol = self.tickets[0]["symbol"]
        # The default fixture sizes one share per name, so price this one at half: two shares, one filled.
        prices = {**self.book.prices, symbol: round(self.book.prices[symbol] / 2, 2)}
        book = Fixture(self.root, prices=prices)
        path, plan = book.plan(out="plan-half")
        ticket = next(item for item in plan["tickets"] if item["symbol"] == symbol)
        self.assertEqual(ticket["quantity"], 2, ticket)
        submitted = book.advance(1)
        book.record(path, ticket, as_of=submitted)
        at = book.advance(golive.REPRICE_AFTER_SECONDS + 100)
        row = book.order_row(symbol, ticket["side"], 2, 1, ticket["client_order_id"], status=7, at=at)
        code, repriced = book.reprice(path, [row])
        self.assertEqual(code, 0, "a resolved row never blocks the next round")
        parent = next(item for item in repriced["tickets"] if item["n"] == ticket["n"])
        self.assertEqual(parent["broker_state"], "CANCELLED_PARTIAL")
        self.assertFalse(parent["cancel"]["needed"], "the broker has nothing live to cancel")
        child = repriced["tickets"][-1]
        self.assertEqual(child["quantity"], 1)
        self.assertTrue(child["submit"], child["gate"]["failed_rules"])
        book.positions = [(symbol, 1 if ticket["side"] == "BUY" else -1)]
        book.write("cash.json", book.cash())
        book.write("positions.json", envelope({"positions": book.position_rows()}))
        _, report = book.reconcile(path, [row])
        self.assertEqual(report["unresolved_status_codes"], [])
        self.assertEqual(report["open_orders"], [])
        self.assertEqual([item["filled"] for item in report["missing_fills"]], [1],
                         "the shortfall is a missing fill, which is a fact, not an unknown")
        self.assertEqual([row["quantity"] for row in book.rows("fills")], [1])
        release = {"verified": True, "commit": "f" * 40, "uncommitted": [],
                   "reason": "fixture: every runtime .py matches the checked-out commit"}
        with mock.patch.object(golive, "release_check", return_value=release):
            status, _, evidence = book.generate_evidence(
                book.snapshot(), orders="reconcile_orders", out="evidence-7")
        self.assertEqual(status, 0, "R17 reads a cancelled partial as reconciled")
        self.assertTrue(evidence["assertions"]["reconciled"], evidence["reconcile"])

    def set_book(self, tickets):
        """Put the account into the state the fills describe, then re-capture cash and positions."""
        self.book.positions = [(ticket["symbol"], ticket["quantity"] if ticket["side"] == "BUY"
                                else -ticket["quantity"]) for ticket in tickets]
        self.book.write("cash.json", self.book.cash())
        self.book.write("positions.json", envelope({"positions": self.book.position_rows()}))


class TestPricePolicyEvidence(GoliveTest):
    def test_the_planner_prices_at_the_mid_which_is_why_golive_overrides_it(self):
        fixture = self.fixture()
        quotes = {name: {"price": price, "verified_by_relay": True}
                  for name, price in fixture.prices.items()}
        baseline = planner_plan(fixture.config, fixture.targets,
                                {"equity": EQUITY, "account_id": fixture.account, "positions": []}, quotes)
        self.assertTrue(baseline["orders"])
        for order in baseline["orders"]:
            self.assertEqual(order["limit_price"], fixture.prices[order["symbol"]])
        _, plan = fixture.plan()
        for ticket in plan["tickets"]:
            self.assertNotEqual(ticket["limit_price"], plan["quotes"][ticket["symbol"]]["price"])
            self.assertAlmostEqual(ticket["arrival_price"], plan["quotes"][ticket["symbol"]]["price"])


if __name__ == "__main__":
    unittest.main()
