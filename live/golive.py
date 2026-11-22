"""Go-live execution tooling for the P* ENTRY batch (prompt 07 part 1).

The moomoo MCP is reachable only from the Desktop chat session, so the work is split deliberately: the
chat agent saves every raw MCP response verbatim to a file and carries the exact payload back, and this
module does all parsing, sizing, gating, repricing and reconciliation in deterministic Python. It never
calls a broker, never edits a target book and never invents a number: a field the raw payloads do not
carry stays null and is reported as unverified, and anything unreadable raises instead of being guessed.

Every instruction it writes comes from the prompt-06 path: sizing and the frozen execution rules from
`live/order_planner.py`, the R0-R21 decision from `rules/gate.py` (dispatched by `config["strategy"]`),
the account attribution from `live/pstar.py::classify` and the risk actions from `live/risk_monitor.py`.
The one thing this module adds is a marketable limit price, because the planner prices a limit exactly AT
the relay-verified quote (the mid) and a limit at the mid is not marketable.

Nothing here places an order: `plan` writes tickets for the chat agent to submit, and `record` only writes
down what the broker answered. Since the user decided automatic execution on 2026-11-21 there is no human
approval step in the flow; the human control is the `pause`/`resume` switch in this file.

usage (project venv, from the repository root; docs/GOLIVE.md gives the run order and the evidence files):
  python -m live.golive pause     --account 5832812 --source "slack PAUSE 1732..." [--orders orders.json]
  python -m live.golive resume    --account 5832812 --source "user in chat"
  python -m live.golive flag      --account 5832812 --read flag.json [--set paused] [--lease local-entry]
  python -m live.golive snapshot  --account 5322438 --cash cash.json --positions positions.json \
                                  --orders orders.json --accounts accounts.json
  python -m live.golive evidence  --account 5322438 --snapshot <file> --orders orders.json \
                                  --trigger "user pasted 07 Part 2" --out evidence.json
  python -m live.golive evidence  --account 5832812 --side cloud --snapshot <file> --orders orders.json \
                                  --flag flag.json [--risk-report risk.json] --trigger "scheduled run" \
                                  --auditor-session <id> --executor-session <id> --out evidence.json
  python -m live.golive plan      --account 5322438 --snapshot <file> --targets <file> --quotes quotes.json \
                                  [--borrow borrow.json] [--orders orders.json] [--evidence evidence.json] \
                                  [--flag flag.json] [--smoke] [--final-exit] [--risk-action risk.json]
  python -m live.golive record    --plan plan.json --ticket 3 --response response.json [--orders orders.json]
  python -m live.golive reprice   --plan plan.json --orders orders.json --quotes quotes.json [--working 1] \
                                  [--flag flag.json]
  python -m live.golive reconcile --plan plan.json --snapshot <file> --orders orders.json [--working 1]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from journal.store import Journal
from live import pause_switch
from live.contracts import (PSTAR_SCHEMA_VERSION, ROOT, digest, finite, load_strategy_config, strict_json,
                            timestamp, verify)
from live.eligibility import plan_eligibility
from live.order_planner import client_order_id, holdings, plan as planner_plan, target_shares
from live.pstar import classify, read_journal
from live.relay import paper_payload
from live.risk_monitor import actions, measure, warn_lines
from rules.gate import ACCOUNTS, order_fingerprint
from rules.gate_pstar import market_flatten_window
from rules.trading_calendar import ET

MARKET_ID = 100                      # US margin paper account, per sim_trade_account_list
GO_DIR = ROOT / "reports/live/golive"
STATE_DIR = GO_DIR / "state"
MODE_OF_ACCOUNT = {value: key for key, value in ACCOUNTS.items()}

# The prompt-07 price policy: a marketable limit, 0.05% through the touch, at least one cent through it.
PRICE_OFFSET = 0.0005
CENT = 0.01
REPRICE_AFTER_SECONDS = 300          # "still unfilled after 5 minutes"
MAX_REPRICE_ROUNDS = 3
SMOKE_ETF = "IWM"                    # one ETF buy
SMOKE_SHORT_MAX_PRICE = 50.0         # one executable short priced below $50
SMOKE_SIZE = 1                       # one share each way
# The risk ladder `plan --risk-action` reads, weakest first: WARN posts to Slack and places nothing,
# ALERT builds reduce-only tickets, KILL flattens the strategy book and latches the halt.
RISK_LEVELS = ("OK", "WARN", "ALERT", "KILL")
RISK_SCHEMA = "pstar-risk/1"
# The peak the Risk Auditor carries in its report is the only high-water mark that crosses between two
# machines; these two bounds are what stops a typo in that field from flattening the account.
PEAK_SANITY_MULTIPLE = 10.0
MAX_PEAK_HISTORY = 60

# The paper order list's `status` field, enumerated 1-7 as the moomoo simulated-trading order-status codes:
#   1 pending new   2 submitted   3 partially filled   4 filled
#   5 cancelled     6 rejected    7 partially filled then cancelled
# Provenance, stated honestly: the 1-7 list is documentation-derived knowledge carried in the operator's
# 2026-11-21 instruction. It was NOT verified in this delivery -- the MCP metadata for
# sim_trade_history_order_list publishes only request fields (acc_id, market, next_key, page_size, time_begin,
# time_end) and no status enum, so nothing here proves the mapping for the codes the tests and docs call
# unobserved. What local evidence does reach: 4 with cum_qty == qty and 5 with cum_qty == 0 both appear in the
# 2026-11-20 history of 5322438; 1, 2, 3, 6 and 7 have never been seen on a real book.
# Code 0 appears there on two option rows with create_time == update_time and is in no published table, so
# it stays UNKNOWN until the operator proves what it means with --working; golive never calls an order
# finished or still-live on a guess. If a future read shows a code behaving otherwise, fix this table from
# that observation and treat the documentation as the weaker source.
STATUS_LIVE = {1, 2, 3}              # the exchange still works these, so they hold the batch
STATUS_FILLED = {4}
STATUS_CLOSED_NO_FILL = {5, 6}       # cancelled or rejected with nothing filled
STATUS_CANCELLED_PARTIAL = {7}       # filled, then cut short: resolved, and no longer live
STATUS_CANCELLED = 5                 # the operator's own `sim_trade_cancel_order` lands here
STATUS_REJECTED = 6                  # anything else is the broker refusing a ticket we sent

STRATEGY_ORDER_PREFIX = "pstar-"     # client order id prefix every ticket of this strategy carries
SYMBOL_KEYS = ("symbol", "code", "stock_code")
QTY_KEYS = ("qty", "quantity", "normal_qty", "position_qty")
COST_KEYS = ("cost_price", "avg_price", "average_cost", "cost_basis", "cost")
MV_KEYS = ("mv", "market_value", "mkt_val")
PRICE_KEYS = ("last_price", "nominal_price", "market_price", "price")
DIRECTION_KEYS = ("direction", "position_side", "long_short", "position_flag")
DIRECTION_SIGN = {"long": 1, "buy": 1, "pos": 1, "positive": 1, "short": -1, "sell": -1, "neg": -1,
                  "negative": -1}
ACCOUNT_KEYS = ("acc_id", "account_id", "acc_id_str", "business_account_id")
SIDE_BY_CODE = {1: "BUY", 2: "SELL"}
ORDER_TYPE_NAMES = {1: "LIMIT", 3: "MARKET"}
PAPER_ACCOUNT_TYPES = (1, 2)         # sim_trade_account_list codes for the two paper accounts

RELAY_NOTE = ("golive.py is the deterministic relay for this session: verified_by_relay on a snapshot, "
              "quote or eligibility row means the value was parsed here from the raw MCP response named in "
              "`source` together with its SHA-256, not typed by a model. Approvals and capability flags are "
              "never asserted here; they are copied from the operator evidence file, which names the human "
              "reply and the session that made it.")
DEFAULT_ASSERTIONS = {"release_verified": False, "lease_verified": False, "permission_policy_verified": False,
                      "reconciled": False, "snapshot_verified_by_relay": False}


class GoliveError(ValueError):
    """The raw payloads cannot be turned into a safe instruction: send nothing and report this."""


# ------------------------------------------------------------------ the durable kill switch, read only
def kill_switch_state(journal_dir, account: str) -> dict:
    """The durable local halt latch as R16 sees it, read straight from the control store (never written).

    Only `golive.py resume` or `golive.py pause` writes a control row, and each records the `--source` that
    asked for it, so a paused account is reported as the reason nothing can pass rather than hidden in a
    per-ticket rejection list (PRD R9/R16).
    """
    if not journal_dir:
        return {"halted": None, "verified_by_relay": False, "note": "no journal folder given"}
    try:
        journal = Journal(Path(journal_dir))
        try:
            control = journal.control(account)
        finally:
            journal.close()
    except (ValueError, OSError) as exc:
        return {"halted": None, "verified_by_relay": False, "note": f"control store unreadable: {exc}"}
    return {"halted": bool(control.get("halted")), "reason": control.get("reason"),
            "verified_by_relay": control.get("verified_by_relay") is True,
            "note": ("halted/paused: R9 and R16 reject every strategy order except a FINAL_EXIT or "
                     "EMERGENCY_EXIT reduction until `resume`"
                     if control.get("halted") else "running: R16 allows strategy orders while no halt is latched")}


# ------------------------------------------------------------------ reading raw MCP files
def read_json(path: Path):
    path = Path(path)
    try:
        return strict_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoliveError(f"cannot read {path}: {exc}") from exc


def write_json(path: Path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return path


def num(value) -> float | None:
    """A broker number arrives as a string, an int, a float or null; unreadable stays null."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if finite(value) else None
    if isinstance(value, str):
        text = value.replace(",", "").strip()
        if not text or text.lower() in ("null", "none", "n/a", "-", "--"):
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed if finite(parsed) else None
    return None


def whole(value) -> int | None:
    """A share count: only a whole number reads; fractional or absent stays null."""
    number = num(value)
    if number is None or number != int(number):
        return None
    return int(number)


def positive(value) -> float | None:
    number = num(value)
    return number if number is not None and number > 0 else None


def epoch(value):
    """A broker timestamp in microseconds (orders) or milliseconds (quotes) as an aware datetime."""
    number = num(value)
    if number is None:
        return None
    if number >= 1e14:
        seconds = number / 1_000_000
    elif number >= 1e11:
        seconds = number / 1_000
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def iso(value) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def text(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def bare(symbol) -> str | None:
    name = text(symbol)
    if name is None:
        return None
    return name.removeprefix("US.")


def _pick(row: dict, keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return key, row[key]
    return None, None


def data_of(raw, tool: str, path: Path) -> dict:
    """Unwrap one MCP envelope and refuse a response that says it did not succeed."""
    if not isinstance(raw, dict):
        raise GoliveError(f"{path.name} is not a {tool} response object")
    code = raw.get("ret_code")
    if code is not None and str(code) != "0":
        raise GoliveError(f"{tool} in {path.name} did not succeed: ret_code={code} "
                          f"ret_msg={raw.get('ret_msg')!r}")
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        raise GoliveError(f"{tool} response in {path.name} has no data block")
    return data


def account_ids(value, found: set) -> set:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ACCOUNT_KEYS:
                marker = text(item)
                if marker and marker.isdigit():
                    found.add(marker)
            account_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            account_ids(item, found)
    return found


def bind_account(payloads, account: str, listing=None) -> dict:
    """Refuse the batch if any raw response names a different account; say so when none names any.

    `sim_trade_cash_info` and `sim_trade_position_list` do not echo `acc_id`, so when only those files are
    given the binding is the operator's declaration, and `binding` records that honestly rather than
    pretending the API asserted it. `sim_trade_account_list` is the exception: it enumerates every paper
    account, so naming the requested one there is an API assertion while naming the *other* one is normal
    and must never look like a mismatch.
    """
    found: set = set()
    for payload in payloads:
        account_ids(payload, found)
    wrong = sorted(found - {account})
    if wrong:
        raise GoliveError(f"raw responses name account {', '.join(wrong)}, not the requested {account}")
    listed: set = set()
    if listing is not None:
        account_ids(listing, listed)
    asserted = found | ({account} & listed)
    return {"requested": account, "asserted_by_api": sorted(asserted),
            "accounts_in_list": sorted(listed),
            "binding": "api-asserted" if asserted else "operator-declared"}


def mode_for(account: str) -> str:
    if account not in MODE_OF_ACCOUNT:
        raise GoliveError(f"{account} is not an authorised paper account (test {ACCOUNTS['test']} or "
                          f"competition {ACCOUNTS['competition']})")
    return MODE_OF_ACCOUNT[account]


def provenance(path: Path) -> dict:
    path = Path(path)
    return {"file": relative(path), "file_full": str(path.resolve()), "sha256": sha_file(path),
            "bytes": path.stat().st_size,
            "captured_at": iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))}


def relative(path) -> str:
    """How an artifact is shown to the operator: repository-relative when it is inside, absolute if not."""
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def show(value, fmt=",.2f") -> str:
    return "null" if value is None else format(value, fmt)


# ------------------------------------------------------------------ parsers
def parse_cash(data: dict) -> dict:
    out = {"cash": num(data.get("balance")), "frozen_cash": num(data.get("hold")),
           "market_value": num(data.get("mv")), "long_gross": num(data.get("long_mv")),
           "short_gross": num(data.get("short_mv")), "equity": positive(data.get("total_asset")),
           "buy_power_long": num(data.get("max_power_long")),
           "realized_profit": num(data.get("realized_profit")),
           "unrealized_profit": num(data.get("unrealized_profit"))}
    if out["equity"] is None:
        raise GoliveError("cash_info gave no usable total_asset, so the equity cannot be sized")
    return out


def parse_positions(data: dict) -> tuple[list[dict], list[str]]:
    """Signed whole-share holdings: the sign comes from a negative quantity or a named direction only."""
    rows = []
    for raw in data.get("positions") or ():
        if not isinstance(raw, dict):
            raise GoliveError(f"position row is not an object: {raw!r}")
        _, symbol = _pick(raw, SYMBOL_KEYS)
        if not isinstance(symbol, str) or not symbol:
            raise GoliveError(f"position row has no symbol; keys seen: {sorted(raw)}")
        _, quantity_raw = _pick(raw, QTY_KEYS)
        quantity = whole(quantity_raw)
        if quantity is None:
            raise GoliveError(f"{symbol}: quantity {quantity_raw!r} is absent or fractional; "
                              f"keys seen: {sorted(raw)}")
        _, direction = _pick(raw, DIRECTION_KEYS)
        named = None if direction is None else DIRECTION_SIGN.get(str(direction).strip().lower())
        if direction is not None and named is None:
            raise GoliveError(f"{symbol}: cannot tell a long from a short in direction {direction!r}; "
                              f"keys seen: {sorted(raw)}")
        if named is not None and quantity and named != (1 if quantity > 0 else -1):
            raise GoliveError(f"{symbol}: direction {direction!r} contradicts quantity {quantity_raw!r}; "
                              f"reconcile the account before planning")
        sign = named if named is not None else (1 if quantity >= 0 else -1)
        magnitude = abs(quantity)
        if sign > 0 and magnitude and (num(raw.get("mv")) or 0) < 0:
            sign = -1
        _, cost_raw = _pick(raw, COST_KEYS)
        _, mv_raw = _pick(raw, MV_KEYS)
        price_key, price_raw = _pick(raw, PRICE_KEYS)
        market_value = num(mv_raw)
        price = positive(price_raw)
        basis = None if price is None else f"broker field {price_key}"
        if price is None and market_value is not None and magnitude:
            price, basis = abs(market_value) / magnitude, "derived from the signed market value"
        if market_value is None and price is not None:
            market_value = sign * magnitude * price
        rows.append({"symbol": bare(symbol), "broker_symbol": symbol, "quantity": sign * magnitude,
                     "avg_cost": positive(cost_raw), "market_value": market_value, "price": price,
                     "price_basis": basis})
    keys = sorted({key for raw in data.get("positions") or () if isinstance(raw, dict) for key in raw})
    return rows, keys


def order_state(quantity, filled, code, working=()) -> str:
    if quantity is None or filled is None:
        return "UNREADABLE"
    if quantity > 0 and filled >= quantity:
        return "FILLED"
    if code in STATUS_FILLED:
        # A filled status with less than the ordered quantity is contradictory evidence: not live, not
        # finished, and never a reason to reissue the balance.
        return "UNKNOWN"
    if code in STATUS_CANCELLED_PARTIAL:
        # Documented as "partially filled then cancelled": the broker will fill nothing more, so this is
        # a resolved order, not an unknown one, and it must not hold the batch.
        return "CANCELLED_PARTIAL" if filled else "CLOSED_NO_FILL"
    if filled > 0:
        return "PARTIAL"
    if code in working or code in STATUS_LIVE:
        return "WORKING"
    if code in STATUS_CLOSED_NO_FILL:
        return "CLOSED_NO_FILL"
    # Any other code is unmapped: the operator proves what it means with --working.
    return "UNKNOWN"


def parse_orders(data: dict, working=()) -> list[dict]:
    rows = []
    for raw in data.get("orders") or ():
        if not isinstance(raw, dict):
            raise GoliveError(f"order row is not an object: {raw!r}")
        quantity, filled = whole(raw.get("qty")), whole(raw.get("cum_qty"))
        side = raw.get("side")
        rows.append({"order_id": text(raw.get("order_id")), "broker_symbol": raw.get("symbol"),
                     "symbol": bare(raw.get("symbol")), "side": SIDE_BY_CODE.get(side, text(side)),
                     "broker_side_code": side, "quantity": quantity, "filled": filled,
                     "remaining": None if quantity is None or filled is None else quantity - filled,
                     "limit_price": positive(raw.get("price")),
                     "avg_fill_price": positive(raw.get("avg_fill_price")),
                     "status_code": raw.get("status"),
                     "state": order_state(quantity, filled, raw.get("status"), working),
                     "client_order_id": text(raw.get("text")),
                     "submitted_at": iso(epoch(raw.get("create_time"))),
                     "updated_at": iso(epoch(raw.get("update_time"))),
                     "order_type": ORDER_TYPE_NAMES.get(raw.get("order_type"), raw.get("order_type")),
                     "order_trade_time_type": raw.get("order_trade_time_type"),
                     "time_in_force_code": raw.get("time_in_force"), "market_code": raw.get("market")})
    return rows


def parse_quotes(payload) -> dict[str, dict]:
    """Best bid/offer, the broker's own classification and the fund or market size, per name."""
    responses = payload if isinstance(payload, list) else [payload]
    out: dict[str, dict] = {}
    for index, raw in enumerate(responses):
        data = data_of(raw, "quote_market_snapshot", Path(f"quotes[{index}]"))
        for row in data.get("snapshot_list") or ():
            if not isinstance(row, dict):
                raise GoliveError(f"quote row is not an object: {row!r}")
            name = bare(row.get("code"))
            if not name:
                raise GoliveError(f"quote row has no code; keys seen: {sorted(row)}")
            bid, ask, last = positive(row.get("bid_price")), positive(row.get("ask_price")), \
                positive(row.get("last_price"))
            status = row.get("sec_status")
            if not isinstance(status, str):
                halted = None
            elif status.upper() != "NORMAL" or row.get("suspension") is True:
                halted = True
            else:
                halted = False
            if row.get("trust_valid") is True:
                security_type = "ETF"
            elif row.get("equity_valid") is True:
                security_type = "COMMON_STOCK"
            else:
                security_type = None
            size = positive(row.get("total_market_val"))
            if bid and ask:
                price, basis = (bid + ask) / 2, "mid of bid/ask"
            elif last:
                price, basis = last, "last trade (no two-sided quote)"
            else:
                price, basis = None, None
            out[name] = {"symbol": name, "broker_symbol": row.get("code"),
                         "market": "US" if str(row.get("code", "")).startswith("US.") else None,
                         "security_type": security_type, "halted": halted,
                         "as_of": iso(epoch(row.get("update_time"))),
                         "bid": bid, "ask": ask, "last": last, "price": price, "price_basis": basis,
                         "market_cap": size, "market_value": positive(row.get("trust_aum")) or size,
                         "lot_size": whole(row.get("lot_size"))}
    return out


def parse_borrow(path: Path) -> dict[str, dict]:
    """`sim_trade_max_buy_sell` echoes no symbol, so the saved file must name each response's symbol."""
    payload = read_json(path)
    if isinstance(payload, dict) and "ret_code" in payload:
        raise GoliveError(f"{path.name}: a max_buy_sell response names no symbol; save each one as "
                          '{"symbol": "US.AAPL", "response": <raw response>} with the raw block verbatim}')
    items = payload if isinstance(payload, list) else [{"symbol": key, "response": value}
                                                       for key, value in (payload or {}).items()]
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise GoliveError(f"{path.name}: borrow row is not an object: {item!r}")
        symbol = item.get("symbol") or item.get("code")
        raw = item.get("response", item)
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        if not isinstance(symbol, str):
            raise GoliveError(f"{path.name}: borrow row has no symbol: {item!r}")
        out[bare(symbol)] = {"max_sell_short_qty": whole(data.get("max_sell_short_qty")),
                             "max_sell_qty_round_lot": whole(data.get("max_sell_qty_round_lot")),
                             "max_margin_buy_qty_round_lot": whole(data.get("max_margin_buy_qty_round_lot")),
                             "max_cash_buy_qty_round_lot": whole(data.get("max_cash_buy_qty_round_lot"))}
    return out


