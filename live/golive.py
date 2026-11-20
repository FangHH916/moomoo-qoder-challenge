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

Nothing here places an order: `plan` writes tickets for a human to submit through the chat session, and
`record` only writes down what the broker answered.

usage (project venv, from the repository root; docs/GOLIVE.md gives the run order and the evidence files):
  python -m live.golive arm       --account 5322438 --reply "ARM 5322438"
  python -m live.golive halt      --account 5322438 --reason "why the halt is latched"
  python -m live.golive snapshot  --account 5322438 --cash cash.json --positions positions.json \
                                  --orders orders.json --accounts accounts.json
  python -m live.golive evidence  --account 5322438 --snapshot <file> --orders orders.json \
                                  --trigger "user pasted 07 Part 2" --out evidence.json
  python -m live.golive plan      --account 5322438 --snapshot <file> --targets <file> --quotes quotes.json \
                                  [--borrow borrow.json] [--orders orders.json] [--evidence evidence.json] \
                                  [--smoke]
  python -m live.golive record    --plan plan.json --ticket 3 --response response.json [--orders orders.json]
  python -m live.golive reprice   --plan plan.json --orders orders.json --quotes quotes.json [--working 1]
  python -m live.golive reconcile --plan plan.json --snapshot <file> --orders orders.json [--working 1]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from journal.store import Journal
from live.contracts import (PSTAR_SCHEMA_VERSION, ROOT, digest, finite, load_strategy_config, strict_json,
                            timestamp, verify)
from live.eligibility import plan_eligibility
from live.order_planner import client_order_id, holdings, plan as planner_plan, target_shares
from live.pstar import classify, read_journal
from live.relay import paper_payload
from live.risk_monitor import actions, measure
from rules.gate import ACCOUNTS, order_fingerprint
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

