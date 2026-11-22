#!/usr/bin/env python3
"""P* risk check: one report from three raw moomoo reads, standard library only.

Run by the Risk Auditor on every scheduled check of account 5832812. It reads the raw MCP response files
the agent saved (`sim_trade_cash_info`, `sim_trade_position_list`, `sim_trade_history_order_list`, plus the
sealed target book for the allowed-symbol list), computes the risk level with the frozen arithmetic, and
writes two artifacts and nothing else:

* `risk_report.json` - schema `pstar-risk/1`, the file `golive.py plan --risk-action` consumes as a
  trigger. The report never sizes an order: the plan re-measures the book on its own fresh snapshot;
* the Slack text - the same numbers in the handful of lines that get posted.

The thresholds come from `live/config.pstar.json` (frozen rules) and `live/ops.json` (the WARN layer and
the anomaly limits); the arithmetic mirrors `live/risk_monitor.py` and `rules/gate_pstar.py` on purpose, and
`live/test_risk_check.py` proves the two agree on shared fixtures. It places, cancels and modifies nothing,
and it never calls a broker: every input is a file an agent already read.

Exit codes: 0 nothing to post, 1 something must be posted (WARN/ALERT/KILL/order anomaly), 2 a read failed
and the report says CHECK FAILED.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "pstar-risk/1"
LEVELS = ("OK", "WARN", "ALERT", "KILL")
REPO = Path(__file__).resolve().parents[3]      # skills/pstar-risk-check/scripts -> the repository root
ET = ZoneInfo("America/New_York")
CST = timezone(timedelta(hours=-5))
# moomoo's simulated-trading status codes, as recorded in prompts/09: 1/2 working, 3 partial, 4 filled,
# 5 cancelled without a fill, 6 rejected, 7 partially filled then cancelled. Only 4 and 5 are observed.
LIVE_CODES = (1, 2)
REJECTED_CODES = (6,)
PARTIAL_CODES = (3, 7)
FILLED_CODES = (4,)
SIDE_NAMES = {1: "BUY", 2: "SELL"}
TOOL_BY_FILE = {"cash": "sim_trade_cash_info", "positions": "sim_trade_position_list",
                "orders": "sim_trade_history_order_list", "accounts": "sim_trade_account_list"}
# A peak above ten times the starting assets is a typo, not a high-water mark: the drawdown rule is a
# 8% rule, so no real book in this competition needs that much headroom to be judged honestly.
PEAK_SANITY_MULTIPLE = 10.0
MAX_PEAK_HISTORY = 60


class CheckError(Exception):
    """A read cannot be trusted; the run becomes a CHECK FAILED report rather than a silent exit."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value or "").strip().replace("Z", "+00:00")
    if text.isdigit():
        return datetime.fromtimestamp(float(text), timezone.utc)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CheckError(f"not a timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def bare(symbol) -> str:
    return str(symbol or "").removeprefix("US.").strip().upper()


def read_json(path, what: str):
    if not path:
        raise CheckError(f"no {what} file was given")
    file = Path(path)
    if not file.is_file():
        raise CheckError(f"the {what} file {file} does not exist")
    try:
        return json.loads(file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CheckError(f"the {what} file {file} is not readable JSON: {exc}") from exc


def unwrap(raw, path, tool: str) -> dict:
    """One MCP envelope -> its data block, refusing a response that says it did not succeed."""
    if not isinstance(raw, dict):
        raise CheckError(f"{path}: the {tool} response is not an object")
    code = raw.get("ret_code")
    if code is not None and str(code) != "0":
        raise CheckError(f"{path}: {tool} failed with ret_code={code} ret_msg={raw.get('ret_msg')!r}")
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        raise CheckError(f"{path}: the {tool} response has no data block")
    return data


def account_found(payload: dict) -> set[str]:
    """Every account id the raw response itself names, so a file from the other book is refused."""
    keys = ("account_id", "acc_id", "acct_id", "account")
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and str(item).isdigit():
                    found.add(str(item))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return found


# ------------------------------------------------------------------ the three reads
def read_positions(data: dict) -> list[dict]:
    rows = []
    for raw in data.get("positions") or ():
        if not isinstance(raw, dict):
            raise CheckError(f"position row is not an object: {raw!r}")
        symbol = raw.get("symbol") or raw.get("code") or raw.get("ticker")
        name = bare(symbol)
        if not name:
            raise CheckError(f"position row has no symbol: {sorted(raw)}")
        quantity = number(raw.get("qty", raw.get("quantity")))
        if quantity is None or quantity != int(quantity):
            raise CheckError(f"{name}: quantity {raw.get('qty', raw.get('quantity'))!r} is missing or fractional")
        quantity = int(quantity)
        direction = raw.get("direction")
        if isinstance(direction, (int, float)) and not isinstance(direction, bool):
            quantity = -abs(quantity) if int(direction) in (2, 3) else abs(quantity)
        elif isinstance(direction, str) and direction.strip().lower() in ("short", "sell", "-1"):
            quantity = -abs(quantity)
        value = number(raw.get("mv", raw.get("market_value")))
        price = number(raw.get("price", raw.get("last_price") or raw.get("cur_price")))
        if price is None and value is not None and quantity:
            price = abs(value) / abs(quantity)
        if price is None or price <= 0:
            raise CheckError(f"{name}: no usable price in the position row, so the book cannot be measured")
        if value is None:
            value = quantity * price
        rows.append({"symbol": name, "quantity": quantity, "price": price, "value": value})
    if len({row["symbol"] for row in rows}) != len(rows):
        raise CheckError("the position list names one symbol twice")
    return rows


def read_cash(data: dict) -> dict:
    equity = number(data.get("total_asset"))
    if equity is None or equity <= 0:
        raise CheckError(f"cash info gave no usable total_asset: {data.get('total_asset')!r}")
    long_gross = number(data.get("long_mv"))
    short_gross = number(data.get("short_mv"))
    return {"equity": equity, "cash": number(data.get("balance")), "long_gross": long_gross,
            "short_gross": short_gross}


def read_orders(data: dict) -> list[dict]:
    rows = []
    for raw in data.get("orders") or ():
        if not isinstance(raw, dict):
            raise CheckError(f"order row is not an object: {raw!r}")
        quantity, filled = number(raw.get("qty")), number(raw.get("cum_qty"))
        code = raw.get("status")
        rows.append({"order_id": str(raw.get("order_id") or ""), "symbol": bare(raw.get("symbol")),
                     "side": SIDE_NAMES.get(raw.get("side"), str(raw.get("side") or "").upper()),
                     "quantity": int(quantity) if quantity is not None else None,
                     "filled": int(filled) if filled is not None else None,
                     "status": int(code) if isinstance(code, (int, float)) and not isinstance(code, bool) else code,
                     "client_order_id": str(raw.get("text") or ""),
                     "created_at": timestamp(raw["create_time"]) if raw.get("create_time") else None})
    return rows


# ------------------------------------------------------------------ the peak that survives between sessions
def peak_state(config: dict, path: Path) -> dict:
    stored = read_json(path, "peak file") if path.is_file() else {"sessions": []}
    if not isinstance(stored, dict) or not isinstance(stored.get("sessions"), list):
        raise CheckError(f"{path}: the peak file is not a pstar-risk-check state object")
    return stored


def resolve_peak(args, config: dict, equity: float, now: datetime) -> dict:
    """The high-water mark, and an honest statement of where it came from.

    Preference order, as the user decided on 2026-11-21: a peak file this script keeps (it survives between
    cloud sessions when the workspace does), then the highest equity in a previous end-of-day report, then
    the initial assets. The basis is written into every report so a Slack reader can see which one was used.
    """
    initial = number(config.get("initial_equity")) or 100_000.0
    history: list[dict] = []
    basis = None
    path = Path(args.peak_file) if str(args.peak_file or "").strip() else None
    if path is not None:
        try:
            stored = peak_state(config, path)
        except (CheckError, OSError) as exc:
            stored = None
            basis = f"the peak file {path} is unreadable ({exc})"
        if stored is not None:
            today = now.astimezone(ET).date().isoformat()
            rows = [row for row in stored["sessions"] if row.get("day") != today and number(row.get("equity"))]
            rows.append({"day": today, "as_of": iso(now), "equity": equity})
            rows = rows[-MAX_PEAK_HISTORY:]
            history = rows
            peak = round(max([equity, initial, *(number(row["equity"]) for row in rows)]), 2)
            if args.write_peak:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"account_id": args.account, "initial_equity": initial,
                                                "peak_equity": peak, "sessions": rows,
                                                "written_by": "skills/pstar-risk-check/scripts/risk_check.py",
                                                "note": "the paper cash API reports no peak; this file is it"},
                                               indent=2, sort_keys=True) + "\n", encoding="utf-8")
                except OSError as exc:
                    basis = f"the peak file {path} is not writable ({exc})"
            if basis is None:
                basis = (f"running peak from {path}, day-keyed and kept between sessions "
                         f"(the paper cash API reports no peak)")
                return {"peak_equity": peak, "peak_basis": basis, "peak_history": rows,
                        "previous_close_equity": rows[-2]["equity"] if len(rows) > 1 else equity}
    eod = end_of_day_equities(args.eod_dir, args.account, now)
    if eod:
        peak = round(max([equity, initial, *eod["equities"]]), 2)
        history = [{"day": row["day"], "as_of": row["as_of"], "equity": row["equity"]} for row in eod["rows"]]
        return {"peak_equity": peak,
                "peak_basis": (f"{basis + '; ' if basis else ''}fallback: the previous end-of-day reports under "
                               f"{args.eod_dir} (newest {eod['rows'][-1]['day']}), no peak file"),
                "peak_history": history, "previous_close_equity": eod["equities"][-1]}
    return {"peak_equity": round(max(equity, initial), 2),
            "peak_basis": (f"{basis + '; ' if basis else ''}fallback: max(initial assets "
                           f"{initial:,.0f}, today's equity {equity:,.2f}); no peak file and no previous "
                           f"end-of-day report was readable"),
            "peak_history": [], "previous_close_equity": None}