# ------------------------------------------------------------------ context pieces
def sizing_quotes(quotes: dict) -> dict:
    """The map live/order_planner.py prices and sizes against: the relay-verified quote mid."""
    return {name: {"verified_by_relay": True, "price": row["price"], "as_of": row["as_of"]}
            for name, row in quotes.items() if row.get("price")}


def gate_quote(row: dict, side: str) -> dict:
    """The quote the ticket is actually marketable against: the touch on its own side."""
    touch = {"BUY": row.get("ask"), "SELL": row.get("bid")}.get(side) or row.get("price")
    return {"verified_by_relay": True, "price": touch, "as_of": row.get("as_of"), "bid": row.get("bid"),
            "ask": row.get("ask"), "last": row.get("last")}


def marketable_limit(row: dict, side: str) -> tuple[float | None, str | None]:
    """ask + 0.05% rounded up, or bid - 0.05% rounded down, never less than a cent through the touch."""
    touch = {"BUY": row.get("ask"), "SELL": row.get("bid")}.get(side)
    if touch is None:
        return None, f"the quote row has no {('ask' if side == 'BUY' else 'bid')} to step through"
    if side == "BUY":
        price = max(math.ceil(touch * (1 + PRICE_OFFSET) * 100) / 100, round(touch + CENT, 2))
    else:
        price = min(math.floor(touch * (1 - PRICE_OFFSET) * 100) / 100, round(touch - CENT, 2))
    if price <= 0:
        return None, f"a {side} limit through a ${touch:g} touch would not be a positive price"
    return round(price, 2), None


def pending_from_history(history: list[dict], now: datetime, working) -> tuple[list[dict], list[dict]]:
    """Broker rows the gate must treat as live, plus today's orders this plan did not place."""
    pending, blockers = [], []
    day = now.astimezone(ET).date().isoformat()
    for row in history:
        submitted = row.get("submitted_at") or ""
        if submitted[:10] != day or row.get("state") in RESOLVED_STATES:
            continue
        ours = str(row.get("client_order_id") or "").startswith("pstar-")
        if not row.get("limit_price"):
            blockers.append({"symbol": row.get("symbol"), "order_id": row.get("order_id"),
                             "reason": f"a live broker row has no limit price (order_type "
                                       f"{row.get('order_type')}): reconcile it before planning"})
            continue
        if not ours:
            blockers.append({"symbol": row.get("symbol"), "order_id": row.get("order_id"),
                             "reason": f"an order outside this plan is still {row['state'].lower()}: "
                                       f"{row.get('client_order_id') or row.get('order_id')}"})
            continue
        pending.append({"order_id": row.get("order_id"), "symbol": row["symbol"], "side": row["side"],
                        "remaining_quantity": row.get("remaining") or 0,
                        "limit_price": row["limit_price"],
                        "status": {"PARTIAL": "PARTIAL", "WORKING": "SUBMITTED"}.get(row["state"],
                                                                                   "UNKNOWN"),
                        "submitted_at": submitted, "reduce_only_verified": False})
    return pending, blockers


def executed_from_history(history: list[dict], now: datetime) -> tuple[list, list]:
    """Order ids that used today's executed-order budget, and every pstar client id we have seen."""
    day = now.astimezone(ET).date().isoformat()
    executed, seen = [], []
    for row in history:
        client_id = str(row.get("client_order_id") or "")
        if not client_id.startswith("pstar-") or (row.get("submitted_at") or "")[:10] != day:
            continue
        seen.append(client_id)
        if row.get("filled"):
            executed.append(row.get("order_id"))
    return executed, seen


def eligibility_records(config: dict, quotes: dict, borrow: dict, want: dict, etb_proxy: str | None) -> dict:
    """The broker's own classification and short capacity, in the field names R2, R5 and R11 read."""
    records = {}
    for name, row in quotes.items():
        record = {"verified_by_relay": True, "market": row.get("market"),
                  "security_type": row.get("security_type"), "halted": row.get("halted"),
                  "as_of": row.get("as_of"), "price": row.get("price"), "market_cap": row.get("market_cap"),
                  "market_value": row.get("market_value")}
        if name in want and want[name] < 0:
            capacity = (borrow.get(name) or {}).get("max_sell_short_qty")
            needed = abs(want[name])
            record["short_capacity_shares"] = capacity
            record["shortable"] = capacity is not None and capacity >= needed
            record["borrow_evidence"] = "sim_trade_max_buy_sell.max_sell_short_qty"
            if etb_proxy:
                record["easy_to_borrow"] = record["shortable"]
                record["easy_to_borrow_basis"] = (f"declared proxy: {etb_proxy}. The allowed tools expose "
                                                  "no easy-to-borrow flag or pool depth.")
            else:
                record["easy_to_borrow"] = None
                record["easy_to_borrow_basis"] = ("no borrow-pool evidence: name the allowed tool in the "
                                                  "evidence file, otherwise R5 skips every short")
        records[name] = record
    return records


def build_context(config: dict, snapshot: dict, targets: dict, quotes: dict, eligibility: dict,
                  plan_orders: list, pending: list, executed: list, seen: list, evidence: dict,
                  now: datetime) -> dict:
    assertions = {**DEFAULT_ASSERTIONS, **(evidence.get("assertions") or {})}
    approval = evidence.get("approval") or {}
    return {"now": iso(now), "mode": config["mode"], "account_id": str(snapshot["account_id"]),
            "run_id": f"golive-{now.astimezone(ET).date().isoformat()}",
            "guard_session_id": approval.get("guard_session_id"), "snapshot": snapshot, "targets": targets,
            "expected_target_checksum": targets["checksum"], "quotes": quotes, "eligibility": eligibility,
            "open_orders": pending, "executed_order_ids": executed, "seen_client_order_ids": seen,
            "approved_plan_orders": plan_orders, "plan_checksum": digest(plan_orders), "scout": {},
            "daily_entry_halt": False, "blockers": [], "unverified_fields": [],
            "relay_assertion": RELAY_NOTE, "evidence_file": evidence.get("_file"), **assertions}




def bind_approvals(context: dict, order: dict, config: dict, approval: dict) -> dict:
    """Copy the operator's approval into the shapes R14 and R15 read, bound to this exact plan."""
    if not approval.get("issued_at") or not approval.get("expires_at"):
        return context
    base = {"verified_by_relay": approval.get("verified_by_relay") is True, "decision": "APPROVE",
            "issued_at": approval["issued_at"], "expires_at": approval["expires_at"],
            "mode": context["mode"], "account_id": context["account_id"]}
    context = dict(context)
    context["auditor_approval"] = {**base, "approval_id": f"{approval.get('approval_id')}-audit",
                                   "session_id": approval.get("auditor_session_id"),
                                   "target_checksum": context["targets"]["checksum"],
                                   "snapshot_checksum": digest(context["snapshot"]),
                                   "plan_checksum": context["plan_checksum"],
                                   "config_checksum": digest(config)}
    context["human_approvals"] = [{**base, "approval_id": f"{approval.get('approval_id')}-human",
                                   "order_fingerprint": order_fingerprint(order, context),
                                   "consumed": False}]
    return context


# ------------------------------------------------------------------ config and targets
def load_config(path: Path | None) -> dict:
    config = load_strategy_config("pstar", path)
    if config.get("strategy") != "pstar":
        raise GoliveError("golive only serves the deployed P* strategy")
    return config


def load_targets(path: Path, config: dict) -> dict:
    targets = read_json(path)
    if not isinstance(targets, dict) or type(targets.get("schema_version")) is not int \
            or targets["schema_version"] != PSTAR_SCHEMA_VERSION:
        raise GoliveError(f"{path.name} is not a sealed P* targets contract (schema "
                          f"{PSTAR_SCHEMA_VERSION}); publish one first (docs/GOLIVE.md, step 1)")
    if not verify(targets):
        raise GoliveError(f"{path.name} does not verify its own checksum")
    if targets.get("status") != "READY":
        raise GoliveError(f"{path.name} is {targets.get('status')}: {targets.get('hold_reason')}")
    if targets.get("mode") != config["mode"]:
        raise GoliveError(f"{path.name} is published for mode {targets.get('mode')!r} while the "
                          f"configuration is {config['mode']!r}: publish one for this mode")
    if targets.get("config_checksum") != digest(config):
        raise GoliveError(f"{path.name} was built from a different configuration: republish the targets "
                          f"after any config edit")
    return targets


def equity_reference(account: str, equity: float, now: datetime, initial: float) -> dict:
    """Peak and previous-close equity: the paper API reports neither, so this tool keeps its own history."""
    path = STATE_DIR / f"{account}.json"
    history = read_json(path) if path.exists() else {"sessions": []}
    today = now.astimezone(ET).date().isoformat()
    sessions = [row for row in history.get("sessions") or () if row.get("day") != today]
    earlier = [row["equity"] for row in sessions if finite(row.get("equity"))]
    peak = max([equity, initial, *earlier])
    sessions = (sessions + [{"day": today, "as_of": iso(now), "equity": equity}])[-60:]
    write_json(path, {"account_id": account, "initial_equity": initial, "peak_equity": peak,
                      "sessions": sessions,
                      "note": ("peak_equity and previous_close_equity are recorded by live/golive.py from "
                               "its own snapshots; the paper cash API reports neither")})
    return {"previous_close_equity": earlier[-1] if earlier else equity, "peak_equity": peak,
            "equity_reference": "live/golive.py state history, not a broker field"}


def journal_append(journal_dir: Path, kind: str, account: str, mode: str, now: datetime, reason: str,
                   action: str, symbol=None, intent=None, target_checksum=None, config=None,
                   run_id=None, strategy_id=None) -> dict:
    record = {"event_id": uuid.uuid4().hex, "timestamp": iso(now), "mode": mode, "account_id": account,
              "run_id": run_id or f"golive-{now.astimezone(ET).date().isoformat()}",
              "session_id": "desktop-chat", "agent": "golive", "kind": kind, "symbol": symbol,
              "strategy_id": strategy_id, "intent": intent, "action": action, "reason": reason,
              "rule_checks": [], "target_checksum": target_checksum, "order_fingerprint": None,
              "config_checksum": digest(config) if config else None}
    journal = Journal(Path(journal_dir))
    try:
        journal.append("decisions", record)
    finally:
        journal.close()
    return record


# ------------------------------------------------------------------ operator control and evidence
RELEASE_SCOPE = ("live", "rules", "journal")
APPROVAL_WINDOW_SECONDS = 240        # inside the frozen 300 s approval TTL, so R14 is the authority
LEASE_BASIS = ("local mode, decided by the user 2026-11-20: this Qoder Desktop chat is the only order "
               "sender; no cloud agent exists or has order tools")
PERMISSION_BASIS = ("automatic execution decided by the user on 2026-11-21: every order passes the "
                    "deterministic gate; the user can pause at any time with Slack `PAUSE`")
INDEPENDENT_CHECK_BASIS = ("automatic mode decided by the user 2026-11-21: the independent check is the "
                           "deterministic golive/gate run; the human controls are the pause switch and the "
                           "Slack alerts")
BORROW_PROXY = "sim_trade_max_buy_sell.max_sell_short_qty"
UNRESOLVED_STATES = ("WORKING", "PARTIAL", "UNKNOWN", "UNREADABLE")
# The other side of that list: a resolved row is closed at the broker, so it is neither a live order for
# R12 nor an open order in reconcile. CANCELLED_PARTIAL is here because a fill that was then cancelled is
# a finished fact, not something the next round has to wait for.
RESOLVED_STATES = ("FILLED", "CLOSED_NO_FILL", "CANCELLED_PARTIAL")


def run_git(run, argv: list[str]) -> str | None:
    """One git call, or None: an unavailable git or a non-repository is a failed check, never a pass."""
    try:
        done = run(["git", *argv], capture_output=True, text=True, timeout=30)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if getattr(done, "returncode", 1) != 0:
        return None
    return (done.stdout or "").strip()


def release_state(porcelain: str | None, head: str | None, scope=RELEASE_SCOPE) -> dict:
    """True only when no runtime .py differs from the commit that is checked out.

    `-uall` matters: without it git collapses an untracked directory to `?? live/`, whose path does not
    end in .py, and a wholly uncommitted runtime tree would read as clean. The config's `mode` and
    `execution_enabled` are deliberately not here - R10 already rejects targets whose `config_checksum`
    is not the config in use, so a mode flip cannot pass unnoticed.
    """
    if porcelain is None or head is None:
        return {"verified": False, "commit": None, "uncommitted": [],
                "reason": "git is unavailable or this working tree is not a repository, so the runtime "
                          "code cannot be tied to a commit: R17 stays unsatisfied"}
    paths = []
    for line in porcelain.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        # The last field is the path; on a rename it is the new name, which is the one that matters.
        path = fields[-1].strip('"')
        if path.endswith(".py"):
            paths.append(path)
    return {"verified": not paths, "commit": head, "uncommitted": sorted(paths),
            "reason": ("no uncommitted .py under " + ", ".join(f"{name}/" for name in scope)
                       + f" at commit {head}") if not paths else
                      f"{len(paths)} uncommitted .py file(s) under the runtime directories: "
                      f"{', '.join(sorted(paths))}"}


def release_check(run=subprocess.run, root: Path = ROOT) -> dict:
    porcelain = run_git(run, ["-C", str(root), "status", "--porcelain", "--untracked-files=all",
                              "--", *RELEASE_SCOPE])
    head = run_git(run, ["-C", str(root), "rev-parse", "HEAD"])
    return release_state(porcelain, head)


def snapshot_attestation(snapshot: dict, path: Path, account: str, mode: str) -> dict:
    """True only for a file this tool wrote from raw MCP responses and bound to this account by the API.

    Three things are checked, because `verified_by_relay` is what R11 spends: the shape only `command_
    snapshot` produces, the recorded source hashes recomputed from the files named in it, and the account
    assertion that only `sim_trade_account_list` can give.
    """
    reasons = []
    if not isinstance(snapshot, dict):
        return {"verified": False, "reasons": ["the snapshot file is not an object"]}
    if str(snapshot.get("account_id")) != account or snapshot.get("mode") != mode:
        reasons.append(f"the snapshot names {snapshot.get('account_id')!r} in mode {snapshot.get('mode')!r}, "
                       f"not {account} in mode {mode}")
    for key in ("source", "broker_fields", "account_binding", "as_of", "generated_at"):
        if key not in snapshot:
            reasons.append(f"no {key} field, so this is not a file `golive.py snapshot` wrote")
    if snapshot.get("relay_assertion") != RELAY_NOTE:
        reasons.append("the relay assertion line is missing or edited")
    binding = snapshot.get("account_binding") or {}
    if binding.get("binding") != "api-asserted" or account not in (binding.get("asserted_by_api") or ()):
        reasons.append("the account is not asserted by `sim_trade_account_list`: snapshot with --accounts, "
                       "otherwise the binding is the operator's own declaration")
    for name, row in (snapshot.get("source") or {}).items():
        if not isinstance(row, dict) or not row.get("file_full"):
            reasons.append(f"source {name} records no path")
            continue
        try:
            if sha_file(Path(row["file_full"])) != row.get("sha256"):
                reasons.append(f"source {name} no longer hashes to what the snapshot recorded "
                               f"({relative(row['file_full'])})")
        except OSError:
            reasons.append(f"source {name} cannot be re-read ({relative(row['file_full'])})")
    return {"verified": not reasons, "reasons": reasons}


def reconcile_attestation(history: list, now: datetime) -> dict:
    """`reconciled` is computed from the fresh history, never typed: nothing may be open or unreadable."""
    rows = [{"order_id": row.get("order_id"), "symbol": row.get("symbol"), "side": row.get("side"),
             "state": row.get("state"), "status_code": row.get("status_code"),
             "quantity": row.get("quantity"), "filled": row.get("filled"),
             "client_order_id": row.get("client_order_id")}
            for row in history if row.get("state") in UNRESOLVED_STATES]
    if not rows:
        return {"reconciled": True, "unresolved": [],
                "reason": "the history has no open, unknown or cancel-pending order "
                          f"({len(history)} row(s) read at {iso(now)})"}
    return {"reconciled": False, "unresolved": rows,
            "reason": "{} order(s) are still open or unreadable: {}; an unmapped status code stays "
                      "unresolved until the operator proves it with --working, and R12 holds the batch "
                      "meanwhile".format(
                          len(rows), ", ".join("{} {} (code {})".format(
                              row["symbol"], str(row["state"]).lower(), row["status_code"])
                              for row in rows))}


# ------------------------------------------------------------------ the shared pause switch
LOCAL_LEASE = "local-entry"
CLOUD_LEASE = "cloud-executor"


def switch_cache_path() -> Path:
    """Where the last read of the shared flag is cached, next to the equity history this tool keeps."""
    return STATE_DIR / "switch.json"


def load_switch_ops(config: dict, targets: dict | None = None, path=None) -> dict:
    """The sentinel registry from live/ops.json, checked against the tradable book before anything uses it."""
    try:
        ops = pause_switch.load_ops(path)
    except pause_switch.SwitchError as exc:
        raise GoliveError(f"{exc}: with no usable switch registry no entry round can be checked, so nothing "
                          f"is planned") from exc
    problems = pause_switch.validate(ops, config or {}, (targets or {}).get("weights"))
    if problems:
        raise GoliveError("live/ops.json: " + "; ".join(problems))
    return ops


def switch_of(args, ops: dict, config: dict, now: datetime, intent: str = "ENTRY",
              holder: str | None = None) -> dict:
    """What the shared flag says, and whether this round may send, from the file the operator fetched.

    `--flag` names the raw `quote_user_security` response; with no `--flag` the cached read written by
    `golive.py flag` is used. A flag that is present but stale, contradictory or unreadable fails closed:
    entries stop. A flag nobody fetched is reported as unread and does not stop the local run, because the
    watchlist group has to be provisioned by the user in the app first and the switch cannot be invented
    from here; a cloud-side run is the exception, since there `lease_verified` comes from this same read.
    """
    path = Path(args.flag) if getattr(args, "flag", None) else switch_cache_path()
    path = path if path.exists() else None
    source, captured = None, None
    if path is not None:
        source = read_json(path)
        captured = None
        if isinstance(source, dict) and "codes" in source:
            captured = source.get("read_at")
        if captured is None:
            captured = provenance(path)["captured_at"]
    state = pause_switch.switch_state(source, ops, now, captured_at=captured,
                                      max_age_seconds=config["account_max_age_seconds"])
    decision = pause_switch.may_trade(state, ops, intent, holder)
    if switch_unread(path) and holder != CLOUD_LEASE and decision["allow"] is False:
        # Nobody fetched the flag. The local run may still go: the group is created by the user in the app,
        # this chat is the lease holder for the 2026-11-23 entry, and the gap is stated in the plan, the
        # journal and docs/GOLIVE.md. A cloud run has no such basis, so the refusal above stands for it.
        decision = {"allow": True, "intent": intent, "switch": "unread", "overridden": True,
                    "reasons": [f"the switch was never read ({state['reasons'][0] if state['reasons'] else 'no file'}): "
                                f"{holder} proceeds with the gap recorded - fetch the group and run "
                                f"`golive.py flag --read` before the first submit"]}
    return {"group": pause_switch.registry(ops)["group"], "intent": intent, "holder": holder,
            "file": relative(path) if path else None, "source": provenance(path) if path else None,
            "unread": switch_unread(path), "state": state, "decision": decision,
            "read_command": pause_switch.read_payload(ops)}


def switch_unread(path) -> bool:
    """True when there is no flag file at all, as against a file that says something unusable."""
    return path is None


def switch_summary(switch: dict) -> str:
    state = switch["state"]
    return (f"switch {switch['group']}: {'unread' if switch['unread'] else state['state']}"
            f"{'' if not state.get('lease_holder') else f', lease {state['lease_holder']} to ' + str(state['lease_expires_at'])}"
            f" -> {switch['decision']['reasons'][0]}")


def switch_artifact(args, now: datetime, account: str, mode: str, config: dict, switch: dict,
                    history: list) -> dict:
    """The pause path a blocked round has to take: the cancel list, the switch write, and what to run."""
    cancel = working_strategy_orders(history, account)
    ops = load_switch_ops(config, None, getattr(args, "ops", None))
    writes = None
    if not switch["unread"] and switch["state"]["state"] == pause_switch.PAUSED:
        writes = {"note": "the flag already says paused: nothing to write", "writes": []}
    else:
        try:
            plan = pause_switch.write_plan({"state": pause_switch.PAUSED, "lease": None}, ops, now,
                                           current=set(switch["state"].get("codes") or ()))
            out = artifact_dir(now, account) / f"flag-write-{now:%H%M%S}.json"
            write_json(out, {**plan, "as_of": iso(now), "requested_by": "the switch check blocked a round"})
            writes = {"file": relative(out), **plan}
        except pause_switch.SwitchError as exc:
            writes = {"error": str(exc)}
    record = {"blocked_at": iso(now), "mode": mode, "account_id": account, "switch": switch,
              "cancel": cancel, "switch_write": writes,
              "do": (f"python -m live.golive pause --account {account} --orders <today's order history> "
                     f"--source \"<where the PAUSE came from>\", send every cancel payload, then reconcile: "
                     f"docs/GOLIVE.md 'The pause switch'"),
              "note": ("no new order was planned: the shared switch blocks entries, and a FINAL_EXIT or "
                       "EMERGENCY_EXIT reduction still passes whenever the switch says what it says")}
    out = artifact_dir(now, account) / f"switch-hold-{now:%H%M%S}.json"
    write_json(out, record)
    journal_append(args.journal, "GOLIVE_SWITCH_HOLD", account, mode, now,
                   switch["decision"]["reasons"][0], "SWITCH_HOLD", config=config)
    print(f"SWITCH HOLD {relative(out)}")
    print(f"  {switch_summary(switch)}")
    print(f"  {len(cancel)} live pstar- order(s) to cancel in the history given:")
    for row in cancel[:10]:
        print(f"    sim_trade_cancel_order acc_id={account} market={MARKET_ID} order_id={row['order_id']} "
              f"({row['symbol']} {row['side']}, {row['remaining']} of {row['quantity']} left)")
    if writes and writes.get("writes"):
        for payload in writes["writes"]:
            print(f"    {payload['tool']} {json.dumps(payload['payload'], sort_keys=True)}")
    print(f"  run: {record['do']}")
    return record


