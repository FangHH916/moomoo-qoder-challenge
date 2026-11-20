from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from live.contracts import canonical_bytes, digest, finite
from rules.gate import ACCOUNTS, evaluate_order
from rules.trading_calendar import ET
from live.contracts import timestamp

SECRET_KEYS = re.compile(r"(secret|token|password|api.?key|authorization)", re.I)


def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEYS.search(key):
                raise ValueError("Credentials are not permitted in journals")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str) and re.search(r"(?:xox[baprs]-|xapp-|Bearer\s+|sk-[A-Za-z0-9]{16,}|pt-[A-Za-z0-9]{16,})", value):
        raise ValueError("Possible credential in journal value")


class Journal:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        database = self.folder / "state.sqlite"
        if not database.exists() and any((self.folder / f"{stream}.jsonl").exists() and
                                       (self.folder / f"{stream}.jsonl").stat().st_size for stream in ("decisions", "fills")):
            raise ValueError("Existing journal requires explicit recovery/import; refusing to overwrite it")
        self.db = sqlite3.connect(database, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, stream TEXT NOT NULL, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS controls (account TEXT PRIMARY KEY, halted INTEGER NOT NULL, reason TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS daily_halts (account TEXT, day TEXT, PRIMARY KEY(account,day))")
        self.db.execute("CREATE TABLE IF NOT EXISTS leases (account TEXT PRIMARY KEY, owner TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, consumed INTEGER NOT NULL)")

    def close(self):
        self.db.close()

    def append(self, stream: str, record: dict) -> bool:
        if stream not in ("decisions", "fills"):
            raise ValueError("Unknown journal stream")
        reject_secrets(record)
        mode, account = record["mode"], str(record["account_id"])
        if mode not in ACCOUNTS:
            raise ValueError("Invalid journal mode")
        if stream == "fills":
            if account != ACCOUNTS[mode]:
                raise ValueError("Reject unexpected fill account")
            if not all(finite(record[k]) and record[k] > 0 for k in ("quantity", "price")):
                raise ValueError("Invalid fill quantity/price")
            key = digest([account, record["order_id"], record["fill_id"]])
        else:
            key = record["event_id"]
        body = canonical_bytes(record).decode()
        previous = self.db.execute("SELECT body FROM events WHERE id=?", (key,)).fetchone()
        if previous:
            if previous[0] != body:
                raise ValueError("Conflicting duplicate journal identity")
            self.export(stream)
            return False
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (key, stream, body))
        self.export(stream)
        return True

    def export(self, stream: str):
        target = self.folder / f"{stream}.jsonl"
        temporary = self.folder / f".{stream}.{uuid.uuid4().hex}.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for (body,) in self.db.execute("SELECT body FROM events WHERE stream=? ORDER BY rowid", (stream,)):
                handle.write(body + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # The database commit above is the durability guarantee; the jsonl is a derived view. On Windows
        # a scanner can hold the destination for a moment after a write, so retry the replace rather than
        # raise over an event that is already stored.
        for _ in range(40):
            try:
                temporary.replace(target)
                return
            except PermissionError:
                time.sleep(0.05)
        raise PermissionError(f"journal export could not replace {target}")

    def set_halt(self, account: str, reason: str):
        if account not in ACCOUNTS.values():
            raise ValueError("Invalid control account")
        self.db.execute("INSERT INTO controls VALUES (?,1,?) ON CONFLICT(account) DO UPDATE SET halted=1,reason=excluded.reason", (account, reason))

    def set_resume(self, account: str, reason: str):
        """Arm the account: the only way a row with halted=0 is ever written, so R16 starts from evidence."""
        if account not in ACCOUNTS.values():
            raise ValueError("Invalid control account")
        self.db.execute("INSERT INTO controls VALUES (?,0,?) ON CONFLICT(account) DO UPDATE SET halted=0,reason=excluded.reason", (account, reason))

    def control(self, account: str) -> dict:
        row = self.db.execute("SELECT halted,reason FROM controls WHERE account=?", (account,)).fetchone()
        return {"halted": bool(row[0]) if row else True, "reason": row[1] if row else "Not armed", "verified_by_relay": True}

    def acquire(self, account: str, owner: str) -> bool:
        if account not in ACCOUNTS.values():
            raise ValueError("Unknown lease account")
        try:
            self.db.execute("INSERT INTO leases VALUES (?,?)", (account, owner))
            return True
        except sqlite3.IntegrityError:
            return False

    def release(self, account: str, owner: str):
        self.db.execute("DELETE FROM leases WHERE account=? AND owner=?", (account, owner))

    def consume_approval(self, approval_id: str, fingerprint: str) -> bool:
        try:
            self.db.execute("INSERT INTO approvals VALUES (?,?,1)", (approval_id, fingerprint))
            return True
        except sqlite3.IntegrityError:
            return False

    def evaluate_and_record(self, order: dict, context: dict, config: dict) -> dict:
        account = str(context["account_id"])
        day = timestamp(context["now"]).astimezone(ET).date().isoformat()
        day_halt = self.db.execute("SELECT 1 FROM daily_halts WHERE account=? AND day=?", (account, day)).fetchone() is not None
        context = {**context, "kill_switch": self.control(account), "daily_entry_halt": day_halt}
        result = evaluate_order(order, context, config)
        day_loss = next((r["observed"] for r in result["checks"] if r["id"] == "R8"), None)
        if finite(config.get("daily_loss_limit")) and finite(day_loss) and day_loss <= config["daily_loss_limit"] + 1e-12:
            self.db.execute("INSERT OR IGNORE INTO daily_halts VALUES (?,?)", (account, day))
        dd = next((r["observed"] for r in result["checks"] if r["id"] == "R9"), None)
        if finite(dd) and dd <= config["drawdown_limit"] + 1e-12 and account in ACCOUNTS.values():
            self.set_halt(account, "R9 peak drawdown")
        self.append("decisions", {
            "event_id": uuid.uuid4().hex, "timestamp": context["now"], "mode": context["mode"],
            "account_id": account, "run_id": context.get("run_id"), "session_id": context.get("guard_session_id"),
            "agent": "deterministic-gate", "kind": "ORDER_CHECK", "symbol": order.get("symbol"),
            "strategy_id": context.get("targets", {}).get("strategy_id"), "intent": order.get("intent"),
            "action": result["action"], "reason": ",".join(result["failed_rules"]) or "All checks passed; execution inactive",
            "rule_checks": result["checks"], "target_checksum": context.get("targets", {}).get("checksum"),
            "order_fingerprint": result["order_fingerprint"], "config_checksum": digest(config)})
        return result


def reconcile_order(previous: dict | None, current: dict) -> dict:
    previous = previous or {"cumulative_quantity": 0}
    qty, filled = current["quantity"], current["cumulative_quantity"]
    if not isinstance(qty, int) or not isinstance(filled, int) or not 0 <= filled <= qty:
        raise ValueError("Invalid cumulative fill quantity")
    if filled < previous["cumulative_quantity"]:
        raise ValueError("Cumulative quantity regressed; manual reconciliation required")
    delta = filled - previous["cumulative_quantity"]
    if current["status"] == "FILLED" and filled != qty:
        raise ValueError("Filled status does not match quantity")
    if current["status"] == "CANCELLED" and filled:
        status = "CANCELLED_PARTIAL"
    elif 0 < filled < qty:
        status = "PARTIAL"
    else:
        status = current["status"]
    return {"order_id": current["order_id"], "status": status, "new_filled_quantity": delta,
            "remaining_quantity": qty - filled, "requires_fill_details": delta > 0,
            "note": "Cumulative order status is not an individual fill record."}