def report_day(path: Path, payload: dict) -> str | None:
    """The ET day a saved report belongs to, which is what decides whether it is a *previous* close.

    Reports land under `reports/live/<run>/<day>/<account>/`, so the date is usually a directory name rather
    than a field. A file we cannot date is skipped: an undated equity number is not evidence of a peak.
    """
    for key in ("day", "session", "session_date", "trade_date"):
        value = str(payload.get(key) or "")
        if len(value) == 10 and value[4] == value[7] == "-" and f"{value[:4]}{value[5:7]}{value[8:]}".isdigit():
            return value
    for parent in reversed(path.parents):
        value = parent.name
        if len(value) == 10 and value[4] == value[7] == "-" and f"{value[:4]}{value[5:7]}{value[8:]}".isdigit():
            return value
    return None


def end_of_day_equities(directory, account: str, now: datetime) -> dict | None:
    """The highest equity in a previous end-of-day report, which is the fallback the user set."""
    if not directory or not str(directory).strip():
        return None
    rows = []
    for file in sorted(Path(directory).rglob("summary*.json")):
        try:
            payload = json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("account_id")) != str(account):
            continue
        equity = number(payload.get("equity"))
        day = report_day(file, payload)
        if equity is None or equity <= 0 or day is None or day >= now.astimezone(ET).date().isoformat():
            continue
        rows.append({"day": day, "as_of": day, "equity": equity, "file": str(file)})
    if not rows:
        return None
    rows.sort(key=lambda row: row["day"])
    return {"rows": rows, "equities": [row["equity"] for row in rows]}