def command_flag(args) -> int:
    """Read the shared switch, cache what it says, and write the payloads that would change it.

    This is the only command that touches the switch: `pause`/`resume` latch the durable kill switch and
    point at the file this command writes, and the Coordinator sends the payloads from a Slack `PAUSE`.
    Nothing here places an order or writes the watchlist: the agent sends what is printed.
    """
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    config = load_config(args.config)
    targets = load_targets(args.targets, config) if args.targets else None
    ops = load_switch_ops(config, targets, args.ops)
    if not args.set and not args.read:
        raise GoliveError("`flag` needs --read <raw quote_user_security response> to check the switch, or "
                          "--set paused|running to write it")
    state = None
    if args.read:
        raw = read_json(args.read)
        mark = provenance(args.read)
        state = pause_switch.switch_state(raw, ops, now, captured_at=mark["captured_at"],
                                          max_age_seconds=config["account_max_age_seconds"])
        write_json(switch_cache_path(), {"account_id": account, "group": state["group"],
                                         "read_at": mark["captured_at"], "codes": state["codes"],
                                         "state": state["state"], "lease_holder": state["lease_holder"],
                                         "lease_status": state["lease_status"],
                                         "lease_expires_at": state["lease_expires_at"],
                                         "reasons": state["reasons"], "source": mark,
                                         "relay_note": RELAY_NOTE})
    elif switch_cache_path().exists():
        state = pause_switch.switch_state(read_json(switch_cache_path()), ops, now,
                                         max_age_seconds=config["account_max_age_seconds"])
    if state is None:
        raise GoliveError("the switch has never been read: fetch the watchlist group and pass --read")
    writes = None
    if args.set:
        lease = None if args.lease in (None, "none") else args.lease
        plan = pause_switch.write_plan({"state": args.set, "lease": lease}, ops, now,
                                       current=set(state["codes"]))
        out = artifact_dir(now, account) / f"flag-write-{now:%H%M%S}.json"
        write_json(out, {**plan, "as_of": iso(now), "source": args.source or "operator",
                         "read_back": pause_switch.read_payload(ops), "observed_before_write": state["codes"]})
        writes = {"file": relative(out), **plan}
        if not plan["writes"]:
            print(f"FLAG {args.set} (lease {plan['lease']}): the watchlist already reads as intended, no write")
        else:
            print(f"FLAG {args.set} (lease {plan['lease']}): send these, then re-read and confirm")
            for payload in plan["writes"]:
                print(f"  {payload['tool']} {json.dumps(payload['payload'], sort_keys=True)}")
            print(f"  {relative(out)}: {', '.join(plan['expected_codes'])} is what the re-read must return")
    entry = pause_switch.may_trade(state, ops, "ENTRY", args.holder or LOCAL_LEASE)
    cloud = pause_switch.may_trade(state, ops, "ENTRY", CLOUD_LEASE)
    reason = f"{pause_switch.slack_text(state, args.source or 'golive.py flag')}: entry allowed=" \
             f"{entry['allow']}, cloud allowed={cloud['allow']}"
    journal_append(args.journal, "GOLIVE_FLAG", account, mode, now, reason,
                   "FLAG_SET" if args.set else "FLAG_READ", config=config)
    print(f"  {switch_summary({'group': state['group'], 'unread': False, 'state': state,
                              'decision': entry})}")
    print(f"  codes: {' '.join(state['codes']) or 'none'}")
    for line in state["reasons"]:
        print(f"    {line}")
    print(f"  ENTRY as {args.holder or LOCAL_LEASE}: {entry['allow']} ({entry['reasons'][0]})")
    print(f"  ENTRY as {CLOUD_LEASE}: {cloud['allow']} ({cloud['reasons'][0]})")
    print(f"  FINAL_EXIT: {pause_switch.may_trade(state, ops, 'FINAL_EXIT')['allow']}, EMERGENCY_EXIT: "
          f"{pause_switch.may_trade(state, ops, 'EMERGENCY_EXIT')['allow']} (a reduction never waits for "
          f"the switch)")
    if writes:
        print(f"  {writes['note']}")
    return 0


def mirror_switch(args, now: datetime, account: str, config: dict, desired: dict, source: str) -> dict:
    """The shared-flag writes that belong with a local latch: pausing here has to pause the cloud too.

    Without a flag read the opposite sentinel cannot be removed, so the artifact says what was added and
    that the re-read has to clear the rest: a half-written watchlist flag is how a switch ends up ambiguous.
    """
    ops = load_switch_ops(config, None, getattr(args, "ops", None))
    switch = switch_of(args, ops, config, now, "ENTRY", LOCAL_LEASE)
    codes = None if switch["unread"] else set(switch["state"].get("codes") or ())
    try:
        plan = pause_switch.write_plan(desired, ops, now, current=codes)
    except pause_switch.SwitchError as exc:
        return {"error": str(exc), "switch": switch["state"]}
    if switch["unread"]:
        plan["unremoved"] = ("the flag was never read: the opposite sentinel may still be in the group, so "
                             "re-read it after sending and MOVE_OUT whatever else is there")
    out = artifact_dir(now, account) / f"flag-write-{now:%H%M%S}.json"
    write_json(out, {**plan, "as_of": iso(now), "source": source, "observed": switch["state"]["codes"],
                     "read_back": pause_switch.read_payload(ops)})
    return {"file": relative(out), "writes": plan["writes"], "expected_codes": plan["expected_codes"],
            "unread": switch["unread"], "switch": switch["state"]}


def print_switch(mirrored: dict) -> None:
    if mirrored.get("error"):
        print(f"  shared switch NOT written: {mirrored['error']}")
        return
    if not mirrored["writes"]:
        print(f"  shared switch already reads {mirrored['switch']['state']}: {mirrored['file']}")
        return
    print(f"  shared switch: send these, then re-read {mirrored['file']}")
    for payload in mirrored["writes"]:
        print(f"    {payload['tool']} {json.dumps(payload['payload'], sort_keys=True)}")
    print(f"    the re-read must return exactly {', '.join(mirrored['expected_codes'])}")


def command_resume(args) -> int:
    """Put the current mode's account back in run, with the source recorded and no typed reply.

    Automatic execution (the user decided this on 2026-11-21) removed the `arm` step and the reply it
    demanded. What stays is that only the account of the config's own mode can be switched, and that this
    command writes to the control store and nothing else: no order, no schedule, no cloud change.
    """
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    config = load_config(args.config)
    if not str(args.source or "").strip():
        raise GoliveError("the pause switch needs its provenance: --source is what tells an auditor "
                          "that a Slack message, not a stray keystroke, stopped the book")
    if config.get("mode") != mode:
        raise GoliveError(f"the config mode is {config.get('mode')!r} but {account} is the {mode} account; "
                          f"R1 rejects every account except that mode's own")
    reason = f"resumed at {iso(now)}: {args.source}"
    journal = Journal(Path(args.journal))
    try:
        journal.set_resume(account, reason)
    finally:
        journal.close()
    journal_append(args.journal, "GOLIVE_RESUME", account, mode, now, reason, "RESUME", config=config)
    state = kill_switch_state(args.journal, account)
    print(f"RESUME {account} ({mode}): halted={state['halted']} - {reason}")
    print("  Nothing was sent: every ticket still passes the whole rule set, and pause remains available.")
    print_switch(mirror_switch(args, now, account, config, {"state": pause_switch.RUNNING, "lease": None},
                               args.source))
    return 0


def working_strategy_orders(history: list[dict], account: str) -> list[dict]:
    """The live orders this strategy placed, as complete `sim_trade_cancel_order` payloads."""
    return [{"order_id": row["order_id"], "symbol": row["symbol"], "side": row["side"],
             "quantity": row["quantity"], "filled": row["filled"], "remaining": row["remaining"],
             "state": row["state"], "status_code": row["status_code"],
             "client_order_id": row["client_order_id"],
             "tool": "sim_trade_cancel_order",
             "payload": {"acc_id": account, "market": MARKET_ID, "order_id": row["order_id"]}}
            for row in history
            if str(row.get("client_order_id") or "").startswith(STRATEGY_ORDER_PREFIX)
            and row.get("state") in ("WORKING", "PARTIAL")]


def command_pause(args) -> int:
    """Halt the current mode's account and list the working strategy orders the agent must cancel.

    `halt` keeps its meaning as an alias of this command. The latch is the R9/R16 kill switch, so a paused
    account rejects every entry ticket and still allows a FINAL_EXIT or EMERGENCY_EXIT reduction - which is
    how the scheduled final exit can close a book the user has paused. This writes nothing to the broker.
    """
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    config = load_config(args.config)
    if not str(args.source or "").strip():
        raise GoliveError("the pause switch needs its provenance: --source is what tells an auditor "
                          "that a Slack message, not a stray keystroke, stopped the book")
    if config.get("mode") != mode:
        raise GoliveError(f"the config mode is {config.get('mode')!r} but {account} is the {mode} account; "
                          f"R1 rejects every account except that mode's own")
    reason = f"paused at {iso(now)}: {args.source}"
    cancel: list[dict] = []
    out = None
    if args.orders:
        # Read the cancel list before latching: a history file that does not belong to this account must
        # leave the control exactly as it was, rather than pause the book with no way to finish the job.
        raw_orders = read_json(args.orders)
        named: set = set()
        account_ids(raw_orders, named)
        wrong = sorted(named - {account})
        if wrong:
            raise GoliveError(f"{args.orders}: the order history names account {', '.join(wrong)}, not "
                              f"{account}: pausing the wrong account would cancel the wrong book")
        history = parse_orders(data_of(raw_orders, "sim_trade_history_order_list", args.orders),
                               set(args.working or ()))
        cancel = working_strategy_orders(history, account)
        out = Path(args.out) if args.out else artifact_dir(now, account) / f"cancel-{now:%H%M%S}.json"
    journal = Journal(Path(args.journal))
    try:
        journal.set_halt(account, reason)
    finally:
        journal.close()
    if out is not None:
        write_json(out, {"paused_at": iso(now), "mode": mode, "account_id": account, "source": args.source,
                         "cancel": cancel,
                         "note": ("send every payload here with sim_trade_cancel_order now, then reconcile; "
                                  "the halt already rejects any new entry ticket"),
                         "orders_source": provenance(args.orders)})
    journal_append(args.journal, "GOLIVE_PAUSE", account, mode, now, reason, "PAUSE", config=config)
    state = kill_switch_state(args.journal, account)
    print(f"PAUSE {account} ({mode}): halted={state['halted']} - {reason}")
    if args.orders:
        print(f"  {relative(out)}: {len(cancel)} working strategy order(s) to cancel, send them now")
        for row in cancel:
            print(f"    sim_trade_cancel_order acc_id={account} market={MARKET_ID} "
                  f"order_id={row['order_id']} ({row['symbol']} {row['side']}, {row['remaining']} of "
                  f"{row['quantity']} left, {row['client_order_id']})")
        if not cancel:
            print("    none: no pstar- order is live in that history file")
    else:
        print("  no --orders file given, so no cancel list was written: fetch today's order history and "
              "run pause again with it - the halt is already latched")
    print("  R9 and R16 now reject every entry ticket for this account; only a FINAL_EXIT or "
          "EMERGENCY_EXIT reduction passes, so the scheduled final exit still closes the book.")
    print_switch(mirror_switch(args, now, account, config, {"state": pause_switch.PAUSED, "lease": None},
                               args.source))
    return 0


# The only three things that start a cloud run, decided by the user on 2026-11-21: a schedule, the user's own
# Slack PAUSE/RESUME, or a risk verdict the Auditor measured. Free text is accepted from the local chat, where
# the operator is the one typing; in the cloud it would be a human approval wearing a costume.
CLOUD_TRIGGERS = (("scheduled run", re.compile(r"^scheduled run(?:[ :][A-Za-z0-9._/-]+)*$")),
                  ("slack switch", re.compile(r"^slack (?:pause|resume) \S+$", re.I)),
                  ("risk action", re.compile(r"^risk (?:kill|alert) \S+$", re.I)))
CLOUD_EXECUTOR_ROLE = "executor"       # the role name agents/templates.json must call the order sender


def cloud_trigger_check(trigger: str) -> tuple[bool, str]:
    for name, pattern in CLOUD_TRIGGERS:
        if pattern.match(str(trigger or "").strip()):
            return True, name
    return False, (f"{trigger!r} is not one of the cloud triggers: 'scheduled run [job]', "
                   f"'Slack PAUSE <ts>'/'Slack RESUME <ts>' or 'risk KILL|ALERT <report>'")


def cloud_release_check(templates_path: Path, lock_path: Path) -> dict:
    """`release_verified` in cloud mode: the skill versions pinned on the templates are the sealed ones.

    A cloud session has no git checkout, so the local proof that no runtime .py differs from HEAD cannot run
    there. What replaces it is the pin: every skill a role loads has to name the exact cloud skill id and
    version recorded in `agents/release-lock.json`. A role pointed at `latest` is not running the release that
    was reviewed, and a pin nobody sealed is unverifiable.
    """
    templates, lock = read_json(templates_path), read_json(lock_path)
    if not isinstance(templates, dict) or not isinstance(lock, dict):
        return {"verified": False, "commit": None, "uncommitted": [], "pinned": {},
                "reason": f"{templates_path} or {lock_path} is not a JSON object"}
    reasons, pinned = [], {}
    if templates.get("skills_are_pinned") is not True:
        reasons.append(f"{templates_path.name} does not declare skills_are_pinned true, so a role could "
                       f"resolve to latest")
    versions = templates.get("skill_versions") or {}
    sealed = lock.get("skills") or {}
    for role, row in sorted((templates.get("roles") or {}).items()):
        for name in sorted((row or {}).get("skills") or ()):
            pin, want = versions.get(name), sealed.get(name) or {}
            if not isinstance(pin, dict) or not pin.get("cloud_skill_id") or not pin.get("version"):
                reasons.append(f"{role}: skill {name} names no cloud skill id and version, which is `latest`")
                continue
            if not want.get("cloud_skill_id") or not want.get("expected_cloud_version"):
                reasons.append(f"{name}: agents/release-lock.json seals no cloud skill id/version, so "
                               f"{pin['cloud_skill_id']}@{pin['version']} cannot be checked against anything")
                continue
            if (str(pin["cloud_skill_id"]), str(pin["version"])) != (str(want["cloud_skill_id"]),
                                                                     str(want["expected_cloud_version"])):
                reasons.append(f"{role}: skill {name} pins {pin['cloud_skill_id']}@{pin['version']} but the "
                               f"release lock sealed {want['cloud_skill_id']}@"
                               f"{want['expected_cloud_version']}")
                continue
            pinned[f"{role}/{name}"] = f"{pin['cloud_skill_id']}@{pin['version']}"
    return {"verified": not reasons, "commit": lock.get("release_name"), "uncommitted": [], "pinned": pinned,
            "reason": (f"cloud mode: {len(pinned)} skill pins on {relative(templates_path)} match "
                       f"{relative(lock_path)} ({lock.get('release_name')}, revision "
                       f"{lock.get('local_revision')}); git is not the evidence a cloud session has"
                       if not reasons else "; ".join(reasons))}


def cloud_tool_policy_check(templates_path: Path, executor: str) -> dict:
    """`permission_policy_verified`: the platform refused the order tools, it was not merely asked to.

    `desired_order_tools` in the templates is a wish list. It only counts as a policy once somebody has asked
    a non-Executor role to place an order and watched the platform refuse, which is what `tool_policy` records.
    Until that observation exists the flag is false with the reason, so a cloud run either stops or goes out
    with the limitation named in the plan - never with the denial assumed.
    """
    templates = read_json(templates_path)
    templates = templates if isinstance(templates, dict) else {}
    policy = templates.get("tool_policy") or {}
    traders = sorted(name for name, row in sorted((templates.get("roles") or {}).items())
                     if (row or {}).get("desired_order_tools"))
    reasons = []
    if traders != [policy.get("order_role")]:
        reasons.append(f"order tools are listed for {', '.join(traders) or 'no role'}; the policy names "
                       f"{policy.get('order_role')!r} as the only one")
    if executor not in traders:
        reasons.append(f"the {executor} role lists no order tools, so it could not execute a plan")
    if policy.get("platform_denial_verified") is not True:
        reasons.append("no observed platform refusal of an order tool outside the Executor is recorded in "
                       f"{templates_path.name} (tool_policy.platform_denial_verified)")
    if not policy.get("observed_at"):
        reasons.append("tool_policy records no observation time")
    return {"verified": not reasons, "reasons": reasons, "order_roles": traders,
            "basis": (f"platform policy observed {policy['observed_at']}: {executor} is the only role with "
                      f"sim_trade_input_order/cancel; every other role was refused" if not reasons
                      else "limitation: " + "; ".join(reasons))}


def cloud_lease_check(args, ops: dict, config: dict, now: datetime, exempt: str | None) -> dict:
    """`lease_verified`: the shared flag says the cloud holds the lease or may take it.

    One order stream per book, decided by the user on 2026-11-21: the 2026-11-23 entry runs in the local
    Qoder Desktop chat under a `local-entry` lease that expires 16:00 ET, so a scheduled cloud run may act
    only when no unexpired lease of that kind exists. A Slack PAUSE cancel and a KILL flatten are the two
    actions that never wait for it, and `exempt` carries that statement when the caller knows it.
    """
    if exempt:
        return {"verified": True, "basis": exempt, "switch": None}
    if not getattr(args, "flag", None):
        return {"verified": False,
                "basis": (f"cloud mode needs --flag: the raw quote_user_security read of "
                          f"{pause_switch.registry(ops)['group']} is the only lease evidence a cloud session "
                          f"has"), "switch": None}
    switch = switch_of(args, ops, config, now, "ENTRY", CLOUD_LEASE)
    state, allow = switch["state"], switch["decision"]["allow"]
    if state.get("lease_holder") == CLOUD_LEASE and state.get("lease_status") == pause_switch.LEASED:
        detail = f"the {CLOUD_LEASE} lease is held and runs to {state.get('lease_expires_at')}"
    elif allow:
        detail = (f"no unexpired lease is held (lease {state.get('lease_status')}), so {CLOUD_LEASE} may take "
                  f"it for this batch")
    else:
        detail = switch["decision"]["reasons"][0]
    return {"verified": bool(allow), "basis": f"{switch['file']} read at {state.get('read_at')}: {detail}",
            "switch": {key: value for key, value in switch.items() if key != "read_command"}}


