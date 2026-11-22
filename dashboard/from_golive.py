"""Build a dashboard snapshot from the files `live/golive.py` already saved.

Read-only and offline: it reads reports/live/golive/<day>/<account>/ (snapshot-*.json,
positions-*.json, orders-*-today.json) and agents/release-lock.json, and writes one JSON
file that `python -m dashboard.server --snapshot <file> --journal-dir journal` serves.
No broker, cloud or network call is made.

    python -m dashboard.from_golive --account 5832812 --out .cache/dashboard/snapshot.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLIVE = ROOT / "reports" / "live" / "golive"
SIDE = {1: "BUY", 2: "SELL"}
STATUS = {1: "WORKING", 2: "WORKING", 3: "PARTIAL", 4: "FILLED", 5: "CANCELLED", 6: "REJECTED", 7: "CANCELLED_PARTIAL"}
ORDER_TYPE = {1: "LIMIT", 2: "LIMIT", 3: "MARKET"}


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def latest(folder, pattern):
    files = sorted(folder.glob(pattern), key=lambda p: (p.stat().st_mtime, p.name))
    return files[-1] if files else None


def day_dirs(account):
    return sorted(d / account for d in GOLIVE.iterdir() if d.is_dir() and d.name[:4].isdigit() and (d / account).is_dir())


def day_close(folder):
    snap = latest(folder, "snapshot-*.json")
    if not snap:
        return None
    data = load(snap)
    equity = num(data.get("equity"))
    return (data.get("as_of"), equity, data) if equity is not None else None


def positions(folder):
    path = latest(folder, "positions*.json")
    if not path:
        return None
    rows = []
    for row in (load(path).get("data") or {}).get("positions") or []:
        qty, price = num(row.get("qty")), num(row.get("cur_price"))
        if qty is None or price is None:
            continue
        if row.get("pstn_type") == 1 and qty > 0:  # a short reported with a positive quantity
            qty = -qty
        rows.append({"symbol": str(row.get("symbol")), "quantity": qty, "price": price, "beta": None})
    return rows


def orders(folder):
    path = latest(folder, "orders-*-today.json")
    if not path:
        return None
    rows = []
    for row in (load(path).get("data") or {}).get("orders") or []:
        micros = num(row.get("create_time"))
        stamp = None
        if micros:
            from datetime import datetime, timezone
            stamp = datetime.fromtimestamp(micros / 1e6, tz=timezone.utc).isoformat(timespec="seconds")
        rows.append({
            "order_id": str(row.get("order_id")), "timestamp": stamp, "symbol": str(row.get("symbol")),
            "side": SIDE.get(row.get("side"), str(row.get("side"))),
            "quantity": num(row.get("qty")), "filled_quantity": num(row.get("cum_qty")),
            "price": num(row.get("avg_fill_price")) or num(row.get("price")),
            "type": ORDER_TYPE.get(row.get("order_type"), str(row.get("order_type"))),
            "status": STATUS.get(row.get("status"), "UNKNOWN"),
        })
    return rows


def agents():
    lock_path = ROOT / "agents" / "release-lock.json"
    lock = load(lock_path) if lock_path.exists() else {}
    activation = str(lock.get("activation", "unknown"))
    verified = str(lock.get("cloud_verification_status", "unknown"))
    cloud = f"Cloud · activation {activation} · {verified}"
    return [
        {"name": "Coordinator", "status": cloud, "session_id": None},
        {"name": "Scout", "status": cloud, "session_id": None},
        {"name": "Executor", "status": cloud + " · only role with order tools", "session_id": None},
        {"name": "Auditor", "status": cloud + " · pstar-risk-check", "session_id": None},
        {"name": "Advisor", "status": "not deployed", "session_id": None},
    ]


def build(account):
    days = day_dirs(account)
    if not days:
        raise SystemExit(f"no golive folders for account {account} under {GOLIVE}")
    closes = [c for c in (day_close(d) for d in days) if c]
    if not closes:
        raise SystemExit("no snapshot-*.json with an equity figure")
    as_of, equity, last = closes[-1]
    initial = num(last.get("initial_equity")) or 100000.0
    history = [{"timestamp": a, "equity": e} for a, e, _ in closes]
    peak_file = GOLIVE / "state" / "risk_peak_equity.json"
    peak = max([initial] + [e for _, e, _ in closes])
    if peak_file.exists():
        stored = num(load(peak_file).get("peak_equity"))
        if stored and str(load(peak_file).get("account_id")) == account:
            peak = max(peak, stored)
    trading = [d for d in days if latest(d, "positions*.json")]
    newest = trading[-1] if trading else days[-1]
    return {
        "as_of": as_of, "mode": "competition" if account == "5832812" else "test",
        "account_id": account, "account_type": str(last.get("account_type") or "SIMULATE"),
        "equity": equity, "initial_equity": initial,
        "previous_close_equity": closes[-2][1] if len(closes) > 1 else initial,
        "peak_equity": peak, "positions": positions(newest), "orders": orders(newest),
        "equity_history": history, "agents": agents(), "usage": {"credits": None, "as_of": None},
        "source": f"live/golive.py reads under reports/live/golive/*/{account}/ (newest day {newest.parent.name})",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", default="5832812", choices=["5832812", "5322438"])
    parser.add_argument("--out", type=Path, default=ROOT / ".cache" / "dashboard" / "snapshot.json")
    args = parser.parse_args()
    snapshot = build(args.account)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"wrote {args.out}: equity {snapshot['equity']}, {len(snapshot['positions'] or [])} positions, "
          f"{len(snapshot['orders'] or [])} orders, as of {snapshot['as_of']}")


if __name__ == "__main__":
    main()
