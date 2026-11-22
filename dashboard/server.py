"""Loopback-only, read-only dashboard. No broker, cloud, or execution dependencies.

Run from the project: python -m dashboard.server [--fixture | --snapshot PATH]
With --snapshot, journals default to PATH.parent / 'journal'. --journal-dir
can explicitly select another local journal directory. No source is read by default.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 128 * 1024
MAX_ROWS = 500
AGENT_NAMES = ("Coordinator", "Scout", "Executor", "Auditor", "Advisor")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def number(value):
    try:
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def finite(value):
    return value if number(value) else None


def ratio(numerator, denominator):
    if number(numerator) and number(denominator) and denominator > 0:
        return finite(numerator / denominator)
    return None


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def decode_json(data):
    return json.loads(data, parse_constant=reject_constant)


def safe_path(path, suffix=None):
    """Only explicit local data files; reject network paths and secret-like names."""
    path = Path(path).absolute()
    resolved = path.resolve()
    for candidate in (path, resolved):
        if str(candidate).startswith(("\\\\", "//")):
            raise ValueError("Network paths are not permitted")
        for part in candidate.parts:
            lower = part.lower()
            if lower == ".env" or lower.startswith(".env.") or any(
                word in lower for word in ("secret", "credential", "password", "token")
            ):
                raise ValueError("Secret-like source paths are not permitted")
    if suffix and (path.suffix.lower() != suffix or resolved.suffix.lower() != suffix):
        raise ValueError("Source must be a local " + suffix + " file")
    return resolved


class Issues:
    def __init__(self):
        self.count = 0
        self.items = []

    def add(self, source, message, line=None):
        self.count += 1
        if len(self.items) < 12:
            self.items.append({"source": source, "message": message, "line": line})


def read_json(path, issues, source):
    try:
        path = safe_path(path, ".json")
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Source exceeds 4 MiB limit")
        result = decode_json(data)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result
    except (OSError, ValueError, UnicodeError, RecursionError):
        issues.add(source, "Missing, unreadable, oversized, or malformed JSON source")
        return None


TEXT_FIELDS = {
    "positions": ("symbol",),
    "equity_history": ("timestamp",),
    "orders": ("order_id", "symbol", "side", "status", "type", "timestamp"),
    "agents": ("name", "status", "session_id"),
    "decisions": ("event_id", "timestamp", "mode", "account_id", "run_id", "session_id",
                  "agent", "kind", "symbol", "action", "reason", "target_checksum",
                  "order_fingerprint", "config_checksum"),
    "fills": ("order_id", "fill_id", "timestamp", "symbol", "side"),
}
NUMBER_FIELDS = {
    "positions": ("quantity", "price", "beta"),
    "equity_history": ("equity",),
    "orders": ("quantity", "price", "filled_quantity"),
    "agents": (),
    "decisions": (),
    "fills": ("quantity", "price", "commission", "platform_fee", "regulatory_fee",
              "slippage_bps", "arrival_price"),
}


def clean_row(row, kind, issues, line=None):
    if not isinstance(row, dict):
        issues.add(kind, "Skipped non-object row", line)
        return None
    required = {
        "positions": ("symbol", "quantity", "price"),
        "equity_history": ("timestamp", "equity"),
        "orders": ("symbol",), "agents": ("name",),
        "decisions": (), "fills": ("symbol", "quantity", "price"),
    }[kind]
    if any(row.get(key) is None for key in required) or (
        kind == "decisions" and not any(row.get(key) for key in ("kind", "action", "reason"))
    ):
        issues.add(kind, "Skipped row missing required fields", line)
        return None
    clean = {}
    for key in TEXT_FIELDS[kind]:
        value = row.get(key)
        if key in ("account_id", "order_id", "fill_id") and number(value):
            value = str(value)
        if value is not None and not isinstance(value, str):
            issues.add(kind, "Invalid text field: " + key, line)
            value = None
        clean[key] = value[:4000] if isinstance(value, str) else None
    for key in NUMBER_FIELDS[kind]:
        value = row.get(key)
        if value is not None and not number(value):
            issues.add(kind, "Invalid numeric field: " + key, line)
        clean[key] = finite(value)
    if any(clean.get(key) is None or clean.get(key) == "" for key in required):
        issues.add(kind, "Skipped row with invalid required fields", line)
        return None
    if kind == "equity_history":
        try:
            datetime.fromisoformat(clean["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            issues.add(kind, "Skipped invalid ISO timestamp", line)
            return None
    if kind in ("positions", "fills") and clean["price"] < 0:
        issues.add(kind, "Skipped negative price", line)
        return None
    if kind == "decisions":
        checks = row.get("rule_checks")
        # Serialize bounded structured checks as inert text, never executable markup.
        try:
            clean["rule_checks"] = json.dumps(checks, ensure_ascii=True, allow_nan=False)[:8000] if checks is not None else None
        except (ValueError, OverflowError, RecursionError):
            issues.add(kind, "Invalid rule checks", line)
            clean["rule_checks"] = None
    return clean


def clean_list(value, kind, issues):
    if value is None:
        return None
    if not isinstance(value, list):
        issues.add(kind, "Expected an array")
        return None
    result = []
    if len(value) > 10000:
        issues.add(kind, "Array exceeds 10,000 row limit; not used")
        return None
    for index, row in enumerate(value, 1):
        clean = clean_row(row, kind, issues, index)
        if clean is not None:
            result.append(clean)
    return result


def read_journal(path, kind, issues):
    rows = deque(maxlen=MAX_ROWS)
    valid_count = 0
    try:
        path = safe_path(path, ".jsonl")
        with path.open("rb") as stream:
            total = 0
            line_number = 0
            while True:
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                line_number += 1
                total += len(raw)
                if total > MAX_JOURNAL_BYTES or len(raw) > MAX_LINE_BYTES:
                    issues.add(kind, "Journal read limit reached; later rows not loaded", line_number)
                    break
                if not raw.strip():
                    continue
                try:
                    row = clean_row(decode_json(raw), kind, issues, line_number)
                    if row is not None:
                        rows.append(row)
                        valid_count += 1
                except (ValueError, UnicodeError, RecursionError):
                    issues.add(kind, "Skipped malformed JSON row", line_number)
        return list(rows), {"status": "read", "valid_rows": valid_count, "shown_rows": len(rows)}
    except FileNotFoundError:
        return [], {"status": "not found", "valid_rows": 0, "shown_rows": 0}
    except (OSError, ValueError):
        issues.add(kind, "Journal unavailable or disallowed")
        return [], {"status": "unavailable", "valid_rows": 0, "shown_rows": 0}


def clean_snapshot(raw, issues):
    if raw is None:
        return None
    if not any(key in raw for key in ("as_of", "equity", "positions", "mode", "account_id")):
        issues.add("snapshot", "Snapshot has no recognized status fields")
        return None
    result = {}
    for key in ("as_of", "mode", "account_id", "account_type"):
        value = raw.get(key)
        if key == "account_id" and number(value):
            value = str(value)
        if value is not None and not isinstance(value, str):
            issues.add("snapshot", "Invalid text field: " + key)
            value = None
        result[key] = value[:4000] if isinstance(value, str) else None
    for key in ("equity", "initial_equity", "previous_close_equity", "peak_equity"):
        value = raw.get(key)
        if value is not None and not number(value):
            issues.add("snapshot", "Invalid numeric field: " + key)
        result[key] = finite(value)
    for key in ("positions", "equity_history", "orders", "agents"):
        result[key] = clean_list(raw.get(key), key, issues)
    result["positions_complete"] = isinstance(raw.get("positions"), list) and result["positions"] is not None and len(result["positions"]) == len(raw["positions"])
    returns = raw.get("recent_daily_returns")
    if returns is not None and (not isinstance(returns, list) or not all(number(value) for value in returns)):
        issues.add("snapshot", "Invalid recent daily returns; not used")
        returns = None
    result["recent_daily_returns"] = returns
    usage = raw.get("usage")
    result["usage"] = {"credits": None, "as_of": None}
    if usage is not None:
        if not isinstance(usage, dict):
            issues.add("usage", "Expected an object")
        else:
            credits = usage.get("credits")
            if credits is not None and (not number(credits) or credits < 0):
                issues.add("usage", "Invalid supplied credits")
                credits = None
            result["usage"] = {"credits": credits, "as_of": usage.get("as_of") if isinstance(usage.get("as_of"), str) else None}
    return result


def summarize(snapshot):
    if snapshot is None:
        return {key: None for key in ("equity", "daily_pnl", "daily_pnl_pct", "total_pnl", "total_return_pct", "drawdown_pct", "net_beta", "net_exposure_pct", "gross_exposure_pct", "unknown_beta_count")}
    equity = snapshot["equity"]
    def difference(other):
        return finite(equity - other) if number(equity) and number(other) else None
    daily = difference(snapshot["previous_close_equity"])
    total = difference(snapshot["initial_equity"])
    drawdown = ratio(difference(snapshot["peak_equity"]), snapshot["peak_equity"])
    positions = snapshot["positions"]
    known = snapshot["positions_complete"]
    values = [finite(row["quantity"] * row["price"]) for row in positions] if known else []
    known = known and all(number(value) for value in values)
    unknown = sum(row["beta"] is None and row["quantity"] != 0 for row in positions) if known else None
    net = finite(sum(values)) if known else None
    gross = finite(sum(abs(value) for value in values)) if known else None
    weighted = finite(sum(value * (row["beta"] or 0) for value, row in zip(values, positions))) if known and unknown == 0 else None
    return {
        "equity": equity, "daily_pnl": daily, "daily_pnl_pct": ratio(daily, snapshot["previous_close_equity"]),
        "total_pnl": total, "total_return_pct": ratio(total, snapshot["initial_equity"]),
        "drawdown_pct": min(0, drawdown) if drawdown is not None else None,
        "net_beta": ratio(weighted, equity), "net_exposure_pct": ratio(net, equity),
        "gross_exposure_pct": ratio(gross, equity), "unknown_beta_count": unknown,
    }


class StateSource:
    def __init__(self, snapshot=None, fixture=False, journal_dir=None):
        if snapshot and fixture:
            raise ValueError("Choose snapshot or fixture, not both")
        if journal_dir and not snapshot:
            raise ValueError("--journal-dir requires --snapshot")
        self.fixture = fixture
        self.snapshot = safe_path(snapshot, ".json") if snapshot else None
        self.journal_dir = safe_path(journal_dir) if journal_dir else (self.snapshot.parent / "journal" if self.snapshot else None)

    def state(self):
        issues = Issues()
        raw = None
        decisions, fills = [], []
        sources = {}
        if self.fixture:
            bundle = read_json(BASE / "fixture.json", issues, "fixture")
            if bundle:
                raw = bundle.get("snapshot")
                if not isinstance(raw, dict):
                    issues.add("fixture", "Missing snapshot object")
                    raw = None
                decisions = (clean_list(bundle.get("decisions"), "decisions", issues) or [])[-MAX_ROWS:]
                fills = (clean_list(bundle.get("fills"), "fills", issues) or [])[-MAX_ROWS:]
            sources = {"snapshot": "fixture.json (synthetic)", "decisions": {"status": "fixture", "shown_rows": len(decisions)}, "fills": {"status": "fixture", "shown_rows": len(fills)}}
        elif self.snapshot:
            raw = read_json(self.snapshot, issues, "snapshot")
            sources["snapshot"] = self.snapshot.name
            decisions, sources["decisions"] = read_journal(self.journal_dir / "decisions.jsonl", "decisions", issues)
            fills, sources["fills"] = read_journal(self.journal_dir / "fills.jsonl", "fills", issues)
        snapshot = clean_snapshot(raw, issues)
        # Demo usage is never presented as actual Qoder billing/credit consumption.
        if self.fixture and snapshot:
            snapshot["usage"] = {"credits": None, "as_of": None}
        status = "partial" if issues.count and snapshot else "error" if issues.count else "ready" if snapshot else "empty"
        return {
            "schema_version": 1, "source_mode": "fixture" if self.fixture else "snapshot" if self.snapshot else "none",
            "synthetic": self.fixture, "connected": False, "execution_enabled": False,
            "status": status, "read_at": datetime.now(timezone.utc).isoformat(),
            "snapshot": snapshot, "summary": summarize(snapshot), "decisions": decisions, "fills": fills,
            "parse_error_count": issues.count, "errors": issues.items,
            "provenance": {"sources": sources, "journal_row_limit": MAX_ROWS, "note": "Local files only. No account connection or execution. Research does not imply forward returns."},
        }


def json_bytes(value):
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalDashboard/1.0"
    sys_version = ""

    def __init__(self, *args, source, assets, **kwargs):
        self.source = source
        self.assets = assets
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        # No persistent access logs and no untrusted request strings on the terminal.
        pass

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        # BaseHTTPRequestHandler normalizes leading //; require the literal route.
        if self.requestline.split()[1] != self.path:
            self.respond(404, json_bytes({"error": "Route not found"}))
            return
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in hosts:
            self.respond(403, json_bytes({"error": "Loopback host required"}))
            return
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            self.respond(403, json_bytes({"error": "Cross-origin access denied"}))
            return
        if self.path == "/api/state":
            self.respond(200, json_bytes(self.source.state()))
        elif self.path in self.assets:
            data, content_type = self.assets[self.path]
            self.respond(200, data, content_type)
        else:
            self.respond(404, json_bytes({"error": "Route not found"}))

    do_HEAD = do_GET

    def deny_method(self):
        self.close_connection = True
        self.respond(405, json_bytes({"error": "Read-only dashboard; execution disabled"}))

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = deny_method


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def make_server(source, port=8766):
    assets = {route: ((BASE / filename).read_bytes(), mime) for route, (filename, mime) in STATIC.items()}
    return LocalServer(("127.0.0.1", port), partial(Handler, source=source, assets=assets))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fixture", action="store_true", help="Opt in to visibly synthetic bundled demo data")
    mode.add_argument("--snapshot", type=Path, help="Explicit local JSON status file (no account connection)")
    parser.add_argument("--journal-dir", type=Path, help="Local directory containing only decisions.jsonl and fills.jsonl to read")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        source = StateSource(args.snapshot, args.fixture, args.journal_dir)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    server = make_server(source, args.port)
    print(f"Local read-only dashboard: http://127.0.0.1:{args.port}/ | PID {os.getpid()} | "
          + ("SIMULATED DEMO — no connected account" if args.fixture else "local snapshot" if args.snapshot else "Not connected / no data"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