def command_evidence(args) -> int:
    """Compute the evidence file: every field is measured here, so the chat agent never types it."""
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    side = str(getattr(args, "side", "local") or "local").strip().lower()
    if side not in ("local", "cloud"):
        raise GoliveError(f"--side {side!r}: evidence is either local (this Desktop chat, which holds the "
                          f"lease and the human trigger) or cloud (the Executor session, which needs the "
                          f"skill pins, the shared flag and two different sessions)")
    config = load_config(args.config)
    if config.get("mode") != mode:
        raise GoliveError(f"the config mode is {config.get('mode')!r} but {account} is the {mode} account; "
                          f"R1 rejects every account except that mode's own")
    snapshot = read_json(args.snapshot)
    snapshot_ok = snapshot_attestation(snapshot if isinstance(snapshot, dict) else {}, args.snapshot,
                                       account, mode)
    raw_orders = read_json(args.orders)
    named: set = set()
    account_ids(raw_orders, named)
    wrong = sorted(named - {account})
    if wrong:
        raise GoliveError(f"{args.orders}: the order history names account {', '.join(wrong)}, not {account}")
    history = parse_orders(data_of(raw_orders, "sim_trade_history_order_list", args.orders),
                           set(args.working or ()))
    ops = load_switch_ops(config, None, args.ops)
    reconciled = reconcile_attestation(history, now)
    risk_level = None
    if getattr(args, "risk_report", None):
        report = read_json(args.risk_report)
        if not isinstance(report, dict):
            raise GoliveError(f"{args.risk_report}: the risk report is not a JSON object")
        risk_level = str(report.get("level") or "").upper()
    approval_id = (f"golive-{now.astimezone(ET).date().isoformat()}-{account}-"
                   f"{now.astimezone(ET):%H%M%S}")
    expires = now + timedelta(seconds=APPROVAL_WINDOW_SECONDS)
    if side == "cloud":
        templates = Path(getattr(args, "templates", None) or ROOT / "agents" / "templates.json")
        lock = Path(getattr(args, "release_lock", None) or ROOT / "agents" / "release-lock.json")
        auditor, executor = str(args.auditor_session or ""), str(args.executor_session or "")
        ok, kind = cloud_trigger_check(args.trigger)
        if not ok:
            raise GoliveError(f"--side cloud: {kind}")
        if not auditor or not executor:
            raise GoliveError("--side cloud needs --auditor-session and --executor-session: R14 fails an "
                              "approval whose auditor and executor are the same session, and a cloud role has "
                              "no human typing on either side")
        if auditor == executor:
            raise GoliveError(f"--side cloud: one session both measuring and executing is not an independent "
                              f"check (R14): auditor and executor are both {auditor}")
        release = cloud_release_check(templates, lock)
        policy = cloud_tool_policy_check(templates, CLOUD_EXECUTOR_ROLE)
        # The two batches that must not wait for a lease the other executor is holding: a KILL flatten and
        # the cancels a Slack PAUSE asks for. Both only take risk off, which is what the lease protects.
        exempt = (f"a {risk_level} batch flattens the book: it never waits for the lease, and the plan itself "
                  f"sets the shared switch" if risk_level == "KILL" else
                  ("a Slack PAUSE batch only cancels live orders and holds the book, which is the whole point "
                   "of the pause, so it never waits for the lease"
                   if kind == "slack switch" and "pause" in str(args.trigger).lower() else None))
        lease = cloud_lease_check(args, ops, config, now, exempt)
        sources = {"templates": provenance(templates), "release_lock": provenance(lock)}
        if args.risk_report:
            sources["risk_report"] = provenance(args.risk_report)
        if lease.get("switch"):
            sources["flag"] = lease["switch"]["source"]
    else:
        templates = lock = None
        release = release_check()
        policy = {"verified": True, "reasons": [], "order_roles": ["qoder-desktop-chat"],
                  "basis": PERMISSION_BASIS}
        lease = {"verified": True, "basis": LEASE_BASIS, "switch": None}
        sources = {}
    evidence = {
        "created_by": "live/golive.py evidence", "mode": mode, "account_id": account, "side": side,
        "generated_at": iso(now), "trigger": args.trigger,
        "assertions": {
            "release_verified": release["verified"], "lease_verified": lease["verified"],
            "permission_policy_verified": policy["verified"], "reconciled": reconciled["reconciled"],
            "snapshot_verified_by_relay": snapshot_ok["verified"]},
        "bases": {
            "release_verified": release["reason"],
            "lease_verified": lease["basis"],
            "permission_policy_verified": policy["basis"],
            "reconciled": reconciled["reason"],
            "snapshot_verified_by_relay": ("bound to " + account + " through sim_trade_account_list and "
                                           "re-hashed from its own source files" if snapshot_ok["verified"]
                                           else "; ".join(snapshot_ok["reasons"])),
            "approval": INDEPENDENT_CHECK_BASIS},
        "approval": {"approval_id": approval_id, "issued_at": iso(now), "expires_at": iso(expires),
                     "verified_by_relay": True,
                     "auditor_session_id": (args.auditor_session if side == "cloud"
                                            else "golive-deterministic-checks"),
                     "guard_session_id": (args.executor_session if side == "cloud" else "qoder-desktop-chat"),
                     "confirmed_by": ("the deterministic gate, decided by the user on 2026-11-21: no typed "
                                      "approval exists in cloud mode" if side == "cloud"
                                      else "user-started run"),
                     "trigger": args.trigger, "basis": INDEPENDENT_CHECK_BASIS,
                     "window_seconds": APPROVAL_WINDOW_SECONDS},
        "borrow": {"easy_to_borrow_proxy": BORROW_PROXY,
                   "accepted_by": "the user on 2026-11-20: the paper tools expose no easy-to-borrow flag "
                                  "or pool depth, so a short passes only if max_sell_short_qty is at least "
                                  "its planned quantity"},
        "release": release, "reconcile": reconciled, "snapshot": snapshot_ok, "tool_policy": policy,
        "lease": {key: value for key, value in lease.items() if key != "switch"},
        "sources": {"snapshot": provenance(args.snapshot), "orders": provenance(args.orders),
                    "config": {"file": str(args.config) if args.config else "live/config.pstar.json",
                               "checksum": digest(config)}, **sources,
                    **({"risk_report_level": risk_level} if risk_level else {})},
        "relay_note": RELAY_NOTE}
    out = Path(args.out)
    write_json(out, evidence)
    flags = evidence["assertions"]
    journal_append(args.journal, "GOLIVE_EVIDENCE", account, mode, now,
                   f"{side}: release " + str(flags["release_verified"]) + ", lease "
                   + str(flags["lease_verified"]) + ", reconciled "
                   + str(flags["reconciled"]) + ", snapshot relay "
                   + str(flags["snapshot_verified_by_relay"]) + f", approval {approval_id} until "
                   f"{iso(expires)}", "EVIDENCE", config=config)
    print(f"EVIDENCE {relative(out)} ({side} mode)")
    print(f"  release_verified {flags['release_verified']}: {release['reason']} "
          f"(commit {release['commit'] or 'unknown'})")
    print(f"  lease_verified {flags['lease_verified']}: {lease['basis']}")
    print(f"  permission_policy_verified {flags['permission_policy_verified']}: {policy['basis']}")
    print(f"  reconciled {flags['reconciled']} ({reconciled['reason']})")
    print(f"  snapshot_verified_by_relay {flags['snapshot_verified_by_relay']} "
          f"({'; '.join(snapshot_ok['reasons']) or 'as recorded'})")
    print(f"  approval {approval_id} valid {iso(now)} to {iso(expires)} "
          f"(auditor {evidence['approval']['auditor_session_id']} != executor "
          f"{evidence['approval']['guard_session_id']}; R14 fails past {iso(expires)}, and so does an expired "
          f"quote: rerun `evidence` and `plan`, neither of which sends anything)")
    print(f"  borrow proxy {BORROW_PROXY}")
    return 0 if all(flags.values()) else 2


# ------------------------------------------------------------------ snapshot
def command_snapshot(args) -> int:
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    config = load_config(args.config)
    if config.get("mode") != mode:
        raise GoliveError(f"the config mode is {config.get('mode')!r} but {account} is the {mode} account; "
                          f"R1 rejects every account except that mode's own")
    cash_raw, position_raw = read_json(args.cash), read_json(args.positions)
    order_raw = read_json(args.orders) if args.orders else None
    account_raw = read_json(args.accounts) if args.accounts else None
    binding = bind_account([item for item in (cash_raw, position_raw, order_raw) if item is not None],
                           account, listing=account_raw)

    cash = parse_cash(data_of(cash_raw, "sim_trade_cash_info", args.cash))
    positions, row_keys = parse_positions(data_of(position_raw, "sim_trade_position_list", args.positions))
    orders = parse_orders(data_of(order_raw, "sim_trade_history_order_list", args.orders)) \
        if order_raw is not None else None
    account_type = market_id = None
    if account_raw is not None:
        listed = data_of(account_raw, "sim_trade_account_list", args.accounts).get("accounts") or ()
        for row in listed:
            if text(row.get("account_id")) == account:
                account_type, market_id = row.get("account_type"), row.get("market_id")
        if account_type is None:
            raise GoliveError(f"{args.accounts}: account {account} is not in the paper account list")
        if market_id != MARKET_ID:
            raise GoliveError(f"account {account} is market {market_id}, not the US paper market {MARKET_ID}")

    long_gross, short_gross = cash["long_gross"], cash["short_gross"]
    if long_gross is None or short_gross is None:
        rows = [row for row in positions if row["market_value"] is not None]
        long_gross = sum(row["market_value"] for row in rows if row["quantity"] > 0) \
            if long_gross is None else long_gross
        short_gross = -sum(row["market_value"] for row in rows if row["quantity"] < 0) \
            if short_gross is None else short_gross
    margin_dollars = (config["margin_long_factor"] * long_gross + config["margin_short_factor"] * short_gross
                      ) if finite(long_gross) and finite(short_gross) else None
    margin = (margin_dollars / cash["equity"]
              if margin_dollars is not None and finite(cash["equity"]) and cash["equity"] else None)
    zero_rows = [row for row in positions if not row["quantity"]]
    positions = [row for row in positions if row["quantity"]]
    if any(row["price"] is None for row in positions):
        raise GoliveError("a position row has no usable price: "
                          f"{[row['symbol'] for row in positions if row['price'] is None]}; the risk "
                          f"monitor cannot measure an unpriced holding")
    if len({row["symbol"] for row in positions}) != len(positions):
        raise GoliveError("duplicate position symbol in the broker rows")

    reference = equity_reference(account, cash["equity"], now, config["initial_equity"])
    snapshot = {"mode": mode, "account_id": account, "account_type": account_type,
                "broker_account_type": account_type,
                "account_type_claim": ("SIMULATE (paper) per sim_trade_account_list account_type "
                                       f"{account_type}" if account_type in PAPER_ACCOUNT_TYPES else
                                       "unknown: no --accounts evidence file"),
                "market_id": market_id, "as_of": provenance(args.cash)["captured_at"],
                "as_of_source": f"mtime of {Path(args.cash).name}: sim_trade_cash_info carries no timestamp",
                "generated_at": iso(now), "equity": cash["equity"],
                "initial_equity": config["initial_equity"], **reference,
                "cash": cash["cash"], "frozen_cash": cash["frozen_cash"],
                "market_value": cash["market_value"], "long_gross": long_gross,
                "short_gross": short_gross, "margin": margin, "margin_dollars": margin_dollars,
                "buy_power_long": cash["buy_power_long"],
                "realized_profit": cash["realized_profit"],
                "unrealized_profit": cash["unrealized_profit"],
                "positions": positions, "zero_quantity_rows": zero_rows,
                "open_orders": orders,
                "account_binding": binding, "unverified_fields": [],
                "gross_source": ("sim_trade_cash_info long_mv/short_mv" if cash["long_gross"] is not None
                                 else "derived from the signed position market values"),
                "relay_assertion": RELAY_NOTE,
                "source": {"cash": provenance(args.cash), "positions": provenance(args.positions)}}
    # A field the broker did not give stays null and is named here; nothing unknown is reported as zero.
    snapshot["unverified_fields"] = sorted(key for key, value in snapshot.items()
                                           if value is None and key != "unverified_fields")
    if account_raw is not None:
        snapshot["account_type"] = "SIMULATE"
    for key, path in (("orders", args.orders), ("accounts", args.accounts)):
        if path:
            snapshot["source"][key] = provenance(path)
    snapshot["broker_fields"] = {"cash": sorted(data_of(cash_raw, "sim_trade_cash_info", args.cash)),
                                 "position_row_keys": row_keys}

    out = artifact_dir(now, account) / f"snapshot-{now.astimezone(timezone.utc):%H%M%S}.json"
    write_json(out, snapshot)
    journal_append(args.journal, "GOLIVE_SNAPSHOT", account, mode, now,
                   f"equity {cash['equity']:.2f}, {len(positions)} positions, margin "
                   f"{'unknown' if margin is None else format(margin, '.4%')}", "SNAPSHOT", config=config)
    print(f"SNAPSHOT {relative(out)}")
    print(f"  account {account} mode {mode} equity {cash['equity']:,.2f} cash {show(cash['cash'])} "
          f"positions {len(positions)} ({'flat' if not positions else 'held'})")
    print(f"  long gross {show(long_gross)} short gross {show(short_gross)} margin "
          f"{'unknown' if margin is None else f'{show(margin_dollars)} of equity, {margin:.4%}'}")
    print(f"  account binding: {binding['binding']} "
          f"({', '.join(binding['asserted_by_api']) or 'no raw response echoes an account id'})")
    if zero_rows:
        print(f"  zero-quantity broker rows kept out of the book: {[row['symbol'] for row in zero_rows]}")
    if account_type not in PAPER_ACCOUNT_TYPES:
        print("  R1 WILL REJECT: no --accounts evidence, so the account type and market are unasserted")
    if snapshot["unverified_fields"]:
        print(f"  unverified fields (null, never zero): {', '.join(snapshot['unverified_fields'])}")
    return 0


def artifact_dir(now: datetime, account: str) -> Path:
    return GO_DIR / now.astimezone(ET).date().isoformat() / account


# ------------------------------------------------------------------ plan
def command_plan(args) -> int:
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    if getattr(args, "final_exit", False) and args.smoke:
        raise GoliveError("--final-exit and --smoke are different batches: the flatten closes the real "
                          "book and the smoke test trades one share on the test account")
    config = load_config(args.config)
    if config.get("mode") != mode:
        raise GoliveError(f"the config mode is {config.get('mode')!r} but {account} is the {mode} account; "
                          f"R1 rejects every account except that mode's own")
    snapshot = read_json(args.snapshot)
    if not isinstance(snapshot, dict) or str(snapshot.get("account_id")) != account \
            or snapshot.get("mode") != mode:
        raise GoliveError(f"{args.snapshot}: the snapshot is not for {account} in mode {mode}")
    targets = load_targets(args.targets, config)
    quotes = parse_quotes(read_json(args.quotes))
    borrow = parse_borrow(args.borrow) if args.borrow else {}
    evidence = read_json(args.evidence) if args.evidence else {"assertions": {}, "approval": {}}
    evidence["_file"] = str(Path(args.evidence).resolve()) if args.evidence else None
    history = (parse_orders(data_of(read_json(args.orders), "sim_trade_history_order_list",
                                            args.orders), set(args.working or ()))
                 if args.orders else [])
    pending, blockers = pending_from_history(history, now, set(args.working or ()))
    executed, seen = executed_from_history(history, now)
    paths = {"snapshot": provenance(args.snapshot), "targets": provenance(args.targets),
             "quotes": provenance(args.quotes), "journal": str(args.journal),
             "borrow": provenance(args.borrow) if args.borrow else None,
             "orders": provenance(args.orders) if args.orders else None,
             "evidence": provenance(args.evidence) if args.evidence else None}

    # The shared switch is read before anything is planned, so a Slack PAUSE that landed while the snapshot
    # was being fetched stops this run instead of arriving after the tickets are already written. A scheduled
    # exit is a reduction and never waits for the flag, which is what makes the 11-27 flatten survive a pause.
    side = str(evidence.get("side") or "local")
    final_exit = bool(getattr(args, "final_exit", False))
    ops = load_switch_ops(config, targets, args.ops)
    if getattr(args, "risk_action", None) and (final_exit or args.smoke):
        raise GoliveError("--risk-action, --final-exit and --smoke are three different batches: the risk "
                          "batch trades what the risk report measured, the flatten closes the whole account "
                          "and the smoke test is one share on the test account")
    # The attribution decides which names the strategy may not touch, and a KILL needs it before the switch
    # is read, so the risk report is loaded first and the batch intent follows it.
    attribution = classify(snapshot, config, targets["strategy_id"],
                           read_journal(args.journal / "decisions.jsonl"))
    protected = set(attribution["discretionary"]) | set(attribution["unclassified"])
    risk_report = None
    if args.risk_action:
        risk_report = load_risk_report(args.risk_action, config, ops, account, mode, now, snapshot,
                                       protected)
    batch_intent = ("FINAL_EXIT" if final_exit else
                    "EMERGENCY_EXIT" if risk_report and risk_report["risk"]["halt"] else
                    "EXIT" if risk_report else "ENTRY")
    switch = switch_of(args, ops, config, now, batch_intent,
                       CLOUD_LEASE if side == "cloud" else LOCAL_LEASE)
    if not switch["decision"]["allow"]:
        switch_artifact(args, now, account, mode, config, switch, history)
        return 2
    if final_exit:
        result = final_exit_plan(config, snapshot, targets, quotes, evidence, args, now, account, mode,
                                 pending, executed, seen, blockers, paths, history)
    elif risk_report:
        paths["risk_report"] = risk_report["provenance"]
        result = risk_action_plan(config, snapshot, targets, quotes, evidence, args, now, account, mode,
                                  pending, executed, seen, blockers, paths, history, risk_report)
    elif args.smoke:
        result = smoke_plan(config, snapshot, targets, quotes, borrow, evidence, args, now, account, mode,
                            pending, executed, seen, blockers, paths)
    else:
        result = entry_plan(config, snapshot, targets, quotes, borrow, evidence, args, now, account, mode,
                            pending, executed, seen, blockers, paths)
    result["switch"] = {key: value for key, value in switch.items() if key != "read_command"}
    out = Path(args.out) if args.out else artifact_dir(now, account) / "plan.json"
    if out.exists():
        existing = read_json(out) or {}
        if any(ticket.get("order_id") for ticket in existing.get("tickets") or ()):
            raise GoliveError(f"{out} already records submitted tickets and a ticket with an order_id is "
                              f"never planned or sent again: write the next plan with --out")
    write_json(out, result)
    rejected = result["summary"]["rejected_tickets"]
    journal_append(args.journal, "GOLIVE_PLAN", account, mode, now,
                   f"{len(result['submit'])} of {len(result['tickets'])} tickets pass the rules; "
                   f"{len(rejected)} rejected; {result['summary']['note']}"
                   + (f" | {result['note']}" if result.get("note") else "")
                   + (f" | order budget {result['order_budget']['used']}+{result['order_budget']['exit_tickets']}"
                      f" of {result['order_budget']['cap']}" if result.get("order_budget") else ""),
                   ("PLAN_FINAL_EXIT" if final_exit else
                    "PLAN_RISK_ACTION" if risk_report else "PLAN") if result["submit"] else "PLAN_NO_SUBMIT",
                   target_checksum=targets["checksum"], config=config, run_id=result["run_id"])
    print_plan(out, result, rejected)
    return 0 if result["submit"] else 2


def entry_plan(config, snapshot, targets, quotes, borrow, evidence, args, now, account, mode, pending,
               executed, seen, blockers, paths) -> dict:
    book = sizing_quotes(quotes)
    # The planner recomputes the same skip list from the same inputs, so it is the authority here.
    want, _ = target_shares(targets["weights"], snapshot["equity"], book)
    records = eligibility_records(config, quotes, borrow, want,
                                  (evidence.get("borrow") or {}).get("easy_to_borrow_proxy"))
    eligibility = plan_eligibility(config, targets, records, book, now)
    journal_records = read_journal(args.journal / "decisions.jsonl")
    attribution = classify(snapshot, config, targets["strategy_id"], journal_records)
    protected = set(attribution["discretionary"]) | set(attribution["unclassified"])
    state = measure(config, snapshot["positions"], snapshot["equity"], snapshot["initial_equity"],
                    snapshot["peak_equity"])
    risk = actions(config, state, sorted(protected))
    plan = planner_plan(config, targets, snapshot, book, journal_records=journal_records,
                        eligibility=eligibility, risk=risk, executed_today=len(executed))
    orders, unpriced = [], []
    for order in plan["orders"]:
        row = quotes.get(order["symbol"]) or {}
        price, reason = marketable_limit(row, order["side"])
        if price is None:
            unpriced.append({"symbol": order["symbol"], "reason": f"not priced: {reason}",
                             "weight": order.get("weight")})
            continue
        ticket = dict(order)
        ticket["limit_price"] = price
        ticket["arrival_price"] = row.get("price")
        ticket["price_basis"] = (f"marketable limit off {row.get('bid')}/{row.get('ask')} bid/ask: "
                                 f"{order['side'].lower()} {-PRICE_OFFSET:+.2%}, cent through the touch")
        orders.append(ticket)
    plan["skipped"] = list(plan["skipped"]) + unpriced
    context = build_context(config, snapshot, targets, book, records, orders, pending, executed, seen,
                            evidence, now)
    tickets = gate_tickets(orders, context, config, evidence, quotes, args.journal)
    return assemble(config, snapshot, targets, quotes, evidence, plan, tickets, orders,
                    plan["skipped"], eligibility, risk, attribution, executed, pending,
                    blockers, now, account, mode, paths, smoke=False, records=records)


