"""Standard-library tests; temporary test data stays inside dashboard/."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from dashboard.server import (
    BASE, MAX_ROWS, Issues, StateSource, clean_snapshot, json_bytes,
    make_server, summarize,
)


def sample_snapshot():
    return {
        "as_of": "2026-11-18T21:00:00Z", "mode": "test", "account_id": 5322438,
        "account_type": "SIMULATE", "equity": 1100, "initial_equity": 1000,
        "previous_close_equity": 1050, "peak_equity": 1200,
        "positions": [{"symbol": "TEST", "quantity": 2, "price": 100, "beta": 1.5},
                      {"symbol": "HEDGE", "quantity": -1, "price": 50, "beta": 1}],
        "equity_history": [], "orders": [], "agents": [],
        "usage": {"credits": 4.25, "as_of": "2026-11-18T21:00:00Z"},
    }


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dashboard-test-", dir=BASE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot_path = self.root / "status.json"
        self.journal = self.root / "journal"
        self.journal.mkdir()

    def write_snapshot(self, value):
        self.snapshot_path.write_text(json.dumps(value), encoding="utf-8")

    def test_default_has_no_data_and_never_opens_source(self):
        with patch.object(Path, "open", side_effect=AssertionError("Unexpected read")):
            state = StateSource().state()
        self.assertEqual(state["status"], "empty")
        self.assertEqual(state["source_mode"], "none")
        self.assertIsNone(state["snapshot"])
        self.assertFalse(state["connected"])
        self.assertFalse(state["execution_enabled"])
        self.assertIsNone(state["summary"]["net_beta"])
        self.assertEqual(state["decisions"], [])

    def test_fixture_is_opt_in_synthetic_and_no_credits(self):
        state = StateSource(fixture=True).state()
        self.assertEqual(state["status"], "ready")
        self.assertTrue(state["synthetic"])
        self.assertEqual(state["snapshot"]["account_id"], "5322438")
        self.assertEqual(state["snapshot"]["mode"], "test")
        self.assertIsNone(state["snapshot"]["usage"]["credits"])
        self.assertEqual(state["parse_error_count"], 0)
        self.assertEqual(len(state["decisions"]), 8)
        self.assertEqual(len(state["fills"]), 2)

    def test_summary_uses_signed_values_and_supplied_usage(self):
        self.write_snapshot(sample_snapshot())
        state = StateSource(self.snapshot_path).state()
        summary = state["summary"]
        self.assertEqual(summary["daily_pnl"], 50)
        self.assertEqual(summary["total_pnl"], 100)
        self.assertAlmostEqual(summary["daily_pnl_pct"], 50 / 1050)
        self.assertAlmostEqual(summary["drawdown_pct"], -100 / 1200)
        self.assertAlmostEqual(summary["net_beta"], 250 / 1100)
        self.assertAlmostEqual(summary["net_exposure_pct"], 150 / 1100)
        self.assertAlmostEqual(summary["gross_exposure_pct"], 250 / 1100)
        self.assertEqual(state["snapshot"]["usage"]["credits"], 4.25)
        self.assertFalse(state["connected"])

    def test_unknown_beta_is_not_zero(self):
        raw = sample_snapshot()
        raw["positions"][0].pop("beta")
        summary = summarize(clean_snapshot(raw, Issues()))
        self.assertEqual(summary["unknown_beta_count"], 1)
        self.assertIsNone(summary["net_beta"])
        self.assertIsNotNone(summary["gross_exposure_pct"])
        raw["positions"][0]["beta"] = 0
        self.assertIsNotNone(summarize(clean_snapshot(raw, Issues()))["net_beta"])

    def test_empty_positions_are_distinct_from_missing_or_invalid(self):
        raw = sample_snapshot()
        raw["positions"] = []
        self.assertEqual(summarize(clean_snapshot(raw, Issues()))["net_beta"], 0)
        del raw["positions"]
        self.assertIsNone(summarize(clean_snapshot(raw, Issues()))["net_beta"])
        raw["positions"] = [{"symbol": "TEST", "price": 100}]
        self.assertIsNone(summarize(clean_snapshot(raw, Issues()))["net_exposure_pct"])

    def test_denominators_and_overflows_remain_finite(self):
        for denominator in (0, -1, None):
            raw = sample_snapshot()
            for key in ("equity", "initial_equity", "previous_close_equity", "peak_equity"):
                raw[key] = denominator
            summary = summarize(clean_snapshot(raw, Issues()))
            for key in ("daily_pnl_pct", "total_return_pct", "drawdown_pct", "net_beta", "gross_exposure_pct"):
                self.assertIsNone(summary[key])
            json_bytes(summary)
        raw = sample_snapshot()
        raw["positions"] = [{"symbol": "TEST", "quantity": 1e308, "price": 1e308, "beta": 1e308}]
        summary = summarize(clean_snapshot(raw, Issues()))
        self.assertIsNone(summary["net_beta"])
        json_bytes(summary)

    def test_malformed_snapshot_never_substitutes_fixture(self):
        for data in ("", "{", "[]", '{"equity": NaN}', '{"unexpected": 1}'):
            with self.subTest(data=data):
                self.snapshot_path.write_text(data, encoding="utf-8")
                state = StateSource(self.snapshot_path).state()
                self.assertEqual(state["status"], "error")
                self.assertGreater(state["parse_error_count"], 0)
                self.assertIsNone(state["snapshot"])
                self.assertFalse(state["synthetic"])
        state = StateSource(self.root / "absent.json").state()
        self.assertEqual(state["status"], "error")

    def test_malformed_journal_rows_are_counted_and_good_rows_survive(self):
        self.write_snapshot(sample_snapshot())
        decisions = self.journal / "decisions.jsonl"
        fills = self.journal / "fills.jsonl"
        decisions.write_bytes(b'{"action":"HOLD","event_id":"test-1"}\nnot-json\n[]\n\n{"action":"BLOCK"}\n\xff\n{"action":')
        fills.write_text('{"symbol":"TEST","quantity":1,"price":10}\n{"symbol":"TEST","quantity":true,"price":10}\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in (self.snapshot_path, decisions, fills)}
        state = StateSource(self.snapshot_path).state()
        self.assertEqual(state["status"], "partial")
        self.assertEqual(len(state["decisions"]), 2)
        self.assertEqual(len(state["fills"]), 1)
        self.assertGreaterEqual(state["parse_error_count"], 5)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_only_configured_named_sources_are_opened_read_only(self):
        self.write_snapshot(sample_snapshot())
        source = StateSource(self.snapshot_path)
        original = Path.open
        opened = []
        def capture(path, mode="r", *args, **kwargs):
            self.assertIn(mode, ("r", "rb"))
            opened.append(path.name)
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, "open", capture):
            source.state()
        self.assertEqual(opened, ["status.json", "decisions.jsonl", "fills.jsonl"])

    def test_journal_retention_keeps_latest_valid_rows(self):
        self.write_snapshot(sample_snapshot())
        rows = [json.dumps({"event_id": str(index), "action": "HOLD"}) for index in range(MAX_ROWS + 5)]
        (self.journal / "decisions.jsonl").write_text("\n".join(rows), encoding="utf-8")
        state = StateSource(self.snapshot_path).state()
        self.assertEqual(len(state["decisions"]), MAX_ROWS)
        self.assertEqual(state["decisions"][0]["event_id"], "5")
        self.assertEqual(state["provenance"]["sources"]["decisions"]["valid_rows"], MAX_ROWS + 5)

    def test_source_modes_and_secret_paths_rejected_without_reading(self):
        with self.assertRaises(ValueError):
            StateSource(self.snapshot_path, fixture=True)
        with self.assertRaises(ValueError):
            StateSource(journal_dir=self.journal)
        for name in (".env", ".env.json", "credentials.json", "secret.json", "password.json", "status.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                StateSource(self.root / name)

    def test_explicit_journal_directory_is_used(self):
        self.write_snapshot(sample_snapshot())
        (self.root / "decisions.jsonl").write_text('{"action":"HOLD","event_id":"explicit"}\n', encoding="utf-8")
        (self.journal / "decisions.jsonl").write_text('{"action":"BLOCK","event_id":"wrong-source"}\n', encoding="utf-8")
        state = StateSource(self.snapshot_path, journal_dir=self.root).state()
        self.assertEqual([row["event_id"] for row in state["decisions"]], ["explicit"])

    def test_invalid_history_and_numeric_fields_report_issues(self):
        raw = sample_snapshot()
        raw["equity_history"] = [{"timestamp": "invalid", "equity": 1000}, {"timestamp": "2026-11-18T21:00:00Z", "equity": 1100}]
        raw["positions"][0]["beta"] = "unknown"
        self.write_snapshot(raw)
        state = StateSource(self.snapshot_path).state()
        self.assertEqual(state["parse_error_count"], 2)
        self.assertEqual(len(state["snapshot"]["equity_history"]), 1)
        self.assertIsNone(state["summary"]["net_beta"])
        self.assertEqual(state["status"], "partial")

    def test_bounded_source_reads_fail_visibly(self):
        self.write_snapshot(sample_snapshot())
        with patch("dashboard.server.MAX_FILE_BYTES", 10):
            state = StateSource(self.snapshot_path).state()
        self.assertEqual(state["status"], "error")
        (self.journal / "decisions.jsonl").write_text('{"action":"HOLD"}\n' + "x" * 100, encoding="utf-8")
        with patch("dashboard.server.MAX_LINE_BYTES", 20):
            state = StateSource(self.snapshot_path).state()
        self.assertEqual(state["status"], "partial")
        self.assertEqual(len(state["decisions"]), 1)
        self.assertIn("limit reached", state["errors"][0]["message"])

    def test_unexpected_snapshot_fields_not_exposed_and_json_escaped(self):
        raw = sample_snapshot()
        payload = '</script><img src=x onerror="alert(1)">&'
        raw["account_id"] = payload
        raw["unrelated_private_field"] = "must-not-leak"
        self.write_snapshot(raw)
        state = StateSource(self.snapshot_path).state()
        body = json_bytes(state)
        self.assertNotIn(b"<", body)
        self.assertNotIn(b">", body)
        self.assertNotIn(b"&", body)
        self.assertNotIn(b"must-not-leak", body)
        self.assertEqual(json.loads(body)["snapshot"]["account_id"], payload)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(StateSource(fixture=True), 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_loopback_binding_and_golden_routes(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for path in ("/", "/index.html", "/app.js", "/style.css", "/api/state"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertTrue(json.loads(self.request("GET", "/api/state")[2])["synthetic"])

    def test_route_allowlist_and_traversal(self):
        for path in ("/fixture.json", "/server.py", "/test_server.py", "/journal/decisions.jsonl", "/.env", "/../index.html", "/%2e%2e/.env", "/%2findex.html", "//index.html", "/api/state?path=.env", "/style.css?file=x", "/unknown", "/dashboard/"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_mutating_methods_denied(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/api/state")
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
                self.assertIn(b"Read-only", body)

    def test_no_cors_and_no_dns_rebinding(self):
        self.assertEqual(self.request("GET", "/api/state", {"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", {"Origin": "https://attacker.example"})[0], 403)
        status, headers, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_head_has_no_body(self):
        status, _, body = self.request("HEAD", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