# The paper API returns `status` as an integer and its schema names no code table. Two codes are evidenced
# by the 2026-11-20 order history of 5322438: 4 appeared only with cum_qty == qty, and 5 only with
# cum_qty == 0 and no later update. Any other code stays UNKNOWN until an operator proves what it means
# with --working; golive never calls an order finished or still-live on a guess.
STATUS_CLOSED_NO_FILL = {5}

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

    Only `golive.py arm`, and only on the user's own typed reply, can put a row with halted=0 here, so an
    un-armed account is reported as the reason nothing can pass rather than hidden in a per-ticket
    rejection list (PRD R9/R16).
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
            "note": ("halted/unarmed: R16 rejects every strategy order until an owner rearms the account"
                     if control.get("halted") else "armed: R16 allows strategy orders while no halt is latched")}


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
    if filled > 0:
        return "PARTIAL"
    if code in working:
        return "WORKING"
    if code in STATUS_CLOSED_NO_FILL:
        return "CLOSED_NO_FILL"
    # Status 4 with less than the ordered quantity is contradictory evidence, and any other code is
    # unmapped: neither is ever called a live order without the operator proving it with --working.
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
        if submitted[:10] != day or row.get("state") == "FILLED":
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
                   run_id=None) -> dict:
    record = {"event_id": uuid.uuid4().hex, "timestamp": iso(now), "mode": mode, "account_id": account,
              "run_id": run_id or f"golive-{now.astimezone(ET).date().isoformat()}",
              "session_id": "desktop-chat", "agent": "golive", "kind": kind, "symbol": symbol,
              "strategy_id": None, "intent": intent, "action": action, "reason": reason,
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
PERMISSION_BASIS = "every submit batch needs the user's `CONFIRM` in this chat"
INDEPENDENT_CHECK_BASIS = ("local mode decided by the user 2026-11-20: the independent check is the "
                           "deterministic golive/gate run, not a separate agent; the human control is the "
                           "user's CONFIRM before any submission")
BORROW_PROXY = "sim_trade_max_buy_sell.max_sell_short_qty"
UNRESOLVED_STATES = ("WORKING", "PARTIAL", "UNKNOWN", "UNREADABLE")


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


def command_arm(args) -> int:
    """Arm the account of the current config mode on the user's exact typed reply, nothing else."""
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    config = load_config(args.config)
    if config.get("mode") != mode:
        raise GoliveError(f"the config mode is {config.get('mode')!r} but {account} is the {mode} account; "
                          f"R1 rejects every account except that mode's own")
    expected = f"ARM {account}"
    if args.reply != expected:
        raise GoliveError(f"--reply must be exactly the text the user typed: expected {expected!r}, "
                          f"received {args.reply!r}. Nothing is armed on the agent's own initiative")
    reason = f"armed by the user in chat at {iso(now)}"
    journal = Journal(Path(args.journal))
    try:
        journal.set_resume(account, reason)
    finally:
        journal.close()
    journal_append(args.journal, "GOLIVE_ARM", account, mode, now, f"{args.reply} - {reason}", "ARM",
                   config=config)
    state = kill_switch_state(args.journal, account)
    print(f"ARM {account} ({mode}): halted={state['halted']} - {reason}")
    print("  Only the user's typed reply armed this account; no order was placed and no schedule created.")
    return 0


def command_halt(args) -> int:
    """Latch the halt again through the store's own method: this writes nothing to the broker."""
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
    config = load_config(args.config)
    if config.get("mode") != mode:
        raise GoliveError(f"the config mode is {config.get('mode')!r} but {account} is the {mode} account; "
                          f"R1 rejects every account except that mode's own")
    reason = args.reason
    journal = Journal(Path(args.journal))
    try:
        journal.set_halt(account, reason)
    finally:
        journal.close()
    journal_append(args.journal, "GOLIVE_HALT", account, mode, now, reason, "HALT", config=config)
    state = kill_switch_state(args.journal, account)
    print(f"HALT {account} ({mode}): halted={state['halted']} - {reason}")
    print("  Nothing was cancelled or sent: cancel and flatten are separate, confirmed submissions.")
    return 0


def command_evidence(args) -> int:
    """Compute the evidence file: every field is measured here, so the chat agent never types it."""
    now = args.as_of or datetime.now(timezone.utc)
    account, mode = args.account, mode_for(args.account)
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
    release = release_check()
    reconciled = reconcile_attestation(history, now)
    approval_id = (f"golive-{now.astimezone(ET).date().isoformat()}-{account}-"
                   f"{now.astimezone(ET):%H%M%S}")
    expires = now + timedelta(seconds=APPROVAL_WINDOW_SECONDS)
    evidence = {
        "created_by": "live/golive.py evidence", "mode": mode, "account_id": account,
        "generated_at": iso(now), "trigger": args.trigger,
        "assertions": {
            "release_verified": release["verified"], "lease_verified": True,
            "permission_policy_verified": True, "reconciled": reconciled["reconciled"],
            "snapshot_verified_by_relay": snapshot_ok["verified"]},
        "bases": {
            "release_verified": release["reason"],
            "lease_verified": LEASE_BASIS,
            "permission_policy_verified": PERMISSION_BASIS,
            "reconciled": reconciled["reason"],
            "snapshot_verified_by_relay": ("bound to " + account + " through sim_trade_account_list and "
                                           "re-hashed from its own source files" if snapshot_ok["verified"]
                                           else "; ".join(snapshot_ok["reasons"])),
            "approval": INDEPENDENT_CHECK_BASIS},
        "approval": {"approval_id": approval_id, "issued_at": iso(now), "expires_at": iso(expires),
                     "verified_by_relay": True, "auditor_session_id": "golive-deterministic-checks",
                     "guard_session_id": "qoder-desktop-chat", "confirmed_by": "user",
                     "trigger": args.trigger, "basis": INDEPENDENT_CHECK_BASIS,
                     "window_seconds": APPROVAL_WINDOW_SECONDS},
        "borrow": {"easy_to_borrow_proxy": BORROW_PROXY,
                   "accepted_by": "the user on 2026-11-20: the paper tools expose no easy-to-borrow flag "
                                  "or pool depth, so a short passes only if max_sell_short_qty is at least "
                                  "its planned quantity"},
        "release": release, "reconcile": reconciled, "snapshot": snapshot_ok,
        "sources": {"snapshot": provenance(args.snapshot), "orders": provenance(args.orders),
                    "config": {"file": str(args.config) if args.config else "live/config.pstar.json",
                               "checksum": digest(config)}},
        "relay_note": RELAY_NOTE}
    out = Path(args.out)
    write_json(out, evidence)
    flags = evidence["assertions"]
    journal_append(args.journal, "GOLIVE_EVIDENCE", account, mode, now,
                   "release " + str(flags["release_verified"]) + ", reconciled "
                   + str(flags["reconciled"]) + ", snapshot relay "
                   + str(flags["snapshot_verified_by_relay"]) + f", approval {approval_id} until "
                   f"{iso(expires)}", "EVIDENCE", config=config)
    print(f"EVIDENCE {relative(out)}")
    print(f"  release_verified {flags['release_verified']}: {release['reason']} "
          f"(commit {release['commit'] or 'unknown'})")
    print(f"  reconciled {flags['reconciled']} ({reconciled['reason']})")
    print(f"  snapshot_verified_by_relay {flags['snapshot_verified_by_relay']} "
          f"({'; '.join(snapshot_ok['reasons']) or 'as recorded'})")
    print(f"  lease_verified true, permission_policy_verified true: local mode, decided by the user")
    print(f"  approval {approval_id} valid {iso(now)} to {iso(expires)} "
          f"({APPROVAL_WINDOW_SECONDS}s; R14 fails past {iso(expires)}, so re-plan rather than submit)")
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

    if args.smoke:
        result = smoke_plan(config, snapshot, targets, quotes, borrow, evidence, args, now, account, mode,
                            pending, executed, seen, blockers, paths)
    else:
        result = entry_plan(config, snapshot, targets, quotes, borrow, evidence, args, now, account, mode,
                            pending, executed, seen, blockers, paths)
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
                   f"{len(rejected)} rejected; {result['summary']['note']}",
                   "PLAN" if result["submit"] else "PLAN_NO_SUBMIT",
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
                            "limit_price": order["limit_price"], "intent": order["intent"],
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
    if not smoke:                       # ETF tickets first, then the shorts, as the runbook submits them
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
    for line in summary["slack_report"][:10]:
        print(f"    {line}")
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
        ticket["cancel"] = {"tool": "sim_trade_cancel_order",
                            "payload": {"acc_id": str(plan["account_id"]), "market": MARKET_ID,
                                        "order_id": ticket["order_id"]}}
        if row["state"] == "UNKNOWN":
            blockers.append({"symbol": ticket["symbol"],
                             "reason": f"ticket {ticket['n']} status code {row['status_code']} is "
                                        f"unmapped: pass --working <code> for a code you have proved means "
                                        f"a live order, or cancel it by hand"})
            continue
        if rounds >= MAX_REPRICE_ROUNDS:
            ticket["status"] = "skipped_unfilled"
            ticket["note"] = f"cancel only: {rounds} reprice rounds done, {remaining} share(s) left"
            changes.append(f"ticket {ticket['n']}: cancel {ticket['order_id']}, marked skipped_unfilled")
            continue
        quote = quotes.get(ticket["symbol"]) or {}
        price, reason = marketable_limit(quote, ticket["side"])
        if price is None:
            blockers.append({"symbol": ticket["symbol"],
                             "reason": f"ticket {ticket['n']} cannot be repriced: {reason}"})
            continue
        next_index += 1
        child = dict(ticket["order"])
        child["quantity"] = remaining
        child["limit_price"] = price
        child["arrival_price"] = quote.get("price")
        child["client_order_id"] = f"{ticket['client_order_id'][:88]}-r{rounds + 1}"
        child["price_basis"] = (f"reprice round {rounds + 1} off {quote.get('bid')}/{quote.get('ask')} "
                                f"bid/ask: {ticket['side'].lower()} {-PRICE_OFFSET:+.2%}, cent through "
                                f"the touch")
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
        changes.append(f"ticket {ticket['n']}: cancel {ticket['order_id']}, reissue {remaining} share(s) "
                       f"of {ticket['symbol']} at {price} as ticket {next_index} (round {rounds + 1})")
    plan["repriced_at"] = iso(now)
    plan["blockers"] = blockers
    write_json(plan_path, plan)
    journal_append(args.journal, "GOLIVE_REPRICE", str(plan["account_id"]), plan["mode"], now,
                   "; ".join(changes) or "no ticket was due for repricing",
                   "REPRICE" if changes else "REPRICE_NONE",
                   target_checksum=(plan.get("targets") or {}).get("checksum"), run_id=plan.get("run_id"))
    print(f"REPRICE {plan_path.name}: {len(changes)} change(s)")
    for line in changes:
        print(f"  {line}")
    for row in blockers:
        print(f"  blocker: {row['symbol']}: {row['reason']}")
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
    band = config["trade_band_of_equity"] * equity
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
                                       f"above the {config['trade_band_of_equity']:.2%}-of-equity band"})
    state = measure(config, snapshot["positions"], equity, snapshot["initial_equity"],
                    snapshot["peak_equity"])
    executed, _ = executed_from_history(history, now)
    budget = config["competition_order_cap"] - config["emergency_flatten_reserve"]
    result = {"reconciled_at": iso(now), "mode": mode, "account_id": account, "plan": str(plan_path),
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
              "complete_reconciliation": not missing and not unresolved and not open_orders}
    out_dir = plan_path.parent
    write_json(out_dir / "reconcile.json", result)
    write_json(out_dir / "summary.md", summary_text(result, fills, plan))
    written = append_fills(args.journal, result, fills, plan, now)
    journal_append(args.journal, "GOLIVE_RECONCILE", account, mode, now,
                   f"{result['filled_complete']} of {result['orders_at_broker']} orders fully filled, "
                   f"{len(missing)} missing, {len(open_orders)} still live at the broker, "
                   f"{len(deviations)} deviations above the band, margin "
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
    print(f"  {len(deviations)} deviation(s) above the {config['trade_band_of_equity']:.2%}-of-equity "
          f"band; {len(executed)} of {budget} executed orders used today "
          f"({config['emergency_flatten_reserve']} reserved for an emergency flatten of "
          f"{config['competition_order_cap']})")
    return 0 if result["complete_reconciliation"] else 2


def summary_text(result: dict, fills: list, plan: dict) -> str:
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
             f"- deviations above the band: {len(result['deviations'])}", "",
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
        lines += ["", "## deviations above the band"]
        for row in result["deviations"]:
            lines.append(f"- {row['symbol']}: target {row['target_shares']}, held {row['held_shares']} "
                         f"({row['share_difference']:+d} share(s), {show(row['dollars'])} USD) - "
                         f"{row['note']}")
    lines += ["", f"Unverified fields, kept null rather than zero: "
                  f"{', '.join(result['unverified_fields']) or 'none'}.",
              f"Complete reconciliation: {result['complete_reconciliation']}, because fill identity is "
              f"{result['fill_identity']} and every mapped status code is evidenced.", ""]
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


# ------------------------------------------------------------------ cli
def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m live.golive",
                                   description="Go-live tooling: parse, size, gate, record, reprice, "
                                               "reconcile. It never places an order.")
    root.add_argument("--journal", type=Path, default=ROOT / "journal")
    root.add_argument("--config", type=Path, help="a P* configuration file; the deployed one by default")
    root.add_argument("--as-of", type=timestamp, help="the clock for this run; defaults to now (UTC)")
    sub = root.add_subparsers(dest="command", required=True)

    arm = sub.add_parser("arm", help="arm the current mode's account on the user's exact typed reply")
    arm.add_argument("--account", required=True)
    arm.add_argument("--reply", required=True, help="exactly the text ARM <account> the user typed")
    arm.set_defaults(handler=command_arm)

    halt = sub.add_parser("halt", help="latch the halt again; it cancels and sends nothing")
    halt.add_argument("--account", required=True)
    halt.add_argument("--reason", required=True)
    halt.set_defaults(handler=command_halt)

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
    plan.add_argument("--out", type=Path)
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