def smoke_plan(config, snapshot, targets, quotes, borrow, evidence, args, now, account, mode, pending,
               executed, seen, blockers, paths) -> dict:
    """Stage A: one ETF buy, one short sale and the two closes, one share each, on the test account only."""
    if mode != "test" or account != ACCOUNTS["test"]:
        raise GoliveError(f"--smoke is Stage A only: it must name the test account {ACCOUNTS['test']}, "
                          f"not {account} in mode {mode}")
    book = sizing_quotes(quotes)
    weights = targets["weights"]
    if weights.get(SMOKE_ETF, 0) <= 0:
        raise GoliveError(f"{SMOKE_ETF} is not a long in the target book, so the smoke test cannot use it")
    candidates = []
    for name, weight in weights.items():
        row = quotes.get(name) or {}
        if weight >= 0 or not row.get("bid") or row["bid"] >= SMOKE_SHORT_MAX_PRICE:
            continue
        if row.get("security_type") != "COMMON_STOCK" or row.get("halted") is not False:
            continue
        capacity = (borrow.get(name) or {}).get("max_sell_short_qty")
        if borrow and (capacity is None or capacity < SMOKE_SIZE):
            continue
        candidates.append((round(row["bid"], 2), name))
    if not candidates:
        raise GoliveError("no executable short in the target book is priced below "
                          f"${SMOKE_SHORT_MAX_PRICE:g} in the quotes and borrow evidence given")
    _, short = sorted(candidates)[0]
    want = {SMOKE_ETF: SMOKE_SIZE, short: -SMOKE_SIZE}
    records = eligibility_records(config, quotes, borrow, want,
                                  (evidence.get("borrow") or {}).get("easy_to_borrow_proxy"))
    orders = []
    for symbol, side, intent in ((SMOKE_ETF, "BUY", "ENTRY"), (short, "SELL", "ENTRY"),
                                 (SMOKE_ETF, "SELL", "EXIT"), (short, "BUY", "EXIT")):
        row = quotes.get(symbol) or {}
        price, reason = marketable_limit(row, side)
        if price is None:
            raise GoliveError(f"the smoke ticket for {symbol} {side} cannot be priced: {reason}")
        orders.append({"account_id": account, "symbol": symbol, "side": side, "quantity": SMOKE_SIZE,
                       "limit_price": price, "order_type": "LIMIT", "time_in_force": "DAY",
                       "session": "RTH", "intent": intent, "sleeve": targets["sleeves"].get(symbol),
                       "weight": weights.get(symbol, 0.0), "reason": "Stage A smoke test",
                       "workflow": "paper-trading-gate", "target_checksum": targets["checksum"],
                       "arrival_price": row.get("price"),
                       "price_basis": (f"marketable limit off {row.get('bid')}/{row.get('ask')} bid/ask: "
                                       f"{side.lower()} {-PRICE_OFFSET:+.2%}, cent through the touch"),
                       "client_order_id": client_order_id(targets, symbol, side, SMOKE_SIZE)})
    opens = [order for order in orders if order["intent"] == "ENTRY"]
    closes = [order for order in orders if order["intent"] == "EXIT"]
    context = build_context(config, snapshot, targets, book, records, opens, pending, executed, seen,
                            evidence, now)
    tickets = gate_tickets(opens, context, config, evidence, quotes, args.journal)
    projected = project_snapshot(snapshot, opens)
    close_context = build_context(config, projected, targets, book, records, closes, pending, executed,
                                  seen, evidence, now)
    tickets = tickets + gate_tickets(closes, close_context, config, evidence, quotes, args.journal)
    for position, ticket in enumerate(tickets, start=1):
        ticket["n"] = position  # both gate groups share one numbering: `record --ticket n` must be unique
    plan = {"status": "PLAN", "note": None, "orders": orders, "skipped": [],
            "counts": {"orders": len(orders), "order_budget": (config["competition_order_cap"]
                                                               - config["emergency_flatten_reserve"]),
                       "budget_left_today": (config["competition_order_cap"]
                                             - config["emergency_flatten_reserve"] - len(executed))},
            "target_shares": want, "final_shares": {}, "entry_batch": False,
            "plan_checksum": digest(orders)}
    attribution = classify(snapshot, config, targets["strategy_id"], [])
    return assemble(config, snapshot, targets, quotes, evidence, plan, tickets, orders, [],
                    {"blocked": [], "skipped": [], "slack_report": []}, {"slack_report": []}, attribution,
                    executed, pending, blockers, now, account, mode, paths, smoke=True, records=records)


