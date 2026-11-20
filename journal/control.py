import argparse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from journal.store import Journal
from live.contracts import ROOT
from rules.gate import ACCOUNTS


def main():
    parser = argparse.ArgumentParser(description="Local halt latch only; does not cancel orders or send cloud commands")
    parser.add_argument("action", choices=["halt", "status"])
    parser.add_argument("--mode", choices=list(ACCOUNTS), required=True)
    parser.add_argument("--reason", default="Owner local kill request")
    parser.add_argument("--journal", type=Path, default=ROOT / "journal")
    args = parser.parse_args()
    account = ACCOUNTS[args.mode]
    journal = Journal(args.journal)
    try:
        if args.action == "halt":
            journal.set_halt(account, args.reason)
            journal.append("decisions", {"event_id": uuid.uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": args.mode, "account_id": account, "run_id": None, "session_id": None,
                "agent": "owner-local-control", "kind": "KILL_SWITCH", "symbol": None,
                "action": "HALT", "reason": args.reason, "rule_checks": [], "target_checksum": None,
                "order_fingerprint": None, "config_checksum": None})
        print(journal.control(account))
        print("No broker action taken. Reconcile/cancel/flatten through an authorised paper-only session.")
    finally:
        journal.close()


if __name__ == "__main__":
    main()