# ------------------------------------------------------------------ the frozen arithmetic (mirrors live/risk_monitor.py)
def measure(config: dict, positions: list[dict], equity: float, initial: float, peak: float) -> dict:
    if not all(finite(value) and value > 0 for value in (equity, initial, peak)):
        raise CheckError("equity, initial equity and peak equity must all be positive numbers")
    if peak + 1e-9 < equity:
        raise CheckError("the peak equity is below the current equity, which cannot be measured from history")
    values = {row["symbol"]: row["quantity"] * row["price"] for row in positions}
    quantities = {row["symbol"]: row["quantity"] for row in positions}
    prices = {row["symbol"]: row["price"] for row in positions}
    long_gross = sum(value for value in values.values() if value > 0)
    short_gross = -sum(value for value in values.values() if value < 0)
    margin = (config["margin_long_factor"] * long_gross
              + config["margin_short_factor"] * short_gross) / equity
    return {"values": values, "prices": prices, "quantities": quantities, "equity": equity,
            "initial_equity": initial, "long_gross": long_gross, "short_gross": short_gross,
            "margin": margin, "drawdown": equity / peak - 1.0, "positions": len(values),
            "max_ticker": max(values, key=lambda symbol: abs(values[symbol])) if values else None,
            "max_ticker_vs_initial": (max(abs(value) for value in values.values()) / initial) if values else 0.0,
            "over_trim": sorted(symbol for symbol, value in values.items()
                                if abs(value) > config["trim_ticker_above_of_initial"] * initial)}


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def actions(config: dict, state: dict, protected=()) -> dict:
    protected, trim_to = set(protected), config["ticker_cap_of_initial"] * state["initial_equity"]
    halt = state["drawdown"] <= config["drawdown_limit"] + 1e-12
    targets = dict(state["quantities"])
    for name in state["over_trim"]:
        if name in protected:
            continue
        shares = int(math.copysign(math.floor(trim_to / state["prices"][name]), state["quantities"][name]))
        if abs(shares) < abs(state["quantities"][name]):
            targets[name] = shares
    scale = 1.0
    if not halt and state["margin"] > config["delever_margin_above"]:
        scale = config["delever_to"] / state["margin"]
        for name in list(targets):
            if name not in protected:
                targets[name] = int(round(targets[name] * scale))
    if halt:
        targets = {name: (state["quantities"][name] if name in protected else 0) for name in targets}
    return {"halt": halt, "delever_scale": scale, "target_shares": targets,
            "share_changes": {name: targets[name] - state["quantities"][name] for name in targets
                              if targets[name] != state["quantities"][name]}}