def competition_order_use(journal_dir: Path, history: list, orders: list, config: dict, account: str,
                          now: datetime, intent: str = "ENTRY") -> dict:
    """The 200-order competition budget as a flatten sees it: spent, needed, left, rounds affordable.

    Two counts, and the larger one is used. `sim_trade_history_order_list` only returns the window the
    operator asked for, so a single-day read is not evidence that fewer orders were ever sent; the journal
    is the running total of what this tool recorded submitting. An order placed without `record` is invisible
    to both, which is stated rather than assumed away.
    """
    cap, reserve = int(config["competition_order_cap"]), int(config["emergency_flatten_reserve"])
    start = timestamp(config["competition_start"])
    path = Path(journal_dir) / "decisions.jsonl"
    journal_used, note = 0, None
    try:
        rows = read_journal(path)
    except (ValueError, OSError) as exc:
        note = (f"{relative(path)} is unreadable ({type(exc).__name__}), so the journaled count is unknown "
                f"and only the order-history window counts")
        rows = []
    for row in rows:
        if row.get("kind") != "GOLIVE_RECORD" or row.get("action") != "SUBMITTED":
            continue
        if str(row.get("account_id")) != str(account):
            continue
        try:
            when = timestamp(row.get("timestamp") or "")
        except (TypeError, ValueError):
            note = f"a journaled submission has no readable timestamp: {row.get('event_id')}"
            continue
        if when >= start:
            journal_used += 1
    broker_used = sum(1 for row in history
                      if str(row.get("client_order_id") or "").startswith(STRATEGY_ORDER_PREFIX)
                      and (row.get("submitted_at") or "9999") >= iso(start))
    used = max(journal_used, broker_used)
    tickets = len(orders)
    left = cap - used - tickets
    return {"cap": cap, "emergency_reserve": reserve, "strategy_budget": cap - reserve,
            "since": iso(start), "journal_used": journal_used, "history_window_used": broker_used,
            "used": used, "exit_tickets": tickets, "left_after_exit": left,
            "rounds_affordable": 0 if tickets <= 0 else max(0, left // tickets),
            "rounds_needed": reprice_round_cap(config, intent), "over_cap": left < 0, "note": note,
            "basis": ("journal GOLIVE_RECORD/SUBMITTED rows for this account since competition_start, "
                      "compared with pstar- rows in the order-history file given; the larger count is used, "
                      "and an order this tool never recorded is in neither")}


def reprice_round_cap(config: dict, intent: str = "ENTRY") -> int:
    """How many 5-minute reprices a batch may spend: three for a rebalance, the whole exit window for a flatten."""
    if intent != "FINAL_EXIT":
        return MAX_REPRICE_ROUNDS
    start, flatten = timestamp(config["final_exit_start"]), timestamp(config["final_flatten_at"])
    return max(1, math.ceil((flatten - start).total_seconds() / REPRICE_AFTER_SECONDS))


def final_exit_orders(config, snapshot, targets, quotes, account, now) -> tuple[list, list, dict]:
    """Every position on the book closed in one batch, from the broker's own share counts.

    The quantity is what the account actually holds, not what the target book says, and it includes names
    the entry batch would never trade (discretionary or unclassified): the scheduled exit is the one moment
    the whole account is supposed to go flat, and leaving an unattributed position open past 2026-11-27
    would keep it on the books after the competition ends. Those names are listed separately so the plan
    and the Slack post say what is being closed for a reason other than the strategy.
    """
    flatten = market_flatten_window(config, now)
    held, orders, unpriced, outside = holdings(snapshot), [], [], []
    for name in sorted(held, key=lambda symbol: (0 if held[symbol] > 0 else 1, symbol)):
        quantity = held[name]
        if not quantity:
            continue
        side, size = ("SELL", quantity) if quantity > 0 else ("BUY", -quantity)
        row = quotes.get(name) or {}
        if flatten:
            price = None
            basis = (f"MARKET: {config['final_flatten_at']} passed, so this is one of the tickets still open "
                     f"at the flatten deadline. R7 allows MARKET for nothing else, and the gate still needs "
                     f"a relay-verified quote inside {config['quote_max_age_seconds']}s to send it")
        else:
            price, reason = marketable_limit(row, side)
            if price is None:
                unpriced.append({"symbol": name, "side": side, "quantity": size,
                                 "reason": f"not priced: {reason}",
                                 "weight": targets["weights"].get(name, 0.0),
                                 "note": "no marketable limit; the ticket waits for a quote or goes out as "
                                         "MARKET after final_flatten_at"})
                continue
            basis = (f"marketable limit off {row.get('bid')}/{row.get('ask')} bid/ask: {side.lower()} "
                     f"{-PRICE_OFFSET:+.2%}, cent through the touch")
        if name not in targets["weights"]:
            outside.append(f"{name} {'long' if quantity > 0 else 'short'} {size} share(s)")
        orders.append({"account_id": account, "symbol": name, "side": side, "quantity": size,
                       "limit_price": price, "order_type": "MARKET" if flatten else "LIMIT",
                       "time_in_force": "DAY", "session": "RTH", "intent": "FINAL_EXIT",
                       "sleeve": targets["sleeves"].get(name),
                       "weight": targets["weights"].get(name, 0.0),
                       "reason": ("close the whole book: scheduled final exit of the competition"
                                  + ("" if name in targets["weights"] else "; position outside the target book")),
                       "workflow": "paper-trading-gate", "target_checksum": targets["checksum"],
                       "arrival_price": row.get("price"), "price_basis": basis,
                       "client_order_id": client_order_id(targets, name, side, size)})
    return orders, unpriced, {"flatten_deadline_passed": flatten, "outside_target_book": outside}


def final_exit_plan(config, snapshot, targets, quotes, evidence, args, now, account, mode, pending,
                    executed, seen, blockers, paths, history) -> dict:
    orders, unpriced, detail = final_exit_orders(config, snapshot, targets, quotes, account, now)
    if not orders:
        raise GoliveError("the fresh snapshot holds no position, so there is nothing to flatten: check the "
                          "account id and re-read the positions before sending anything")
    held = holdings(snapshot)
    # No borrow evidence: covering a short reduces it, so R5's capacity test does not apply and an entry
    # check must not be the reason a scheduled exit fails.
    records = eligibility_records(config, quotes, {}, {},
                                  (evidence.get("borrow") or {}).get("easy_to_borrow_proxy"))
    context = build_context(config, snapshot, targets, sizing_quotes(quotes), records, orders, pending,
                            executed, seen, evidence, now)
    tickets = gate_tickets(orders, context, config, evidence, quotes, args.journal)
    budget = competition_order_use(args.journal, history, orders, config, account, now, "FINAL_EXIT")
    note = (f"{len(orders)} ticket(s) close every position on the account"
            + ("; the flatten deadline has passed, so these are MARKET" if detail["flatten_deadline_passed"]
               else f"; MARKET is only allowed after {config['final_flatten_at']}"))
    plan = {"status": "FINAL_EXIT", "note": note, "orders": orders, "skipped": unpriced,
            "counts": {"orders": len(orders), "order_budget": budget["strategy_budget"],
                       "budget_left_today": budget["left_after_exit"], "competition_budget": budget},
            "target_shares": {name: 0 for name in held}, "final_shares": {}, "entry_batch": False,
            "reduce_only": True,
            "outside_target_book": detail["outside_target_book"],
            "flatten_deadline_passed": detail["flatten_deadline_passed"],
            "plan_checksum": digest(orders)}
    attribution = classify(snapshot, config, targets["strategy_id"],
                           read_journal(args.journal / "decisions.jsonl"))
    state = measure(config, snapshot["positions"], snapshot["equity"], snapshot["initial_equity"],
                    snapshot["peak_equity"])
    risk = {"halt": False, "flatten": True, "delever_scale": 1.0, "target_shares": plan["target_shares"],
            "share_changes": {name: -shares for name, shares in held.items() if shares},
            "orders_required": len(orders), "margin_after": 0.0, "protected_skipped": [],
            "slack_report": [f"scheduled final exit at {config['final_exit_start']} ET: close all "
                             f"{len(orders)} position(s), margin {state['margin']:.1%} before the batch"]}
    result = assemble(config, snapshot, targets, quotes, evidence, plan, tickets, orders, unpriced,
                      {"blocked": [], "skipped": [], "slack_report": []}, risk,
                      attribution, executed, pending, blockers, now, account, mode, paths, smoke=False,
                      records=records)
    result["intent"] = "FINAL_EXIT"
    result["note"] = note
    result["final_exit"] = {"note": note, "outside_target_book": detail["outside_target_book"],
                            "flatten_deadline_passed": detail["flatten_deadline_passed"],
                            "final_exit_start": config["final_exit_start"],
                            "final_flatten_at": config["final_flatten_at"],
                            "reprice_rounds": reprice_round_cap(config, "FINAL_EXIT")}
    result["order_budget"] = budget
    return result


def load_risk_report(path, config, ops, account, mode, now, snapshot, protected) -> dict:
    """Read the Risk Auditor's `risk_report.json` and re-measure it against the fresh snapshot.

    The report is the trigger, never the arithmetic: every share count, threshold and ticket below comes from
    `live/risk_monitor.py` on the snapshot this plan is built from, so a report taken off a different book or
    an older price run cannot size an order. Disagreement is refused rather than averaged:

    * the fresh book is worse than the report (an ALERT that has since become a drawdown kill) - the plan
      takes the harder action and says so, because a reduction the current book genuinely needs is never
      refused on the grounds that the paper trail lagged behind it;
    * the fresh book is better than the report - refused, because the breach the auditor saw is no longer
      there and selling into a book that does not need it would be trading on stale evidence.
    """
    if not path:
        raise GoliveError("--risk-action needs the risk report file the pstar-risk-check skill wrote")
    report = read_json(path)
    if not isinstance(report, dict):
        raise GoliveError(f"{path}: the risk report is not a JSON object")
    if report.get("schema_version") != RISK_SCHEMA:
        raise GoliveError(f"{path}: unsupported risk schema {report.get('schema_version')!r}, this build "
                          f"reads {RISK_SCHEMA!r}")
    if str(report.get("account_id")) != str(account) or report.get("mode") != mode:
        raise GoliveError(f"{path}: the report is for account {report.get('account_id')} in mode "
                          f"{report.get('mode')!r}, not {account} in {mode}: a risk action on the wrong "
                          f"book is the one mistake a risk check must not make")
    level = str(report.get("level") or "").upper()
    if level not in RISK_LEVELS:
        raise GoliveError(f"{path}: level {report.get('level')!r} is not one of {', '.join(RISK_LEVELS)}")
    when = timestamp(report.get("generated_at") or "")
    age = (now - when).total_seconds()
    if age > config["account_max_age_seconds"]:
        raise GoliveError(f"{path}: the report was measured {age:.0f}s ago, over the "
                          f"{config['account_max_age_seconds']:.0f}s freshness window: re-run the check "
                          f"before acting on it")
    peak, peak_source = choose_peak(config, snapshot, report, now)
    state = measure(config, snapshot["positions"], snapshot["equity"], snapshot["initial_equity"], peak)
    risk = actions(config, state, sorted(protected))
    warnings = warn_lines(config, ops, state)
    fresh = ("KILL" if risk["halt"] else "ALERT" if risk["share_changes"]
             else "WARN" if warnings else "OK")
    if RISK_LEVELS.index(level) > RISK_LEVELS.index(fresh):
        raise GoliveError(f"{path}: the report says {level} but the fresh snapshot says {fresh} "
                          f"(margin {state['margin']:.2%}, drawdown {state['drawdown']:.2%}, largest name "
                          f"{state['max_ticker_vs_initial']:.2%} of initial assets): no ticket is built from "
                          f"a breach the current book does not show")
    held = holdings(snapshot)
    reported = {str(row.get("symbol")).removeprefix("US."): row for row in report.get("positions") or ()}
    drift = sorted(name for name in set(held) | set(reported)
                   if held.get(name, 0) != (reported.get(name) or {}).get("quantity"))
    return {"file": str(path), "provenance": provenance(path), "level": level, "fresh_level": fresh,
            "generated_at": report.get("generated_at"), "age_seconds": round(age),
            "peak_used": peak, "peak_source": peak_source, "snapshot_peak": snapshot.get("peak_equity"),
            "findings": list(report.get("findings") or ()), "anomalies": list(report.get("anomalies") or ()),
            "peak_basis": report.get("peak_basis"), "report_measured": report.get("measured") or {},
            "book_drift": drift, "state": state, "risk": risk, "source": report.get("source"),
            "warnings": warnings}


def choose_peak(config: dict, snapshot: dict, report: dict, now: datetime) -> tuple[float, str]:
    """The high-water mark the drawdown rule is measured against, and where it came from.

    The Risk Auditor and the Executor do not share a filesystem, so the peak that survives between cloud
    sessions is the one the auditor keeps in its own state file and carries in the report. A snapshot that
    has never seen an earlier day would then read a 0% drawdown on a book that is 9% off its peak, so the
    report's peak is adopted when it is higher - but only when the report shows the day-by-day history that
    produced it, because a number with no history behind it is not evidence of anything.
    """
    peak, source = float(snapshot["peak_equity"]), "this snapshot's own equity history"
    claimed = report.get("peak_equity")
    if not finite(claimed) or claimed <= peak + 1e-9:
        return peak, source
    initial = float(config["initial_equity"])
    if claimed > PEAK_SANITY_MULTIPLE * initial:
        raise GoliveError(f"the report claims a peak of {claimed:,.2f}, more than {PEAK_SANITY_MULTIPLE:.0f}x "
                          f"the {initial:,.0f} starting assets: no drawdown action is taken on that number")
    rows = [row for row in report.get("peak_history") or () if isinstance(row, dict)
            and finite(row.get("equity")) and row.get("day")]
    days = sorted({str(row["day"]) for row in rows})
    if not rows:
        raise GoliveError(f"the report claims a peak of {claimed:,.2f} above this snapshot's "
                          f"{peak:,.2f} and carries no day-by-day peak history to show for it")
    if len(days) != len(rows):
        raise GoliveError("the report's peak history repeats a day, so its high-water mark cannot be traced")
    if len(days) > MAX_PEAK_HISTORY:
        raise GoliveError(f"the report's peak history runs to {len(rows)} days, over the "
                          f"{MAX_PEAK_HISTORY}-day window this tool keeps")
    if days[0] < timestamp(config["competition_start"]).astimezone(ET).date().isoformat():
        raise GoliveError(f"the report's peak history starts {days[0]}, before the competition opened on "
                          f"{config['competition_start']}: a peak from outside the competition is not this "
                          f"book's peak")
    if days[-1] > now.astimezone(ET).date().isoformat():
        raise GoliveError(f"the report's peak history has a row dated {days[-1]}, in the future")
    if max(float(row["equity"]) for row in rows) + 1e-6 < claimed:
        raise GoliveError(f"the report claims a peak of {claimed:,.2f} but its own history tops out at "
                          f"{max(float(row['equity']) for row in rows):,.2f}")
    return float(claimed), (f"the report's day-keyed peak history ({days[0]} to {days[-1]}), which this "
                            f"snapshot has never seen")


def risk_action_plan(config, snapshot, targets, quotes, evidence, args, now, account, mode, pending,
                     executed, seen, blockers, paths, history, report) -> dict:
    """The between-rebalance risk batch: reduce-only tickets, and on a KILL the halt latches with them.

    `live/risk_monitor.actions` is the single source of the arithmetic, so the trim to 38% of initial assets,
    the scale to 90% margin and the drawdown flatten are the same numbers the planner and the backtest use.
    A KILL latches the durable kill switch (R9/R16) and mirrors `paused` to the shared flag before any ticket
    is printed: the account stops first and the flatten follows, which is the order that holds if the run
    dies halfway through.
    """
    risk, state = report["risk"], report["state"]
    if not risk["share_changes"]:
        raise GoliveError(f"{report['file']} says {report['level']} but the fresh book needs no reduction: "
                          f"margin {state['margin']:.2%}, largest name "
                          f"{state['max_ticker_vs_initial']:.2%} of initial assets")
    intent = "EMERGENCY_EXIT" if risk["halt"] else "EXIT"
    latched = None
    if risk["halt"]:
        # Latch before the tickets are judged, so what the journal records is the gate's answer on a halted
        # book: an EMERGENCY_EXIT reduction is exactly what R9 and R16 have to let through.
        reason = (f"paused at {iso(now)}: risk KILL from {report['file']} measured at "
                  f"{report['generated_at']}: drawdown {state['drawdown']:.2%} of the peak")
        journal = Journal(Path(args.journal))
        try:
            journal.set_halt(account, reason)
        finally:
            journal.close()
        journal_append(args.journal, "GOLIVE_PAUSE", account, mode, now, reason, "RISK_KILL", config=config)
        latched = mirror_switch(args, now, account, config, {"state": pause_switch.PAUSED, "lease": None},
                                f"risk KILL from {report['file']}")
    held, orders, unpriced = holdings(snapshot), [], []
    for name in sorted(risk["share_changes"], key=lambda symbol: (0 if held.get(symbol, 0) > 0 else 1,
                                                                  symbol)):
        delta = risk["share_changes"][name]
        side, size = ("SELL", -delta) if delta < 0 else ("BUY", delta)
        row = quotes.get(name) or {}
        price, reason = marketable_limit(row, side)
        if price is None:
            unpriced.append({"symbol": name, "side": side, "quantity": size,
                             "reason": f"not priced: {reason}",
                             "weight": targets["weights"].get(name, 0.0),
                             "note": "R7 allows MARKET for a scheduled FINAL_EXIT only, so this reduction "
                                     "waits for a quote rather than going out at the market"})
            continue
        orders.append({"account_id": account, "symbol": name, "side": side, "quantity": size,
                       "limit_price": price, "order_type": "LIMIT", "time_in_force": "DAY", "session": "RTH",
                       "intent": intent, "sleeve": targets["sleeves"].get(name),
                       "weight": targets["weights"].get(name, 0.0),
                       "reason": (f"risk {report['fresh_level']}: "
                                  + ("drawdown flatten of every strategy position" if risk["halt"] else
                                     "reduce-only risk action from " + report["file"])),
                       "workflow": "paper-trading-gate", "target_checksum": targets["checksum"],
                       "arrival_price": row.get("price"),
                       "price_basis": f"marketable limit off {row.get('bid')}/{row.get('ask')} bid/ask: "
                                      f"{side.lower()} {-PRICE_OFFSET:+.2%}, cent through the touch",
                       "client_order_id": client_order_id(targets, name, side, size)})
    records = eligibility_records(config, quotes, {}, {},
                                  (evidence.get("borrow") or {}).get("easy_to_borrow_proxy"))
    context = build_context(config, snapshot, targets, sizing_quotes(quotes), records, orders, pending,
                            executed, seen, evidence, now)
    tickets = gate_tickets(orders, context, config, evidence, quotes, args.journal)
    budget = competition_order_use(args.journal, history, orders, config, account, now, intent)
    note = (f"{len(orders)} reduce-only ticket(s) for risk {report['fresh_level']}"
            + (" - the drawdown limit is reached, so every strategy position closes and the halt latches"
               if risk["halt"] else
               f" - trim to {config['ticker_cap_of_initial']:.0%} of initial assets and margin to "
               f"{config['delever_to']:.0%}" if risk["delever_scale"] < 1.0 else
               f" - trim the names above {config['trim_ticker_above_of_initial']:.0%} of initial assets"))
    plan = {"status": "RISK_ACTION", "note": note, "orders": orders, "skipped": unpriced,
            "counts": {"orders": len(orders), "order_budget": budget["strategy_budget"],
                       "budget_left_today": budget["left_after_exit"], "competition_budget": budget},
            "target_shares": dict(risk["target_shares"]), "final_shares": dict(risk["target_shares"]),
            "entry_batch": False, "reduce_only": True, "plan_checksum": digest(orders)}
    attribution = classify(snapshot, config, targets["strategy_id"],
                           read_journal(args.journal / "decisions.jsonl"))
    result = assemble(config, snapshot, targets, quotes, evidence, plan, tickets, orders, unpriced,
                      {"blocked": [], "skipped": [], "slack_report": []}, risk, attribution, executed,
                      pending, blockers, now, account, mode, paths, smoke=False, records=records)
    result["intent"] = intent
    result["note"] = note
    result["risk_action"] = {
        "level": report["fresh_level"], "reported_level": report["level"], "source": report["file"],
        "measured_at": report["generated_at"], "age_seconds": report["age_seconds"],
        "findings": report["findings"], "anomalies": report["anomalies"],
        "warnings": report["warnings"],
        "peak_basis": report["peak_basis"], "book_drift": report["book_drift"],
        "peak_used": report["peak_used"], "peak_source": report["peak_source"],
        "snapshot_peak": report["snapshot_peak"],
        "margin": state["margin"], "margin_after": risk["margin_after"], "drawdown": state["drawdown"],
        "delever_scale": risk["delever_scale"], "protected_skipped": risk["protected_skipped"],
        "halt": risk["halt"], "slack_report": risk["slack_report"],
        "note": (f"the fresh snapshot is {report['fresh_level']}, the report said {report['level']}"
                 if report["fresh_level"] != report["level"] else
                 "the fresh snapshot and the report agree on the level")}
    result["order_budget"] = budget
    if latched is not None:
        result["risk_action"]["kill_switch"] = kill_switch_state(args.journal, account)
        result["risk_action"]["switch_mirror"] = latched
    return result


def project_snapshot(snapshot: dict, orders: list) -> dict:
    """The account the closing tickets go into: this snapshot with both opens filled."""
    held = {str(row["symbol"]): dict(row) for row in snapshot.get("positions") or ()}
    for order in orders:
        name = order["symbol"]
        delta = order["quantity"] * (1 if order["side"] == "BUY" else -1)
        row = dict(held.get(name) or {"symbol": name, "quantity": 0, "avg_cost": None,
                                      "market_value": None, "price": order["limit_price"],
                                      "broker_symbol": "US." + name, "price_basis": "projected at the limit"})
        row["quantity"] = int(row.get("quantity") or 0) + delta
        row["price"] = row.get("price") or order["limit_price"]
        row["market_value"] = row["quantity"] * row["price"]
        if row["quantity"]:
            held[name] = row
        else:
            held.pop(name, None)
    positions = [row for _, row in sorted(held.items())]
    long_gross = sum(row["market_value"] for row in positions if row["quantity"] > 0)
    short_gross = -sum(row["market_value"] for row in positions if row["quantity"] < 0)
    return {**snapshot, "positions": positions, "long_gross": long_gross, "short_gross": short_gross,
            "projection": "this snapshot with every opening smoke ticket filled"}


def gate_tickets(orders: list, context: dict, config: dict, evidence: dict, quotes: dict,
                 journal_dir: Path) -> list[dict]:
    """Gate every order of this group against the group's own plan checksum and journal each decision.

    `Journal.evaluate_and_record` is the production path: it injects the durable kill switch and the daily
    entry halt from the local control store, evaluates R0-R21 and appends the ORDER_CHECK decision, so what
    an auditor reads in `journal/decisions.jsonl` is exactly what this plan was judged on.
    """
    journal = Journal(Path(journal_dir))
    tickets = []
    try:
        for index, order in enumerate(orders):
            single = {**context, "quotes": {**context["quotes"],
                                            order["symbol"]: gate_quote(
                                                quotes.get(order["symbol"]) or {}, order["side"])}}
            bound = bind_approvals(single, order, config, evidence.get("approval") or {})
            result = journal.evaluate_and_record(dict(order), bound, config)
            tickets.append({"n": index + 1, "order": order, "symbol": order["symbol"],
                            "side": order["side"], "quantity": order["quantity"],
                            "limit_price": order.get("limit_price"), "intent": order["intent"],
                            "sleeve": order.get("sleeve"), "weight": order.get("weight"),
                            "arrival_price": order.get("arrival_price"),
                            "price_basis": order["price_basis"],
                            "client_order_id": order["client_order_id"], "tool": "sim_trade_input_order",
                            "payload": paper_payload(order, MARKET_ID), "gate": result,
                            "submit": result["eligible"], "status": "PLANNED", "attempts": 0,
                            "reprice_round": 0, "submissions": []})
    finally:
        journal.close()
    return tickets



def assemble(config, snapshot, targets, quotes, evidence, plan, tickets, orders, skipped, eligibility,
             risk, attribution, executed, pending, blockers, now, account, mode, paths, smoke,
             records=None) -> dict:
    equity = snapshot["equity"]
    cap = config["ticker_cap_of_initial"] * config["initial_equity"]
    held_now = holdings(snapshot)
    moved = {order["symbol"]: order["quantity"] * (1 if order["side"] == "BUY" else -1) for order in orders}
    after = dict(held_now)
    for name, delta in moved.items():
        after[name] = after.get(name, 0) + delta
    price = {name: (quotes.get(name) or {}).get("price") for name in after}
    long_after = sum(shares * (price[name] or 0) for name, shares in after.items() if shares > 0)
    short_after = -sum(shares * (price[name] or 0) for name, shares in after.items() if shares < 0)
    margin_after = (config["margin_long_factor"] * long_after
                    + config["margin_short_factor"] * short_after) / equity
    largest = max(after, key=lambda name: abs(after[name] * (price[name] or 0)), default=None)
    largest_dollars = None if largest is None else abs(after[largest] * (price[largest] or 0))
    passing = [ticket for ticket in tickets if ticket["submit"]]
    if plan.get("reduce_only"):
        # A batch that only closes raises cash before it spends any: for the flatten and the risk kill the
        # target weight is not the position, so the entry ordering would interleave the covers and the sales.
        passing = sorted(passing, key=lambda ticket: (0 if ticket["side"] == "SELL" else 1, ticket["symbol"]))
    elif not smoke:                     # ETF tickets first, then the shorts, as the runbook submits them
        passing = sorted(passing, key=lambda ticket: (0 if ticket["weight"] > 0 else 1, ticket["symbol"]))
    submit = [ticket["n"] for ticket in passing]
    rejected_rows = [{"n": ticket["n"], "symbol": ticket["symbol"], "side": ticket["side"],
                      "quantity": ticket["quantity"], "action": ticket["gate"]["action"],
                      "failed_rules": ticket["gate"]["failed_rules"],
                      "reasons": [check["threshold"] for check in ticket["gate"]["checks"]
                                  if not check["passed"]]}
                     for ticket in tickets if not ticket["submit"]]
    rejected_rules = sorted({rule for row in rejected_rows for rule in row["failed_rules"]})
    summary = {"tickets": len(tickets), "buy": sum(1 for t in tickets if t["side"] == "BUY"),
               "sell": sum(1 for t in tickets if t["side"] == "SELL"), "submit": len(submit),
               "rejected": len(rejected_rows), "rejected_tickets": rejected_rows,
               "rules_failed": sorted({rule for ticket in tickets for rule in ticket["gate"]["failed_rules"]}),
               "reject_tally": {rule: sum(1 for row in rejected_rows if rule in row["failed_rules"])
                                for rule in rejected_rules},
               "price_band_rejections": sum(1 for row in rejected_rows if "R7" in row["failed_rules"]),
               "long_gross_after": long_after, "short_gross_after": short_after,
               "margin_after": margin_after,
               "margin_before": snapshot.get("margin"), "margin_cap": config["margin_cap"],
               "positions_after": len([shares for shares in after.values() if shares]),
               "max_positions": config["max_positions"],
               "shorts_after": len([shares for shares in after.values() if shares < 0]),
               "largest_ticker": largest, "largest_dollars": None if largest_dollars is None else
               round(largest_dollars, 2),
               "largest_vs_cap": None if largest_dollars is None else round(largest_dollars / cap, 4),
               "ticker_cap_dollars": cap, "estimated_fees_usd": round(len(submit)
                                                                      * config["order_platform_fee"], 2),
               "order_platform_fee": config["order_platform_fee"],
               "order_budget": (plan.get("counts") or {}).get("order_budget"),
               "budget_left_today": (plan.get("counts") or {}).get("budget_left_today"),
               "executed_today": len(executed),
               "skipped": [{"symbol": row.get("symbol"), "reason": row.get("reason"),
                            "weight": row.get("weight")} for row in skipped or ()],
               "blocked": list((eligibility or {}).get("blocked") or ()),
               "blocked_detail": (eligibility or {}).get("blocked_detail") or {},
               "slack_report": [*((eligibility or {}).get("slack_report") or ()),
                                *((risk or {}).get("slack_report") or ())],
               "protected": {"discretionary": list(attribution.get("discretionary") or ()),
                             "unclassified": list(attribution.get("unclassified") or ())},
               "planner_status": plan.get("status"), "planner_note": plan.get("note"),
               "entry_batch": bool(plan.get("entry_batch")), "open_orders_seen": len(pending),
               "note": "smoke plan: 2 opens and 2 closes, one share each" if smoke else
               f"{len(submit)} tickets pass the rules, {len(rejected_rows)} do not"}
    return {"run_id": f"golive-{now.astimezone(ET).date().isoformat()}-{mode}",
            "generated_at": iso(now), "mode": mode, "account_id": account, "market_id": MARKET_ID,
            "strategy": config["strategy"], "variant": config["variant"],
            "config_checksum": digest(config), "execution_enabled_in_config": config["execution_enabled"],
            "smoke": smoke,
            "targets": {"signal_date": targets["signal_date"], "rebalance_due": targets["rebalance_due"],
                        "status": targets["status"], "checksum": targets["checksum"],
                        "strategy_id": targets["strategy_id"], **paths["targets"]},
            "snapshot": {"as_of": snapshot.get("as_of"), "equity": equity,
                         "checksum": digest(snapshot), **paths["snapshot"]},
            # The rows R2/R5/R11 read at plan time, kept so a reprice re-gates on the same evidence.
            "eligibility_records": records,
            "inputs": {"quotes": paths["quotes"], "borrow": paths["borrow"], "orders": paths["orders"],
                       "evidence": paths["evidence"]},
            "quotes": {name: {"bid": row.get("bid"), "ask": row.get("ask"), "last": row.get("last"),
                              "price": row.get("price"), "price_basis": row.get("price_basis"),
                              "as_of": row.get("as_of"), "security_type": row.get("security_type")}
                       for name, row in sorted(quotes.items())},
            "weights": targets["weights"], "target_shares": plan.get("target_shares") or {},
            "shares_after": after,
            "price_policy": {"used": "golive marketable limit (prompt 07)",
                             "planner_policy": "live/order_planner.py sets the limit exactly at the "
                                               "relay-verified quote price (the mid here), which is not "
                                               "marketable, so its limit price is replaced ticket by ticket",
                             "rule": "BUY ask + 0.05%, SELL bid - 0.05%, rounded to the cent away from the "
                                     "mid and at least one cent through the touch; LIMIT, session RTH, "
                                     "order_trade_time_type 1 (regular hours only); no other order types",
                             "band_check": "R7 measures the limit against the same-side touch, so the 5 bps "
                                           "offset stays inside the frozen 25 bps band",
                             "known_conflict": "the one-cent minimum through the touch exceeds 25 bps on a "
                                               "touch below about $4.00, so those tickets are rejected by R7 "
                                               "and the frozen band is never relaxed here"},
            "relay_assertion": RELAY_NOTE,
            "kill_switch": kill_switch_state(paths.get("journal"), account),
            "evidence": {"file": paths["evidence"] and paths["evidence"]["file"],
                         "assertions": {key: bool((evidence.get("assertions") or {}).get(key))
                                        for key in DEFAULT_ASSERTIONS},
                         "approval_present": bool((evidence.get("approval") or {}).get("issued_at")),
                         "approval": {key: value for key, value in (evidence.get("approval") or {}).items()
                                      if key in ("approval_id", "auditor_session_id", "guard_session_id",
                                                 "reply", "confirmed_by", "issued_at", "expires_at",
                                                 "verified_by_relay", "note")},
                         "borrow": evidence.get("borrow") or {}},
            "summary": summary, "tickets": tickets, "submit": submit,
            "blockers": list(blockers)}


def print_plan(out: Path, result: dict, rejected: list) -> None:
    summary = result["summary"]
    print(f"PLAN {relative(out)}")
    budget = result.get("order_budget")
    risk = result.get("risk_action")
    if risk:
        print(f"  RISK {risk['level']} from {risk['source']} measured {risk['measured_at']} "
              f"({risk['age_seconds']}s ago): {risk['note']}")
        print(f"    margin {format(risk['margin'], '.2%')} before, "
              f"{'unknown' if risk['margin_after'] is None else format(risk['margin_after'], '.2%')} after; "
              f"drawdown {format(risk['drawdown'], '.2%')} of the peak; peak basis: {risk['peak_basis']}")
        if risk["delever_scale"] < 1.0:
            print(f"    book scaled by {risk['delever_scale']:.4f} to reach the de-lever target")
        for line in risk["findings"]:
            print(f"    finding: {line.get('rule')} {line.get('value')} against "
                  f"{line.get('threshold')}: {line.get('detail') or ''}".rstrip())
        for line in risk["warnings"]:
            print(f"    warning: {line}")
        if risk["protected_skipped"]:
            print(f"    {' '.join(risk['protected_skipped'])} above the trim line but not a strategy "
                  f"position: alert only, never sold here")
        if risk["book_drift"]:
            print(f"    the report and the fresh snapshot disagree on share counts for: "
                  f"{', '.join(risk['book_drift'])} - the fresh snapshot sizes the tickets")
        if risk.get("peak_used") is not None and risk.get("snapshot_peak") != risk.get("peak_used"):
            print(f"    the drawdown is measured off the report's peak {risk['peak_used']:,.2f}, above this "
                  f"snapshot's own {risk['snapshot_peak']:,.2f}: {risk['peak_source']}")
        if risk.get("kill_switch"):
            print(f"    KILL: the durable halt is latched ({risk['kill_switch'].get('reason') or 'halted'}) "
                  f"and R9/R16 now reject every entry ticket")
            print_switch(risk["switch_mirror"])
    if budget:
        print(f"  intent {result.get('intent')}: "
              + ("every position the fresh snapshot shows closes in this batch"
                 if result.get("intent") == "FINAL_EXIT" else
                 "every ticket in this batch reduces exposure; nothing opens"))
        print(f"  ORDER BUDGET before the first ticket: {budget['used']} used + {budget['exit_tickets']} exit "
              f"tickets of the {budget['cap']}-order competition cap since {budget['since'][:10]} ET = "
              f"{budget['left_after_exit']} left, so {budget['rounds_affordable']} of "
              f"{budget['rounds_needed']} five-minute reprice rounds are affordable")
        print(f"    counted from the journal ({budget['journal_used']}) and the order-history file "
              f"({budget['history_window_used']}); the larger is used. "
              f"{budget['emergency_reserve']} of the cap is the emergency reserve this flatten spends.")
        if budget["over_cap"]:
            print(f"    OVER THE CAP by {-budget['left_after_exit']} order(s): R6 holds every ticket, so the "
                  f"book cannot be flattened inside the frozen rules - say so on Slack and close by hand")
        if budget["note"]:
            print(f"    note: {budget['note']}")
    print(f"  {summary['tickets']} tickets: {summary['buy']} BUY, {summary['sell']} SELL; "
          f"{summary['submit']} in the submit list, {summary['rejected']} rejected")
    print(f"  gross after: long {show(summary['long_gross_after'])}, short "
          f"{show(summary['short_gross_after'])}; margin after "
          f"{'unknown' if summary['margin_after'] is None else format(summary['margin_after'], '.4%')} "
          f"(cap {summary['margin_cap']:.0%})")
    print(f"  positions after {summary['positions_after']} of {summary['max_positions']} "
          f"({summary['shorts_after']} short); largest {summary['largest_ticker']} "
          f"{show(summary['largest_dollars'])} = "
          f"{'unknown' if summary['largest_vs_cap'] is None else format(summary['largest_vs_cap'], '.2%')} "
          f"of the ${summary['ticker_cap_dollars']:,.0f} single-name cap")
    print(f"  estimated fees {summary['estimated_fees_usd']:.2f} at "
          f"{summary['order_platform_fee']} per order; {summary['executed_today']} executed today of a "
          f"{summary['order_budget']} budget ({summary['budget_left_today']} left)")
    print(f"  skipped {len(summary['skipped'])} with reasons, blocked {len(summary['blocked'])}")
    switch = result.get("switch") or {}
    if switch:
        state = switch.get("state") or {}
        print(f"  shared switch {switch.get('group')}: "
              f"{'unread, no file fetched' if switch.get('unread') else state.get('state')} "
              f"from {switch.get('file') or switch_cache_path()} - {switch['decision']['reasons'][0]}")
    for line in summary["slack_report"][:10]:
        print(f"    {line}")
    final = result.get("final_exit") or {}
    for line in (final.get("outside_target_book") or [])[:8]:
        print(f"    closing a position outside the target book: {line}")
    if final.get("outside_target_book"):
        print(f"    {len(final['outside_target_book'])} name(s) outside the target book close too: the "
              f"scheduled exit flattens the whole account, not only the strategy book")
    for row in summary["skipped"][:5]:
        print(f"    skipped {row['symbol']}: {row['reason']}")
    for row in rejected[:5]:
        print(f"    rejected ticket {row['n']} {row['side']} {row['quantity']} {row['symbol']}: "
              f"{row['action']} on {', '.join(row['failed_rules'])}")
    if len(rejected) > 5:
        print(f"    {len(rejected) - 5} more rejected tickets in {out.name}")
    for row in (result.get("blockers") or [])[:5]:
        print(f"    blocker: {row['symbol']}: {row['reason']}")
    if not result["submit"]:
        print("  NOTHING TO SUBMIT: no ticket passes the rules with the evidence given (each ticket "
              "carries its gate checks)")
    if not result["execution_enabled_in_config"]:
        print("  NOTE live/config.pstar.json has execution_enabled false: passing tickets above are not "
              "marked execution_allowed by this release")


# ------------------------------------------------------------------ record
def command_record(args) -> int:
    now = args.as_of or datetime.now(timezone.utc)
    plan_path = Path(args.plan)
    plan = read_json(plan_path)
    account, mode = str(plan["account_id"]), plan["mode"]
    ticket = next((item for item in plan["tickets"] if item["n"] == args.ticket), None)
    if ticket is None:
        raise GoliveError(f"{plan_path.name} has no ticket {args.ticket}")
    raw = read_json(args.response)
    if not isinstance(raw, dict):
        raise GoliveError(f"{args.response}: a submission response must be one MCP object")
    code, message, data = raw.get("ret_code"), raw.get("ret_msg"), raw.get("data")
    data = data if isinstance(data, dict) else {}
    order_id = text(data.get("order_id"))
    previous = ticket.get("order_id")
    if previous and order_id and previous != order_id:
        raise GoliveError(f"ticket {args.ticket} already has order_id {previous}; refusing to record "
                          f"{order_id} - a ticket that has been sent is never sent again")
    if previous:
        ticket["order_id"] = previous
    # A second attempt on a ticket that produced no order_id is exactly where a duplicate is born: the
    # submission is not idempotent and `text` is not a key, so nothing resends without a fresh history.
    if not previous and int(ticket.get("attempts") or 0) > 0:
        if not args.orders:
            raise GoliveError(f"ticket {args.ticket} already had {ticket['attempts']} attempt(s) with no "
                              f"order recorded: --orders with a fresh sim_trade_history_order_list response "
                              f"is required before any resend")
        history = parse_orders(data_of(read_json(args.orders), "sim_trade_history_order_list", args.orders))
        match = find_matching_order(history, ticket)
        if match:
            raise GoliveError(f"refusing to resend ticket {args.ticket}: order {match['order_id']} on "
                              f"{match['symbol']} {match['side']} {match['quantity']} (text "
                              f"{match['client_order_id']!r}, submitted {match['submitted_at']}) is "
                              f"already in the broker history")
    ticket["attempts"] = int(ticket.get("attempts") or 0) + 1
    source = provenance(args.response)
    ticket["submissions"] = list(ticket.get("submissions") or []) + [{
        "at": iso(now), "response_file": source["file"], "response_sha256": source["sha256"],
        "ret_code": code, "ret_msg": message, "order_id": order_id, "status_code": data.get("status")}]
    if order_id:
        ticket["order_id"] = order_id
        ticket["status"] = "SUBMITTED"
        ticket["submitted_at"] = iso(now)
        ticket.setdefault("reprice_round", 0)
    else:
        ticket["status"] = "ERROR"
        ticket["error"] = f"ret_code={code} ret_msg={message!r}"
    write_json(plan_path, plan)
    journal_append(args.journal, "GOLIVE_RECORD", account, mode, now,
                   ticket.get("error") or f"order_id {order_id}",
                   "SUBMITTED" if order_id else "SUBMIT_FAILED", symbol=ticket["symbol"],
                   intent=ticket["intent"], target_checksum=(plan.get("targets") or {}).get("checksum"))
    print(f"RECORD ticket {args.ticket} {ticket['side']} {ticket['quantity']} {ticket['symbol']}: "
          f"{ticket['status']}" + (f" order_id {order_id}" if order_id else f" {ticket.get('error')}"))
    return 0 if order_id else 2


def find_matching_order(history: list, ticket: dict) -> dict | None:
    """Exact client id first, then the same shape we placed after this ticket was first attempted."""
    for row in history:
        if row.get("client_order_id") and row["client_order_id"] == ticket["client_order_id"]:
            return row
    first = min((entry.get("at") or "" for entry in ticket.get("submissions") or ()), default="")
    for row in history:
        if (row.get("symbol") == ticket["symbol"] and row.get("side") == ticket["side"]
                and row.get("quantity") == ticket["quantity"]
                and str(row.get("client_order_id") or "").startswith("pstar-")
                and (row.get("submitted_at") or "") >= first):
            return row
    return None


def find_order(history: list, ticket: dict) -> dict | None:
    for row in history:
        if row.get("order_id") == ticket.get("order_id"):
            return row
    for row in history:
        if row.get("client_order_id") == ticket["client_order_id"]:
            return row
    return None


# ------------------------------------------------------------------ reprice
def command_reprice(args) -> int:
    now = args.as_of or datetime.now(timezone.utc)
    plan_path = Path(args.plan)
    plan = read_json(plan_path)
    config = load_config(args.config)
    working = set(args.working or ())
    history = parse_orders(data_of(read_json(args.orders), "sim_trade_history_order_list", args.orders),
                                      set(args.working or ()))
    quotes = parse_quotes(read_json(args.quotes))
    evidence = read_json(args.evidence) if args.evidence else {}
    snapshot_file = Path(args.snapshot) if getattr(args, "snapshot", None) else None
    if snapshot_file is not None:
        fresh = read_json(snapshot_file)
        if str(fresh.get("account_id")) != str(plan["account_id"]):
            raise GoliveError(f"{snapshot_file}: the snapshot is for {fresh.get('account_id')}, but the "
                              f"plan is for {plan['account_id']}")
    # Every reprice round re-reads the flag: a pause that arrives mid-batch must stop the next ticket, not
    # wait for the batch to finish. A reduction plan keeps repricing whatever the switch says.
    side = str((evidence or {}).get("side") or (plan.get("evidence") or {}).get("side") or "local")
    switch = switch_of(args, load_switch_ops(config, plan.get("targets"), args.ops), config, now,
                       str(plan.get("intent") or "ENTRY"),
                       CLOUD_LEASE if side == "cloud" else LOCAL_LEASE)
    allow_new = bool(switch["decision"]["allow"])
    plan_intent = str(plan.get("intent") or "ENTRY")
    changes, blockers = [], []
    next_index = max([ticket["n"] for ticket in plan["tickets"]] or [0])
    for ticket in list(plan["tickets"]):
        if ticket.get("status") != "SUBMITTED" or not ticket.get("order_id"):
            continue
        row = find_order(history, ticket)
        if row is None:
            blockers.append({"symbol": ticket["symbol"],
                             "reason": f"ticket {ticket['n']} order_id {ticket['order_id']} is not in the "
                                       f"order history file: reconcile before repricing"})
            continue
        ticket["broker_state"] = row["state"]
        ticket["broker_status_code"] = row["status_code"]
        ticket["filled"] = row["filled"]
        ticket["remaining"] = row["remaining"]
        if row["state"] == "FILLED":
            ticket["status"] = "FILLED"
            continue
        remaining = row["remaining"] or 0
        if remaining <= 0:
            blockers.append({"symbol": ticket["symbol"],
                             "reason": f"ticket {ticket['n']} shows no remaining quantity in state "
                                       f"{row['state'].lower()}"})
            continue
        waited = (now - timestamp(ticket.get("submitted_at") or iso(now))).total_seconds()
        if waited < REPRICE_AFTER_SECONDS:
            changes.append(f"ticket {ticket['n']} {ticket['symbol']}: only {waited:.0f}s old, 5 minutes "
                           f"not elapsed - not repriced")
            continue
        rounds = int(ticket.get("reprice_round") or 0)
        intent = str(ticket.get("intent") or plan_intent)
        round_cap = reprice_round_cap(config, intent)
        to_market = intent == "FINAL_EXIT" and market_flatten_window(config, now)
        live = row["state"] in ("WORKING", "PARTIAL")
        ticket["cancel"] = {"needed": live, "tool": "sim_trade_cancel_order",
                            "payload": {"acc_id": str(plan["account_id"]), "market": MARKET_ID,
                                        "order_id": ticket["order_id"]}}
        if row["state"] == "UNKNOWN":
            blockers.append({"symbol": ticket["symbol"],
                             "reason": f"ticket {ticket['n']} status code {row['status_code']} is "
                                        f"unmapped: pass --working <code> for a code you have proved means "
                                        f"a live order, or cancel it by hand"})
            continue
        if not allow_new:
            ticket["status"] = "skipped_unfilled"
            ticket["note"] = f"cancel only: {switch['decision']['reasons'][0]}"
            changes.append(f"ticket {ticket['n']}: cancel {ticket['order_id']}, no reissue - "
                           f"{switch['decision']['reasons'][0]}")
            continue
        if rounds >= round_cap:
            ticket["status"] = "skipped_unfilled"
            ticket["note"] = (f"cancel only: {rounds} reprice rounds done (the cap for intent {intent} is "
                              f"{round_cap}), {remaining} share(s) left"
                              if live else f"{rounds} reprice rounds done (cap {round_cap} for {intent}), "
                                           f"{remaining} share(s) left, already {row['state'].lower()} "
                                           f"at the broker")
            changes.append(f"ticket {ticket['n']}: {'cancel ' + ticket['order_id'] + ', ' if live else ''}"
                           f"marked skipped_unfilled")
            continue
        quote = quotes.get(ticket["symbol"]) or {}
        if to_market:
            price = None
            basis = (f"reprice round {rounds + 1}: MARKET - the final-exit flatten deadline "
                     f"{config['final_flatten_at']} passed and {remaining} share(s) are still open")
        else:
            price, reason = marketable_limit(quote, ticket["side"])
            if price is None:
                blockers.append({"symbol": ticket["symbol"],
                                 "reason": f"ticket {ticket['n']} cannot be repriced: {reason}"})
                continue
            basis = (f"reprice round {rounds + 1} off {quote.get('bid')}/{quote.get('ask')} bid/ask: "
                     f"{ticket['side'].lower()} {-PRICE_OFFSET:+.2%}, cent through the touch")
        next_index += 1
        child = dict(ticket["order"])
        child["quantity"] = remaining
        child["order_type"] = "MARKET" if to_market else "LIMIT"
        child["limit_price"] = price
        child["arrival_price"] = quote.get("price")
        child["client_order_id"] = f"{ticket['client_order_id'][:88]}-r{rounds + 1}"
        child["price_basis"] = basis
        new_ticket = {"n": next_index, "order": child, "symbol": child["symbol"], "side": child["side"],
                      "quantity": remaining, "limit_price": price, "intent": child["intent"],
                      "sleeve": child.get("sleeve"), "arrival_price": child["arrival_price"],
                      "price_basis": child["price_basis"], "client_order_id": child["client_order_id"],
                      "tool": "sim_trade_input_order", "payload": paper_payload(child, MARKET_ID),
                      "gate": {"action": "DEFER", "eligible": False, "failed_rules": ["send_after_cancel"],
                               "checks": [{"rule": "sequence", "passed": False,
                                           "threshold": f"send only after the cancel of "
                                                        f"{ticket['order_id']} is confirmed"}]},
                      "status": "PLANNED", "attempts": 0, "reprice_round": rounds + 1,
                      "parent": ticket["n"], "submissions": []}
        new_ticket["gate"] = gate_reprice_ticket(config, plan, evidence, quotes, history, child, ticket,
                                                 now, working, args.journal, snapshot_file)
        new_ticket["submit"] = bool(new_ticket["gate"]["eligible"])
        if snapshot_file is not None:
            new_ticket["reprice_snapshot"] = provenance(snapshot_file)
        plan["tickets"].append(new_ticket)
        ticket["status"] = "REPRICING"
        ticket["child"] = next_index
        changes.append(f"ticket {ticket['n']}: {'cancel ' + ticket['order_id'] + ', ' if live else ''}"
                       f"reissue {remaining} share(s) of {ticket['symbol']} "
                       f"{'at the market' if to_market else f'at {price}'} as ticket "
                       f"{next_index} (round {rounds + 1})")
    plan["repriced_at"] = iso(now)
    plan["blockers"] = blockers
    plan["switch"] = {key: value for key, value in switch.items() if key != "read_command"}
    write_json(plan_path, plan)
    journal_append(args.journal, "GOLIVE_REPRICE", str(plan["account_id"]), plan["mode"], now,
                   "; ".join(changes) or "no ticket was due for repricing",
                   "REPRICE" if changes else "REPRICE_NONE",
                   target_checksum=(plan.get("targets") or {}).get("checksum"), run_id=plan.get("run_id"))
    print(f"REPRICE {plan_path.name}: {len(changes)} change(s)")
    market = sum(1 for line in changes if " at the market " in line)
    if market:
        print(f"  {market} ticket(s) go out as MARKET: the final-exit flatten deadline "
              f"{config['final_flatten_at']} passed, which is the only case R7 allows a market order")
    for line in changes:
        print(f"  {line}")
    for row in blockers:
        print(f"  blocker: {row['symbol']}: {row['reason']}")
    print(f"  {switch_summary(switch)}")
    if not allow_new:
        switch_artifact(args, now, str(plan["account_id"]), plan["mode"], config, switch, history)
    return 0


def gate_reprice_ticket(config: dict, plan: dict, evidence: dict, quotes: dict, history: list,
                        child: dict, parent: dict, now: datetime, working, journal_dir: Path,
                        snapshot_file=None) -> dict:
    """Re-gate a repriced ticket against the account and the rules, with its parent already cancelled."""
    def hold(reason: str) -> dict:
        return {"action": "HOLD", "eligible": False, "execution_allowed": False,
                "failed_rules": ["reprice_gate"], "order_fingerprint": None,
                "checks": [{"id": "reprice_gate", "passed": False, "threshold": reason,
                            "observed": None, "action": "HOLD"}]}

    path = snapshot_file or (plan["snapshot"] or {}).get("file_full")
    if not path:
        return hold("the plan records no snapshot file to re-gate against, and none was given")
    try:
        snapshot = read_json(path)
        targets = load_targets(Path(plan["targets"]["file_full"]), config)
    except (GoliveError, OSError) as exc:
        return hold(str(exc))
    book = sizing_quotes(quotes)
    pending, _ = pending_from_history([row for row in history
                                       if row["order_id"] != parent["order_id"]], now, working)
    executed, seen = executed_from_history(history, now)
    stored = plan.get("eligibility_records")
    if not stored:
        return hold("this plan carries no eligibility records: re-plan it, then reprice from the new plan")
    # The fresh --quotes file decides instrument, halt and price freshness again; the borrow capacity the
    # plan was approved on is the one carried in the plan, because reprice takes no borrow input.
    borrow = {name: {"max_sell_short_qty": row.get("short_capacity_shares")}
              for name, row in stored.items() if row.get("short_capacity_shares") is not None}
    want = dict(plan.get("target_shares") or {})
    if child["symbol"] not in want:
        want[child["symbol"]] = -child["quantity"] if child["side"] == "SELL" else child["quantity"]
    records = eligibility_records(config, quotes, borrow, want,
                                  (evidence.get("borrow") or {}).get("easy_to_borrow_proxy"))
    siblings = [ticket["order"] for ticket in plan["tickets"]
                if ticket["gate"].get("action") == "ALLOW" and ticket["n"] != parent["n"]]
    orders = siblings + [child]
    context = build_context(config, snapshot, targets, book, records, orders, pending, executed, seen,
                            evidence, now)
    single = {**context, "quotes": {**book, child["symbol"]: gate_quote(quotes.get(child["symbol"]) or {},
                                                                        child["side"])}}
    journal = Journal(Path(journal_dir))
    try:
        return journal.evaluate_and_record(dict(child), bind_approvals(single, child, config,
                                                           evidence.get("approval") or {}), config)
    finally:
        journal.close()



# ------------------------------------------------------------------ reconcile
def command_reconcile(args) -> int:
    now = args.as_of or datetime.now(timezone.utc)
    plan_path = Path(args.plan)
    plan = read_json(plan_path)
    config = load_config(args.config)
    account, mode = str(plan["account_id"]), plan["mode"]
    snapshot = read_json(args.snapshot)
    if str(snapshot.get("account_id")) != account:
        raise GoliveError(f"{args.snapshot}: the snapshot is for {snapshot.get('account_id')}, but the "
                          f"plan is for {account}")
    history = parse_orders(data_of(read_json(args.orders), "sim_trade_history_order_list", args.orders),
                                      set(args.working or ()))
    working = set(args.working or ())
    equity = snapshot["equity"]

    fills, missing, unresolved, open_orders, slips, seen_orders = [], [], [], [], [], set()
    for ticket in plan["tickets"]:
        row = find_order(history, ticket)
        if row is None:
            if ticket.get("order_id"):
                missing.append({"n": ticket["n"], "symbol": ticket["symbol"], "side": ticket["side"],
                                "quantity": ticket["quantity"], "order_id": ticket["order_id"],
                                "reason": "we recorded an order_id but the broker history has no row "
                                          "for it"})
            continue
        if row["order_id"] in seen_orders:
            continue
        seen_orders.add(row["order_id"])
        filled, quantity = row.get("filled") or 0, ticket["quantity"]
        arrival, average = ticket.get("arrival_price"), row.get("avg_fill_price")
        slip = None
        if filled and average and arrival:
            slip = (average / arrival - 1) * 10_000 * (1 if ticket["side"] == "BUY" else -1)
            slips.append(slip)
        fills.append({"n": ticket["n"], "symbol": ticket["symbol"], "side": ticket["side"],
                      "order_id": row["order_id"], "client_order_id": ticket["client_order_id"],
                      "ordered_quantity": quantity, "filled_quantity": filled,
                      "remaining_quantity": row.get("remaining"), "state": row["state"],
                      "status_code": row["status_code"], "avg_fill_price": average,
                      "arrival_price": arrival, "slippage_bps": None if slip is None else round(slip, 2),
                      "fee": None, "estimated_fee": config["order_platform_fee"] if filled else 0.0,
                      "submitted_at": ticket.get("submitted_at"), "updated_at": row.get("updated_at")})
        if filled < quantity and row["state"] != "WORKING":
            missing.append({"n": ticket["n"], "symbol": ticket["symbol"], "side": ticket["side"],
                            "quantity": quantity, "filled": filled,
                            "reason": f"expected {quantity} share(s) and the broker shows {filled} in "
                                      f"state {row['state'].lower()}"})
        if row["state"] in ("WORKING", "PARTIAL"):
            # Still live at the broker: not a failure to reconcile, but the batch is not finished either.
            open_orders.append({"n": ticket["n"], "symbol": ticket["symbol"], "side": ticket["side"],
                                "quantity": quantity, "filled": filled, "order_id": row["order_id"],
                                "state": row["state"], "status_code": row["status_code"],
                                "updated_at": row.get("updated_at")})
        if row["state"] in ("UNKNOWN", "UNREADABLE"):
            unresolved.append({"n": ticket["n"], "order_id": row["order_id"], "symbol": ticket["symbol"],
                               "status_code": row["status_code"],
                               "reason": "the status code is not mapped, so this fill cannot be called "
                                         "complete; prove it with --working and re-reconcile"})

    held = holdings(snapshot)
    wanted = {str(name): int(shares) for name, shares in (plan.get("target_shares") or {}).items()}
    flatten = str(plan.get("intent") or "") == "FINAL_EXIT"
    # A flatten has no band to hide inside: after the scheduled exit every share still on the book is a
    # finding, so the deviations list is the residual position list the 12:55 check has to show.
    band = 0.0 if flatten else config["trade_band_of_equity"] * equity
    deviations = []
    for name in sorted(set(wanted) | set(held)):
        difference = held.get(name, 0) - wanted.get(name, 0)
        price = (plan.get("quotes", {}).get(name) or {}).get("price")
        dollars = None if price is None else abs(difference) * price
        if difference and (price is None or dollars > band):
            deviations.append({"symbol": name, "target_shares": wanted.get(name, 0),
                               "held_shares": held.get(name, 0), "share_difference": difference,
                               "dollars": None if dollars is None else round(dollars, 2),
                               "weight": (plan.get("weights") or {}).get(name),
                               "note": "no quote price to size the deviation" if price is None else
                                       ("still open after the scheduled final exit" if flatten else
                                        f"above the {config['trade_band_of_equity']:.2%}-of-equity band")})
    state = measure(config, snapshot["positions"], equity, snapshot["initial_equity"],
                    snapshot["peak_equity"])
    executed, _ = executed_from_history(history, now)
    budget = config["competition_order_cap"] - config["emergency_flatten_reserve"]
    result = {"reconciled_at": iso(now), "mode": mode, "account_id": account, "plan": str(plan_path),
              "intent": plan.get("intent"),
              "targets": plan.get("targets"), "equity": equity, "snapshot_as_of": snapshot.get("as_of"),
              "margin": state["margin"], "margin_cap": config["margin_cap"], "long_gross": state["long_gross"],
              "short_gross": state["short_gross"], "positions": state["positions"],
              "positions_cap": config["max_positions"], "max_ticker": state["max_ticker"],
              "max_ticker_dollars": None if state["max_ticker"] is None else
              round(abs(state["values"][state["max_ticker"]]), 2),
              "max_ticker_vs_initial": state["max_ticker_vs_initial"],
              "ticker_cap_of_initial": config["ticker_cap_of_initial"],
              "drawdown": state["drawdown"], "tickets": len(plan["tickets"]),
              "orders_at_broker": len(seen_orders),
              "filled_complete": sum(1 for row in fills if row["state"] == "FILLED"),
              "missing_fills": missing, "unresolved_status_codes": unresolved,
              "open_orders": open_orders, "deviations": deviations,
              "executed_orders_today": len(executed), "order_budget": budget,
              "budget_left": budget - len(executed), "competition_order_cap": config["competition_order_cap"],
              "emergency_reserve": config["emergency_flatten_reserve"],
              "average_slippage_bps": None if not slips else round(sum(slips) / len(slips), 2),
              "estimated_fees_usd": round(len(seen_orders) * config["order_platform_fee"], 2),
              "actual_fees": None,
              "fill_identity": "one cumulative row per order: the paper API gives no per-fill identity",
              "unverified_fields": sorted({"actual_fees", "commission", "fill_identity"}
                                           | set(snapshot.get("unverified_fields") or ())),
              # For a scheduled exit "complete" means flat: every ticket resolved is not enough while a
              # share is still on the book, because the 12:50 ET check is the one that has to be true.
              "complete_reconciliation": bool(not missing and not unresolved and not open_orders
                                              and (not flatten or state["positions"] == 0))}
    out_dir = plan_path.parent
    alerts = reconciliation_alerts(result, config, plan, fills, now)
    alert_path, added = append_alerts(out_dir / "alerts.json", alerts)
    result["alerts"] = {"file": relative(alert_path), "seen": len(alerts), "written_now": added}
    write_json(out_dir / "reconcile.json", result)
    write_json(out_dir / "summary.md", summary_text(result, fills, plan, alerts))
    written = append_fills(args.journal, result, fills, plan, now)
    journal_append(args.journal, "GOLIVE_RECONCILE", account, mode, now,
                   f"{result['filled_complete']} of {result['orders_at_broker']} orders fully filled, "
                   f"{len(missing)} missing, {len(open_orders)} still live at the broker, "
                   f"{len(deviations)} {'still open after the final exit' if flatten else 'deviations above the band'}, margin "
                   f"{'unknown' if state['margin'] is None else format(state['margin'], '.4%')}",
                   "RECONCILE", target_checksum=(plan.get("targets") or {}).get("checksum"),
                   run_id=plan.get("run_id"))
    print(f"RECONCILE {relative(out_dir)}")
    print(f"  equity {equity:,.2f}, margin {'unknown' if state['margin'] is None else format(state['margin'], '.4%')} "
          f"of a {config['margin_cap']:.0%} cap, {state['positions']} positions of "
          f"{config['max_positions']}, drawdown {state['drawdown']:.4%}")
    print(f"  largest {state['max_ticker']} at {state['max_ticker_vs_initial']:.2%} of initial assets "
          f"(trim threshold {config['ticker_cap_of_initial']:.0%})")
    print(f"  {result['orders_at_broker']} orders at the broker, {result['filled_complete']} fully filled, "
          f"{len(missing)} missing, {len(open_orders)} still live at the broker, "
          f"{len(unresolved)} unresolved status codes, {written} fill row(s) journaled")
    print(f"  average slippage {show(result['average_slippage_bps'], 'g')} bps; estimated fees "
          f"{result['estimated_fees_usd']:.2f} (actual fees: null, the paper order row has no fee field)")
    print(f"  {len(deviations)} {'position(s) still open' if flatten else 'deviation(s)'} "
          f"{'after the scheduled final exit' if flatten else 'above the ' + format(config['trade_band_of_equity'], '.2%') + '-of-equity band'}"
          f"; {len(executed)} of {budget} executed orders used today "
          f"({config['emergency_flatten_reserve']} reserved for an emergency flatten of "
          f"{config['competition_order_cap']})")
    if flatten:
        print(f"  FINAL EXIT: {'the account is flat' if not held or state['positions'] == 0 else str(state['positions']) + ' position(s) still open'} "
              f"as of {snapshot.get('as_of')}"
              + ("" if not deviations else " - each one is listed in reconcile.json and the summary")
              + (f"; {len(open_orders)} exit order(s) are still live at the broker" if open_orders else ""))
    print(f"  {len(alerts)} anomaly alert(s) for the Slack feed, {added} new row(s) in "
          f"{relative(alert_path)}")
    return 0 if result["complete_reconciliation"] else 2


def summary_text(result: dict, fills: list, plan: dict, alerts: list = ()) -> str:
    flatten = str(plan.get("intent") or "") == "FINAL_EXIT"
    deviation_label = "positions still open after the scheduled final exit" if flatten \
        else "deviations above the band"
    lines = [f"# Go-live reconciliation {result['reconciled_at']} - account {result['account_id']}", "",
             f"- equity {result['equity']:,.2f}; margin "
             f"{'unknown' if result['margin'] is None else format(result['margin'], '.4%')} of a "
             f"{result['margin_cap']:.0%} cap; {result['positions']} positions "
             f"(cap {result['positions_cap']}); drawdown {result['drawdown']:.4%}",
             f"- long gross {result['long_gross']:,.2f}; short gross {result['short_gross']:,.2f}",
             f"- largest name {result['max_ticker']} at {result['max_ticker_vs_initial']:.2%} of initial "
             f"assets against the {result['ticker_cap_of_initial']:.0%} trim threshold "
             f"({show(result['max_ticker_dollars'])} USD)",
             f"- tickets {result['tickets']}; orders at the broker {result['orders_at_broker']}; fully "
             f"filled {result['filled_complete']}; missing {len(result['missing_fills'])}; still live "
             f"{len(result['open_orders'])}; unresolved "
             f"status codes {len(result['unresolved_status_codes'])}",
             f"- executed orders today {result['executed_orders_today']} of the {result['order_budget']} "
             f"budget (the competition cap is {result['competition_order_cap']} and "
             f"{result['emergency_reserve']} stay in reserve for an emergency flatten)",
             f"- average slippage against the arrival quote {show(result['average_slippage_bps'], 'g')} bps; "
             f"estimated fees {result['estimated_fees_usd']:.2f} USD; actual fees null - the paper order "
             f"row exposes no fee field",
             f"- {deviation_label}: {len(result['deviations'])}", "",
             "| # | side | filled/ordered | symbol | state | avg fill | arrival | slippage bps |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in fills:
        lines.append(f"| {row['n']} | {row['side']} | {row['filled_quantity']}/{row['ordered_quantity']} "
                     f"| {row['symbol']} | {row['state']} | {show(row['avg_fill_price'], 'g')} | "
                     f"{show(row['arrival_price'], 'g')} | {show(row['slippage_bps'], 'g')} |")
    for label, rows in (("missing fills", result["missing_fills"]),
                        ("unresolved status codes", result["unresolved_status_codes"])):
        if rows:
            lines += ["", f"## {label}"]
            for row in rows:
                lines.append(f"- ticket {row['n']} {row['symbol']}: {row['reason']}")
    if result["open_orders"]:
        lines += ["", "## still live at the broker"]
        for row in result["open_orders"]:
            lines.append(f"- ticket {row['n']} {row['symbol']} {row['side']}: {row['filled']} of "
                         f"{row['quantity']} share(s), state {row['state'].lower()}, order_id "
                         f"{row['order_id']} - the batch is not finished while an order is live")
    if result["deviations"]:
        lines += ["", f"## {deviation_label}"]
        for row in result["deviations"]:
            lines.append(f"- {row['symbol']}: target {row['target_shares']}, held {row['held_shares']} "
                         f"({row['share_difference']:+d} share(s), {show(row['dollars'])} USD) - "
                         f"{row['note']}")
    if flatten:
        lines += ["", f"Final-exit verdict at {result['reconciled_at']}: "
                      f"{'FLAT - no position remains on the account' if result['positions'] == 0 else str(result['positions']) + ' position(s) still open, listed above'}"
                      f" from a snapshot taken {result['snapshot_as_of']}."]
    lines += ["", f"Unverified fields, kept null rather than zero: "
                  f"{', '.join(result['unverified_fields']) or 'none'}.",
              f"Complete reconciliation: {result['complete_reconciliation']}, because fill identity is "
              f"{result['fill_identity']} and every mapped status code is evidenced."]
    if alerts:
        lines += ["", f"## anomalies ({len(alerts)}) - also written to alerts.json for the Slack feed"]
        for row in alerts:
            lines.append(f"- {row['kind']} {row['symbol'] or result['account_id']}"
                         f"{' order ' + str(row['order_id']) if row['order_id'] else ''}: {row['detail']}")
    lines.append("")
    return "\n".join(lines)


def append_fills(journal_dir: Path, result: dict, fills: list, plan: dict, now: datetime) -> int:
    """One fill row per order that actually filled; identity is the order id, never a guess."""
    written = 0
    journal = Journal(Path(journal_dir))
    try:
        for row in fills:
            if not row["filled_quantity"] or not row["avg_fill_price"]:
                continue
            try:
                journal.append("fills", {"event_id": uuid.uuid4().hex, "timestamp": iso(now),
                                         "mode": plan["mode"], "account_id": str(plan["account_id"]),
                                         "run_id": plan.get("run_id"), "session_id": "desktop-chat",
                                         "order_id": row["order_id"], "fill_id": row["order_id"],
                                         "symbol": row["symbol"], "side": row["side"],
                                         "quantity": row["filled_quantity"], "price": row["avg_fill_price"],
                                         "commission": None, "platform_fee": None, "regulatory_fee": None,
                                         "slippage_bps": row["slippage_bps"],
                                         "arrival_price": row["arrival_price"],
                                         "fill_identity": result["fill_identity"]})
                written += 1
            except ValueError:                       # already journaled by order/fill identity
                continue
    finally:
        journal.close()
    return written


# ------------------------------------------------------------------ anomalies for the Slack feed
ALERT_KINDS = ("broker_rejection", "gate_rejection", "unfilled", "partial_fill", "slippage",
               "unexpected_position", "order_budget", "tool_error")


def alert_row(kind: str, account: str, mode: str, now: datetime, detail: str, symbol=None, order_id=None,
              source=None) -> dict:
    if kind not in ALERT_KINDS:
        raise GoliveError(f"unknown alert kind {kind!r}: use one of {', '.join(ALERT_KINDS)}")
    return {"at": iso(now), "kind": kind, "account_id": account, "mode": mode, "symbol": symbol,
            "order_id": order_id, "detail": detail, "source": source}


def append_alerts(path: Path, rows: list[dict]) -> tuple[Path, int]:
    """Append to the day's alert file without repeating what is already there; the monitor posts this."""
    data = read_json(path) if path.exists() else None
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(alerts, list):
        alerts = []
        data = {"created_by": "live/golive.py", "account_id": rows[0]["account_id"] if rows else None,
                "alerts": alerts,
                "note": ("one row per anomaly the automatic run must not swallow: the run summary carries "
                         "the same lines and the cloud monitor posts this file to Slack")}
    known = {(row.get("kind"), row.get("symbol"), row.get("order_id")) for row in alerts}
    added = 0
    for row in rows:
        if (row["kind"], row["symbol"], row["order_id"]) in known:
            continue
        alerts.append(row)
        known.add((row["kind"], row["symbol"], row["order_id"]))
        added += 1
    write_json(path, data)
    return path, added


def reconciliation_alerts(result: dict, config: dict, plan: dict, fills: list, now: datetime) -> list[dict]:
    """The anomalies reconcile can see on its own: broker refusals, bad fills, stray positions, budget.

    A ticket the gate refused is only an anomaly when it is not the halt speaking: R9/R16 rejection is the
    pause switch working, and `pause` already alerted, so repeating it per ticket would bury the real news.
    """
    account, mode = result["account_id"], result["mode"]
    tickets = {row["n"]: row for row in plan.get("tickets") or ()}
    band = config["limit_band_bps"]
    rows = []
    for fill in fills:
        if fill["status_code"] == STATUS_REJECTED:
            rows.append(alert_row("broker_rejection", account, mode, now,
                                  f"the broker refused this ticket (status {STATUS_REJECTED}) and filled "
                                  f"{fill['filled_quantity']} of {fill['ordered_quantity']} share(s)",
                                  fill["symbol"], fill["order_id"]))
        if fill["slippage_bps"] is not None and fill["slippage_bps"] > band:
            rows.append(alert_row("slippage", account, mode, now,
                                  f"filled {fill['filled_quantity']}/{fill['ordered_quantity']} at "
                                  f"{fill['avg_fill_price']} against an arrival quote of "
                                  f"{fill['arrival_price']}: {fill['slippage_bps']} bps beyond the "
                                  f"{band} bps limit band", fill["symbol"], fill["order_id"]))
        if (fill["filled_quantity"] or 0) >= fill["ordered_quantity"]:
            continue
        left = (fill["remaining_quantity"] if fill["remaining_quantity"] is not None
                else fill["ordered_quantity"] - (fill["filled_quantity"] or 0))
        if (tickets.get(fill["n"]) or {}).get("status") == "skipped_unfilled":
            rows.append(alert_row("unfilled", account, mode, now,
                                  f"{left} of {fill['ordered_quantity']} share(s) unfilled after the last "
                                  f"reprice round", fill["symbol"], fill["order_id"]))
        elif fill["state"] not in UNRESOLVED_STATES:
            rows.append(alert_row("partial_fill", account, mode, now,
                                  f"{fill['filled_quantity']} of {fill['ordered_quantity']} share(s) "
                                  f"filled and the order is {fill['state'].lower()} at the broker",
                                  fill["symbol"], fill["order_id"]))
    for ticket in plan.get("tickets") or ():
        gate = ticket.get("gate") or {}
        if ticket.get("submit") or set(gate.get("failed_rules") or ()) <= {"R9", "R16"}:
            continue
        reasons = [str(row.get("threshold")) for row in gate.get("checks") or () if not row.get("passed")]
        rows.append(alert_row("gate_rejection", account, mode, now,
                              f"never sent: {', '.join(gate.get('failed_rules') or ())} held this ticket "
                              f"down - {'; '.join(reasons)}",
                              ticket["symbol"], ticket.get("order_id")))
    for row in result["deviations"]:
        if not row["target_shares"]:
            rows.append(alert_row("unexpected_position", account, mode, now,
                                  f"{row['held_shares']} share(s) held with no place in the target book "
                                  f"({show(row['dollars'])} USD): {row['note']}", row["symbol"]))
    for label, cap in (("strategy orders/day", config["max_orders_per_day"]),
                       ("competition orders", config["competition_order_cap"])):
        used = result["executed_orders_today"]
        if used > 0.8 * cap:
            rows.append(alert_row("order_budget", account, mode, now,
                                  f"{used} of {cap} {label} used, above the 80% alert line"))
    return rows


def command_alert(args) -> int:
    """Record one anomaly the run saw at the broker or in the tool, for the summary and the Slack feed."""
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    row = alert_row(args.kind, account, mode, now, args.detail, symbol=args.symbol,
                    order_id=args.order_id, source=provenance(args.file) if args.file else None)
    path, added = append_alerts(artifact_dir(now, account) / "alerts.json", [row])
    journal_append(args.journal, "GOLIVE_ALERT", account, mode, now, f"{args.kind}: {args.detail}",
                   "ALERT", symbol=args.symbol)
    print(f"ALERT {relative(path)}: {args.kind} {args.symbol or ''} "
          f"({'written' if added else 'already recorded'})")
    return 0


# ------------------------------------------------------------------ cli
def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m live.golive",
                                   description="Go-live tooling: parse, size, gate, record, reprice, "
                                               "reconcile. It never places an order.")
    root.add_argument("--journal", type=Path, default=ROOT / "journal")
    root.add_argument("--config", type=Path, help="a P* configuration file; the deployed one by default")
    root.add_argument("--ops", type=Path, help="the operational parameters file; live/ops.json by default")
    root.add_argument("--as-of", type=timestamp, help="the clock for this run; defaults to now (UTC)")
    sub = root.add_subparsers(dest="command", required=True)

    resume = sub.add_parser("resume", help="put the current mode's account back in run (no typed reply)")
    resume.add_argument("--account", required=True)
    resume.add_argument("--source", required=True,
                        help="where the instruction came from, e.g. `slack RESUME 1763...` or `user in chat`")
    resume.add_argument("--flag", type=Path, help="raw quote_user_security response for the switch group")
    resume.set_defaults(handler=command_resume)

    pause = sub.add_parser("pause", help="halt the current mode's account and list what to cancel")
    pause.add_argument("--account", required=True)
    pause.add_argument("--source", required=True,
                       help="where the instruction came from, e.g. `slack PAUSE 1763...` or `user in chat`")
    pause.add_argument("--orders", type=Path,
                       help="raw today's-order-history response: its live pstar- orders become the "
                            "cancel list")
    pause.add_argument("--working", type=int, action="append",
                       help="a broker status code you have proved means a live order (repeatable)")
    pause.add_argument("--out", type=Path, help="where to write the cancel list")
    pause.add_argument("--flag", type=Path, help="raw quote_user_security response for the switch group")
    pause.set_defaults(handler=command_pause)

    halt = sub.add_parser("halt", help="alias of pause, kept for the runbook's older wording")
    halt.add_argument("--account", required=True)
    halt.add_argument("--source", required=True,
                      help="where the instruction came from, e.g. `slack PAUSE 1763...` or `user in chat`")
    halt.add_argument("--orders", type=Path, help="raw today's-order-history response")
    halt.add_argument("--working", type=int, action="append")
    halt.add_argument("--out", type=Path, help="where to write the cancel list")
    halt.add_argument("--flag", type=Path, help="raw quote_user_security response for the switch group")
    halt.set_defaults(handler=command_pause)

    flag = sub.add_parser("flag", help="read or write the shared pause switch (the moomoo watchlist flag)")
    flag.add_argument("--account", required=True)
    flag.add_argument("--read", type=Path, help="raw quote_user_security response for the switch group")
    flag.add_argument("--set", choices=(pause_switch.PAUSED, pause_switch.RUNNING),
                      help="write the payloads that move the shared flag to this state")
    flag.add_argument("--lease", choices=(LOCAL_LEASE, CLOUD_LEASE, "none"),
                      help="who trades now, with the day token that gives the lease its expiry")
    flag.add_argument("--holder", choices=(LOCAL_LEASE, CLOUD_LEASE),
                      help="ask the decision as this lease holder (local-entry by default)")
    flag.add_argument("--targets", type=Path, help="check the sentinels against a target book too")
    flag.add_argument("--source", help="where the instruction came from, e.g. `slack PAUSE 1763...`")
    flag.set_defaults(handler=command_flag)

    alert = sub.add_parser("alert", help="record one anomaly for the summary and the Slack feed")
    alert.add_argument("--account", required=True)
    alert.add_argument("--kind", required=True, choices=ALERT_KINDS)
    alert.add_argument("--detail", required=True)
    alert.add_argument("--symbol")
    alert.add_argument("--order-id", help="the broker order id the anomaly is about")
    alert.add_argument("--file", type=Path, help="raw response that proves it, hashed into the alert")
    alert.set_defaults(handler=command_alert)

    snapshot = sub.add_parser("snapshot", help="normalise the raw cash, position and order responses")
    snapshot.add_argument("--account", required=True)
    snapshot.add_argument("--cash", type=Path, required=True)
    snapshot.add_argument("--positions", type=Path, required=True)
    snapshot.add_argument("--orders", type=Path)
    snapshot.add_argument("--accounts", type=Path, help="raw sim_trade_account_list response")
    snapshot.set_defaults(handler=command_snapshot)

    evidence = sub.add_parser("evidence", help="compute the R11/R12/R14/R17 evidence file")
    evidence.add_argument("--account", required=True)
    evidence.add_argument("--snapshot", type=Path, required=True)
    evidence.add_argument("--orders", type=Path, required=True)
    evidence.add_argument("--trigger", required=True,
                          help="what authorised this run, e.g. 'user pasted 07 Part 2' or 'user replied GO'")
    evidence.add_argument("--out", type=Path, required=True)
    evidence.add_argument("--working", type=int, action="append", default=[])
    evidence.add_argument("--side", choices=("local", "cloud"), default="local",
                          help="cloud: the Evidence is written for a scheduled Executor session, so the "
                               "release is the pinned skill versions, the lease comes from the shared flag "
                               "and the auditor and executor are two different cloud sessions")
    evidence.add_argument("--auditor-session", help="cloud mode: the Risk Auditor session id that ran "
                                                    "pstar-risk-check and produced the approval")
    evidence.add_argument("--executor-session", help="cloud mode: the Executor session id that will send; "
                                                     "R14 rejects it being the auditor session")
    evidence.add_argument("--flag", type=Path, help="cloud mode: the raw quote_user_security response for "
                                                    "PSTAR-PAUSE, which is the only lease evidence a cloud "
                                                    "session has")
    evidence.add_argument("--templates", type=Path,
                          help="cloud mode: agents/templates.json whose skill pins are checked (default)")
    evidence.add_argument("--release-lock", type=Path,
                          help="cloud mode: agents/release-lock.json to compare the pins against (default)")
    evidence.add_argument("--risk-report", type=Path,
                          help="the pstar-risk-check report for this batch; a KILL level releases the run "
                               "from the lease, because a flatten is a reduction")
    evidence.set_defaults(handler=command_evidence)

    plan = sub.add_parser("plan", help="size, price and gate the tickets")
    plan.add_argument("--account", required=True)
    plan.add_argument("--snapshot", type=Path, required=True)
    plan.add_argument("--targets", type=Path, required=True)
    plan.add_argument("--quotes", type=Path, required=True)
    plan.add_argument("--borrow", type=Path, help="max_buy_sell responses, each wrapped with its symbol")
    plan.add_argument("--orders", type=Path, help="fresh sim_trade_history_order_list response")
    plan.add_argument("--evidence", type=Path, help="operator assertions and the approval block")
    plan.add_argument("--working", type=int, action="append", default=[],
                      help="a broker status code you have proved means a live order (repeatable)")
    plan.add_argument("--smoke", action="store_true",
                      help="Stage A: four one-share tickets, opens then closes, on the test account")
    plan.add_argument("--final-exit", action="store_true",
                      help="the scheduled flatten: close every position on the account with FINAL_EXIT "
                           "intent, marketable limits from final_exit_start and MARKET after final_flatten_at")
    plan.add_argument("--risk-action", type=Path, metavar="RISK_REPORT.json",
                      help="the reduce-only risk batch: build tickets from the pstar-risk-check report "
                           "(ALERT trims and de-levers, KILL flattens every strategy position and latches "
                           "the halt) after re-measuring it on the fresh snapshot")
    plan.add_argument("--out", type=Path)
    plan.add_argument("--flag", type=Path,
                      help="raw quote_user_security response for the shared switch group; the cached read "
                           "from `golive.py flag` is used when this is omitted")
    plan.set_defaults(handler=command_plan)

    record = sub.add_parser("record", help="attach one raw submission response to its ticket")
    record.add_argument("--plan", type=Path, required=True)
    record.add_argument("--ticket", type=int, required=True)
    record.add_argument("--response", type=Path, required=True)
    record.add_argument("--orders", type=Path, help="required before any resend after a failed attempt")
    record.set_defaults(handler=command_record)

    reprice = sub.add_parser("reprice", help="cancel and reissue what has not filled after 5 minutes")
    reprice.add_argument("--plan", type=Path, required=True)
    reprice.add_argument("--orders", type=Path, required=True)
    reprice.add_argument("--quotes", type=Path, required=True)
    reprice.add_argument("--snapshot", type=Path,
                         help="a fresh normalised snapshot; the one the plan recorded by default")
    reprice.add_argument("--evidence", type=Path)
    reprice.add_argument("--working", type=int, action="append", default=[])
    reprice.add_argument("--flag", type=Path,
                         help="raw quote_user_security response: every round re-reads the shared switch")
    reprice.set_defaults(handler=command_reprice)

    reconcile = sub.add_parser("reconcile", help="compare the book to the targets and journal the fills")
    reconcile.add_argument("--plan", type=Path, required=True)
    reconcile.add_argument("--snapshot", type=Path, required=True)
    reconcile.add_argument("--orders", type=Path, required=True)
    reconcile.add_argument("--working", type=int, action="append", default=[])
    reconcile.set_defaults(handler=command_reconcile)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except (GoliveError, ValueError, KeyError, TypeError, OSError) as exc:
        print(f"GOLIVE REFUSED: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