def warn_lines(config: dict, ops: dict, state: dict) -> list[str]:
    risk = ops.get("risk_check") or {}
    try:
        ticker = float(risk["warn_ticker_share_of_initial"])
        margin = float(risk["warn_margin"])
        drawdown = float(risk["warn_drawdown"])
        positions = int(config["max_positions"] * float(risk["position_count_warn_fraction"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckError(f"live/ops.json risk_check is missing a WARN threshold: {exc}") from exc
    lines = []
    if state["max_ticker"] and state["max_ticker_vs_initial"] > ticker:
        lines.append(f"{state['max_ticker']} is {state['max_ticker_vs_initial']:.1%} of initial assets, above "
                     f"the {ticker:.0%} warning line (the trim rule starts at "
                     f"{config['trim_ticker_above_of_initial']:.0%}).")
    if state["margin"] > margin:
        lines.append(f"margin {state['margin']:.1%} is above the {margin:.0%} warning line (the cut starts at "
                     f"{config['delever_margin_above']:.0%}).")
    if state["drawdown"] <= drawdown:
        lines.append(f"drawdown {state['drawdown']:.2%} of the peak is past the {drawdown:.0%} warning line "
                     f"(the flatten rule starts at {config['drawdown_limit']:.0%}).")
    if state["positions"] > positions:
        lines.append(f"{state['positions']} positions is past the {positions}-position warning line "
                     f"({config['max_positions']} is the cap).")
    return lines


# ------------------------------------------------------------------ order anomalies
def allowed_book(config: dict, targets: dict, ops: dict) -> tuple[set, set, str]:
    names = {bare(name) for name in (config.get("etf_universe") or ())}
    names |= {bare(name) for name in (config.get("discretionary_symbols") or ())}
    names |= {bare(name) for name in (targets.get("weights") or {})}
    note = (f"target book {targets.get('signal_date')} with {len(targets.get('weights') or {})} names"
            if targets.get("weights") else "no target book was readable: the ETF list and the discretionary "
                                           "names only, so a stock short outside them is reported as unknown")
    # A pause-switch sentinel is never tradable by construction (`live/pause_switch.py::validate` refuses a
    # book that contains one), so seeing one in the account is an anomaly rather than an allowed holding.
    sentinels = set()
    for entry in (ops.get("pause_switch") or {}).values():
        if isinstance(entry, dict):
            sentinels |= {bare(code) for code in entry.values() if isinstance(code, str)}
        elif isinstance(entry, list):
            sentinels |= {bare(code) for code in entry if isinstance(code, str)}
    return names, sentinels - names, note


def symbol_note(name: str, sentinels: set) -> str:
    """Why an unexpected symbol is unexpected: a traded flag is a different problem than an unknown stock."""
    if name in sentinels:
        return ("is a pause-switch sentinel, so the flag itself was traded: reconcile the account before the "
                "switch is read again")
    return "is not in the allowed book, so nothing here should be open in it"


def order_anomalies(orders: list[dict], now: datetime, config: dict, ops: dict, allowed: set,
                    sentinels: set, today: str) -> tuple[list[dict], dict]:
    risk = ops.get("risk_check") or {}
    minutes = float(risk.get("order_working_minutes_alert", 30))
    fraction = float(risk.get("order_budget_warn_fraction", 0.8))
    discretionary = {bare(name) for name in (config.get("discretionary_symbols") or ())}
    try:
        since = timestamp(config["competition_start"]).astimezone(ET).date().isoformat()
    except (CheckError, KeyError, TypeError, ValueError):
        since = today
    out = []
    today_count, cap_count = 0, 0
    for order in orders:
        day = order["created_at"].astimezone(ET).date().isoformat() if order["created_at"] else today
        if day < since:
            continue                      # a row from before the competition started is not this run's spend
        cap_count += 1
        if day == today:
            today_count += 1
        where = f"{order['symbol']} {order['side']} {order['quantity']} ({order['order_id'] or 'no id'})"
        status, filled, quantity = order["status"], order["filled"], order["quantity"]
        if status not in REJECTED_CODES:
            # A rejected ticket is still evidence about its symbol and its tag, so only the fill and
            # working-time checks are skipped for it.
            partial = (status in PARTIAL_CODES) or (finite(filled) and finite(quantity) and 0 < filled < quantity)
            if partial:
                out.append({"kind": "partial_fill",
                            "detail": f"{where}: {filled} of {quantity} filled (status {status}), so the plan's "
                                      f"residual is not what the batch assumed",
                            "order_id": order["order_id"], "symbol": order["symbol"]})
            live = status in LIVE_CODES or (partial and status not in FILLED_CODES)
            if live and order["created_at"]:
                waiting = (now - order["created_at"]).total_seconds() / 60.0
                if waiting > minutes:
                    out.append({"kind": "order_working_too_long",
                                "detail": f"{where}: working {waiting:.0f} min, past the {minutes:.0f} min line "
                                          f"(status {status})",
                                "order_id": order["order_id"], "symbol": order["symbol"]})
        else:
            out.append({"kind": "broker_rejection",
                        "detail": f"{where}: the broker rejected it (status {status})",
                        "order_id": order["order_id"], "symbol": order["symbol"]})
        if order["symbol"] and order["symbol"] not in allowed:
            out.append({"kind": "unexpected_symbol",
                        "detail": f"{where} {symbol_note(order['symbol'], sentinels)}",
                        "order_id": order["order_id"], "symbol": order["symbol"]})
        if not order["client_order_id"].startswith("pstar-") and order["symbol"] not in discretionary:
            out.append({"kind": "untagged_order",
                        "detail": f"{where}: no pstar- tag and not a discretionary name, so this strategy did "
                                  f"not place it: reconcile before the next batch",
                        "order_id": order["order_id"], "symbol": order["symbol"]})
    daily, cap = int(config["max_orders_per_day"]), int(config["competition_order_cap"])
    for label, used, limit, window in (("daily", today_count, daily, today),
                                       ("competition", cap_count, cap, f"{since} to {today}")):
        if used > fraction * limit:
            out.append({"kind": "order_budget",
                        "detail": f"{used} orders over {window} is past {fraction:.0%} of the {limit}-order "
                                  f"{label} limit: {limit - used} left, and a batch still needs its reprice "
                                  f"rounds and the {int(config['emergency_flatten_reserve'])}-order "
                                  f"emergency reserve",
                        "orders_today": today_count, "orders_since_start": cap_count})
    return out, {"orders_today": today_count, "orders_since_start": cap_count,
                 "since_competition_start": since}


# ------------------------------------------------------------------ the report
def resolve_option(path, fallback: str) -> Path:
    """A file the operator named, else the repository copy, else the cwd copy.

    The zip installs this script outside the checkout, so the defaults try both roots before failing with the
    name of the file that was wanted.
    """
    given = Path(path) if path else None
    if given is not None and given.is_file():
        return given
    for candidate in (REPO / fallback, Path(fallback)):
        if candidate.is_file():
            return candidate
    return given or REPO / fallback


def build_report(args) -> dict:
    config_path = resolve_option(args.config, "live/config.pstar.json")
    ops_path = resolve_option(args.ops, "live/ops.json")
    config = read_json(config_path, "config")
    if config.get("strategy") != "pstar":
        raise CheckError(f"{config_path}: this check only serves the deployed pstar configuration")
    ops = read_json(ops_path, "ops")
    raw = {key: read_json(getattr(args, key), key) for key in ("cash", "positions", "orders")}
    data = {key: unwrap(payload, getattr(args, key), TOOL_BY_FILE[key]) for key, payload in raw.items()}
    account = str(args.account)
    for key, payload in raw.items():
        found = account_found(payload)
        if found and found != {account}:
            raise CheckError(f"{getattr(args, key)} names account {' '.join(sorted(found))}, not {account}: "
                             f"the check would be measuring the wrong book")
    if args.accounts:
        listing = unwrap(read_json(args.accounts, "accounts"), args.accounts, TOOL_BY_FILE["accounts"])
        ids = {str(row.get("account_id")) for row in listing.get("accounts") or () if isinstance(row, dict)}
        if ids and account not in ids:
            raise CheckError(f"{args.accounts}: {account} is not one of the paper accounts "
                             f"{', '.join(sorted(ids)) or 'listed'}")
    targets = {}
    if args.targets:
        try:
            payload = read_json(args.targets, "targets")
            targets = payload if isinstance(payload, dict) else {}
        except CheckError:
            targets = {}
    now = timestamp(args.as_of) if args.as_of else now_utc()
    positions = read_positions(data["positions"])
    cash = read_cash(data["cash"])
    orders = read_orders(data["orders"])
    initial = number(config.get("initial_equity")) or 100_000.0
    peak = resolve_peak(args, config, cash["equity"], now)
    state = measure(config, positions, cash["equity"], initial, peak["peak_equity"])
    discretionary = {bare(name) for name in (config.get("discretionary_symbols") or ())}
    opened = {bare(order["symbol"]) for order in orders if order["client_order_id"].startswith("pstar-")}
    protected = sorted((set(state["quantities"]) - opened) - discretionary)
    risk = actions(config, state, protected)
    warnings = warn_lines(config, ops, state)
    allowed, sentinels, book_note = allowed_book(config, targets, ops)
    today = now.astimezone(ET).date().isoformat()
    anomalies, counts = order_anomalies(orders, now, config, ops, allowed, sentinels, today)
    anomalies += [{"kind": "unexpected_symbol",
                   "detail": f"{name} {quantity} shares {symbol_note(name, sentinels)}",
                   "symbol": name}
                  for name, quantity in sorted(state["quantities"].items()) if name not in allowed]
    findings = []
    if risk["halt"]:
        findings.append({"rule": "drawdown", "value": round(state["drawdown"], 6),
                         "threshold": config["drawdown_limit"],
                         "detail": (f"equity {cash['equity']:,.2f} of a {peak['peak_equity']:,.2f} peak "
                                    f"({state['drawdown']:.2%}) - flatten and halt")})
    for name in state["over_trim"]:
        findings.append({"rule": "ticker", "value": round(abs(state["values"][name]) / initial, 6),
                         "threshold": config["trim_ticker_above_of_initial"],
                         "detail": (f"{name} {abs(state['values'][name]):,.0f} USD is "
                                    f"{abs(state['values'][name]) / initial:.1%} of initial assets"
                                    + (" (not a strategy position: alert only)" if name in protected else
                                       f", trim to {config['ticker_cap_of_initial']:.0%}"))})
    if not risk["halt"] and state["margin"] > config["delever_margin_above"]:
        findings.append({"rule": "margin", "value": round(state["margin"], 6),
                         "threshold": config["delever_margin_above"],
                         "detail": (f"(0.5 x {state['long_gross']:,.0f} + 0.6 x {state['short_gross']:,.0f}) / "
                                    f"{state['equity']:,.0f} = {state['margin']:.1%}, scale the book by "
                                    f"{risk['delever_scale']:.4f} to reach "
                                    f"{config['delever_to']:.0%} margin")})
    level = ("KILL" if risk["halt"] else "ALERT" if risk["share_changes"]
             else "WARN" if warnings else "OK")
    return {"schema_version": SCHEMA_VERSION, "level": level, "account_id": account,
            "mode": config.get("mode"), "strategy_id": targets.get("strategy_id"),
            "variant": config.get("variant"), "generated_at": iso(now),
            "as_of_et": now.astimezone(ET).isoformat(),
            "source": f"skills/pstar-risk-check/scripts/risk_check.py from "
                      f"{args.cash.name}, {args.positions.name}, {args.orders.name}",
            "config_file": str(config_path), "ops_file": str(ops_path), "targets_file": str(args.targets),
            "peak_file": str(args.peak_file) if str(args.peak_file or "").strip() else None,
            "eod_dir": str(args.eod_dir) if str(args.eod_dir or "").strip() else None,
            "targets_checksum": targets.get("checksum"), "allowed_book": book_note,
            "protection_basis": "positions with no pstar- tagged order in the history file given are reported "
                                "but not traded; golive.py re-derives the same split from the journal, and "
                                "that attribution is what sizes a ticket",
            "peak_equity": peak["peak_equity"], "peak_basis": peak["peak_basis"],
            "peak_history": peak["peak_history"],
            "previous_close_equity": peak["previous_close_equity"],
            "measured": {"equity": cash["equity"], "initial_equity": initial,
                         "peak_equity": peak["peak_equity"], "cash": cash["cash"],
                         "long_gross": state["long_gross"], "short_gross": state["short_gross"],
                         "margin": round(state["margin"], 6), "drawdown": round(state["drawdown"], 6),
                         "positions": state["positions"], "max_ticker": state["max_ticker"],
                         "max_ticker_vs_initial": round(state["max_ticker_vs_initial"], 6),
                         "orders_today": counts["orders_today"],
                         "orders_since_start": counts["orders_since_start"],
                         "orders_counted_since": counts["since_competition_start"],
                         "live_orders": sum(1 for order in orders if order["status"] in LIVE_CODES)},
            "findings": findings, "warnings": warnings,
            "anomalies": anomalies,
            "target_shares": {name: quantity for name, quantity in sorted(risk["target_shares"].items())},
            "share_changes": {name: delta for name, delta in sorted(risk["share_changes"].items())},
            "protected": protected,
            "positions": [{"symbol": row["symbol"], "quantity": row["quantity"], "price": row["price"],
                           "value": round(row["value"], 2)}
                          for row in sorted(positions, key=lambda row: row["symbol"])],
            "slack_text": "", "note": "the report triggers a check; only golive.py plan sizes an order"}


def slack_text(report: dict) -> str:
    """The lines that get posted: level, the numbers, the tickets the plan would build, nothing else."""
    measured = report["measured"]
    icon = {"OK": "ok", "WARN": "warn", "ALERT": "ALERT", "KILL": "KILL"}[report["level"]]
    head = (f"pstar risk check {report['account_id']} ({report['mode']}, {icon}): "
            f"{report['level']} at {report['as_of_et']} ET - equity {measured['equity']:,.2f}, "
            f"margin {measured['margin']:.1%}, largest {measured['max_ticker'] or '-'} "
            f"{measured['max_ticker_vs_initial']:.1%} of initial, drawdown {measured['drawdown']:.2%}, "
            f"{measured['positions']} positions")
    lines = [head, f"peak basis: {report['peak_basis']}"]
    lines += [f"finding: {item['detail']}" for item in report["findings"]]
    lines += [f"warning: {line}" for line in report["warnings"]]
    lines += [f"anomaly: {item['detail']}" for item in report["anomalies"]]
    if report["share_changes"]:
        order = sorted(report["share_changes"], key=lambda name: -abs(report["share_changes"][name]))
        lines.append("reduce-only, sized by the plan: "
                     + ", ".join(f"{name} {report['share_changes'][name]:+d}" for name in order))
    if report["protected"]:
        lines.append(f"not opened by this strategy, alert only: {' '.join(report['protected'])}")
    if report["level"] == "KILL":
        lines.append("the pause switch is set by `golive.py plan --risk-action` and the durable halt latches "
                     "with the tickets; nothing reopens without `RESUME`.")
    lines.append("an alert is not a stop order: between checks the book is unhedged.")
    return "\n".join(lines)


def check_failed(args, error: Exception) -> dict:
    now = timestamp(args.as_of) if getattr(args, "as_of", None) else now_utc()
    return {"schema_version": SCHEMA_VERSION, "level": "CHECK_FAILED",
            "account_id": str(getattr(args, "account", "")), "mode": None,
            "generated_at": iso(now), "as_of_et": now.astimezone(ET).isoformat(),
            "source": "skills/pstar-risk-check/scripts/risk_check.py",
            "peak_equity": None, "peak_basis": "unread: the check failed before it could measure a peak",
            "peak_history": [], "measured": {}, "findings": [], "warnings": [],
            "anomalies": [{"kind": "check_failed", "detail": f"{type(error).__name__}: {error}"}],
            "positions": [], "target_shares": {}, "share_changes": {}, "protected": [],
            "slack_text": (f"pstar risk check {getattr(args, 'account', '?')}: CHECK FAILED at "
                           f"{now.astimezone(ET).isoformat()} - {error}\n"
                           f"nothing was measured and nothing was ordered; re-read the account and run again. "
                           f"A failed check is never silent."),
            "note": "a CHECK FAILED report is never fed to `golive.py plan --risk-action`"}


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cash", required=True, type=Path)
    parser.add_argument("--positions", required=True, type=Path)
    parser.add_argument("--orders", required=True, type=Path)
    parser.add_argument("--accounts", type=Path, help="raw sim_trade_account_list, to prove the account exists")
    parser.add_argument("--account", required=True)
    parser.add_argument("--config", type=Path, help="default: live/config.pstar.json in the checkout")
    parser.add_argument("--ops", type=Path, help="default: live/ops.json in the checkout")
    parser.add_argument("--targets", type=Path, help="the sealed target book, for the allowed-symbol list")
    parser.add_argument("--peak-file",
                        default=str(REPO / "reports" / "live" / "golive" / "state" / "risk_peak_equity.json"),
                        help='the running-peak state file; pass "" to measure without one')
    parser.add_argument("--eod-dir", default=str(REPO / "reports" / "live"),
                        help='where end-of-day summary*.json reports live, the peak fallback; "" for none')
    parser.add_argument("--no-write-peak", dest="write_peak", action="store_false",
                        help="measure without updating the peak file")
    parser.add_argument("--as-of", help="pin the clock (ISO or epoch); defaults to now")
    parser.add_argument("--out", type=Path, required=True, help="where to write risk_report.json")
    parser.add_argument("--slack-out", type=Path, help="also write the Slack text to this file")
    args = parser.parse_args(argv)
    try:
        report = build_report(args)
    except (CheckError, OSError, ValueError, KeyError) as exc:
        report = check_failed(args, exc)
    if report["level"] != "CHECK_FAILED":
        report["slack_text"] = slack_text(report)
    write_report(Path(args.out), report)
    if args.slack_out:
        Path(args.slack_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.slack_out).write_text(report["slack_text"] + "\n", encoding="utf-8", newline="\n")
    print(report["slack_text"])
    print(f"\nreport: {args.out}")
    if report["level"] == "CHECK_FAILED":
        return 2
    return 0 if report["level"] == "OK" and not report["anomalies"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
