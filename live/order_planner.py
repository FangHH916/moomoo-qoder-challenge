"""Order planner: the frozen execution rules turned into whole-share limit orders.

What the strategy trades comes from one verified targets payload (`live/pstar.py`) plus what the broker
actually holds, and the rules are the frozen ones in prompt 06 item 6:

* whole shares at the next open, rounded to the nearest share; a name that rounds to zero shares is
  skipped and its weight is **not** redistributed;
* on a rebalance session an existing position is re-sized only when it is off target by more than
  0.25% of equity, while entries and exits always trade;
* between rebalances nothing trades except the risk rules from `live/risk_monitor.py` (39% trim, 95%
  margin, -8% drawdown), which can only reduce exposure;
* the go-live day is one ENTRY batch;
* at most 100 executed strategy orders a day - the other 100 of the competition's 200 stay in reserve
  for an emergency flatten;
* a position the strategy did not open is never sold by it (`live/pstar.py::classify`).

Never places an order: the output is a plan that the rule gate and the auditor see before anything is sent.
"""
from __future__ import annotations

from live.contracts import digest, finite
from live.pstar import classify

ENTRY, EXIT, REBALANCE, EMERGENCY = "ENTRY", "EXIT", "REBALANCE", "EMERGENCY_EXIT"
NORMAL_INTENTS = (ENTRY, EXIT, REBALANCE)


class PlanError(ValueError):
    """The inputs cannot be turned into a safe plan; place nothing new."""


def _shares(value: object) -> int:
    if isinstance(value, bool) or not finite(value) or float(value) != int(value):
        raise PlanError(f"position quantity {value!r} is not a whole number of shares")
    return int(value)


def _name(symbol: object) -> str:
    if not isinstance(symbol, str):
        raise PlanError("position symbol must be a string")
    return symbol.removeprefix("US.")


def holdings(snapshot: dict) -> dict[str, int]:
    return {_name(row["symbol"]): _shares(row["quantity"]) for row in snapshot.get("positions") or ()}


def price_of(quotes: dict, name: str):
    """Only a relay-verified quote may price a limit order; a bare number is accepted for fixtures."""
    quote = (quotes or {}).get(name)
    if isinstance(quote, dict):
        return quote.get("price") if quote.get("verified_by_relay") is True else None
    return quote


def target_shares(weights: dict, equity: float, quotes: dict) -> tuple[dict[str, int], list[dict]]:
    """Whole shares per name; names without a usable price or rounding to zero shares are skipped."""
    want, skipped = {}, []
    for name, weight in sorted(weights.items()):
        if weight == 0:
            continue
        price = price_of(quotes, name)
        if not finite(price) or price <= 0:
            skipped.append({"symbol": name, "reason": "no relay-verified price", "weight": weight})
            continue
        shares = int(round(weight * equity / price))
        if shares == 0:
            skipped.append({"symbol": name, "reason": "rounds to zero shares, weight not redistributed",
                            "weight": weight})
            continue
        want[name] = shares
    return want, skipped


def client_order_id(targets: dict, name: str, side: str, quantity: int) -> str:
    """Deterministic per target book, so a retry of the same plan cannot become a second fill."""
    token = digest({"strategy_id": targets.get("strategy_id"), "session": targets.get("signal_date"),
                    "checksum": targets.get("checksum"), "symbol": name, "side": side,
                    "quantity": quantity})[:12]
    return f"pstar-{targets.get('signal_date')}-{name}-{side}-{quantity}-{token}"


def order_budget(config: dict) -> int:
    """Executed strategy orders allowed today: the competition cap minus the emergency flatten reserve."""
    cap, reserve = int(config["competition_order_cap"]), int(config["emergency_flatten_reserve"])
    if reserve < 0 or cap <= reserve:
        raise PlanError("the order reserve must leave a positive strategy order budget")
    return cap - reserve


def plan(config: dict, targets: dict, snapshot: dict, quotes: dict, *,
         journal_records=(), eligibility: dict | None = None, risk: dict | None = None,
         executed_today: int = 0) -> dict:
    """One order batch from the targets, the broker holdings, the eligibility screen and the risk monitor."""
    if config.get("strategy") != "pstar":
        raise PlanError("the P* planner needs the pstar configuration")
    equity = snapshot.get("equity")
    if not finite(equity) or equity <= 0:
        raise PlanError("snapshot equity must be a positive number")
    if not finite(executed_today) or executed_today < 0:
        raise PlanError("executed_today must be a non-negative count")
    current = holdings(snapshot)
    attribution = classify(snapshot, config, targets.get("strategy_id"), journal_records)
    protected = set(attribution["discretionary"]) | set(attribution["unclassified"])
    ready = targets.get("status") == "READY"
    weights = dict(targets.get("weights") or {}) if ready else {}
    want, skipped = target_shares(weights, equity, quotes)
    for name in sorted(protected & set(want)):
        want.pop(name)
        skipped.append({"symbol": name, "reason": "not opened by this strategy: never sold or re-sized",
                        "weight": weights.get(name, 0.0)})
    for name in sorted(set(want) & set((eligibility or {}).get("blocked") or ())):
        want.pop(name)
        skipped.append({"symbol": name,
                        "reason": "; ".join((eligibility.get("blocked_detail") or {}).get(name)
                                            or ["not tradeable today"]),
                        "weight": weights.get(name, 0.0)})

    rebalance = ready and bool(targets.get("rebalance_due"))
    band = config["trade_band_of_equity"] * equity
    final, reasons = {}, {}
    for name in sorted(set(current) | set(want)):
        held, target = current.get(name, 0), want.get(name, 0)
        if name in protected:
            final[name] = held
            reasons[name] = "protected position, left alone"
            continue
        if not ready:
            final[name] = held
            reasons[name] = "no verified target book"
            continue
        if not rebalance:
            final[name] = held
            reasons[name] = "drift session: only the risk rules may trade"
        elif held == 0:
            final[name] = target
            reasons[name] = "entry"
        elif target == 0:
            final[name] = 0
            reasons[name] = "exit"
        elif abs((target - held) * price_of(quotes, name)) > band:
            final[name] = target
            reasons[name] = "off target by more than the trade band"
        else:
            final[name] = held
            reasons[name] = "inside the trade band"

    # Risk actions only ever reduce exposure, and they apply on drift sessions too.
    halt = bool((risk or {}).get("halt"))
    if halt:
        final = {name: (held if name in protected else 0) for name, held in final.items()}
        reasons.update({name: "drawdown halt: flatten" for name in final if name not in protected})
    risk_changes = set((risk or {}).get("share_changes") or ())
    for name, shares in sorted(((risk or {}).get("target_shares") or {}).items()):
        held = current.get(name, 0)
        if name in protected or not finite(shares) or abs(shares) >= abs(held):
            continue
        if abs(shares) < abs(final.get(name, held)):
            final[name] = shares
            reasons[name] = "drawdown halt: flatten" if halt else "risk rule"

    orders = []
    for name in sorted(final):
        delta = final[name] - current.get(name, 0)
        if delta == 0:
            continue
        price = price_of(quotes, name)
        if not finite(price) or price <= 0:
            skipped.append({"symbol": name, "reason": "no quote to price the limit order", "delta": delta})
            continue
        side = "BUY" if delta > 0 else "SELL"
        orders.append({"account_id": str(snapshot.get("account_id")), "symbol": name, "side": side,
                       "quantity": abs(delta), "limit_price": price, "order_type": "LIMIT",
                       "time_in_force": "DAY", "session": "RTH",
                       "intent": _intent(current.get(name, 0), final[name], halt, name in risk_changes),
                       "sleeve": (targets.get("sleeves") or {}).get(name),
                       "weight": weights.get(name, 0.0), "reason": reasons.get(name, "risk rule"),
                       "workflow": "paper-trading-gate", "target_checksum": targets.get("checksum"),
                       "client_order_id": client_order_id(targets, name, side, abs(delta))})

    normal = [o for o in orders if o["intent"] in NORMAL_INTENTS]
    positions_after = len([shares for shares in final.values() if shares])
    status, note = "PLAN", None
    budget, available = order_budget(config), order_budget(config) - int(executed_today)
    if len(normal) > available:
        status = "HOLD"
        note = (f"{len(normal)} strategy orders exceed the {available} order{'' if available == 1 else 's'} "
                f"left of the {budget}-order daily budget ({config['competition_order_cap']} competition cap "
                f"minus a {config['emergency_flatten_reserve']}-order emergency reserve)")
        orders = [o for o in orders if o["intent"] == EMERGENCY]
        normal = []
    if positions_after > config["max_positions"]:
        status, note = "HOLD", (f"{positions_after} projected positions exceed the "
                                f"{config['max_positions']} cap")
        orders, normal = [], []
    entries = sum(1 for o in normal if o["intent"] == ENTRY)
    return {"status": status, "note": note,
            "strategy_id": targets.get("strategy_id"), "variant": config["variant"],
            "session": targets.get("signal_date"), "signal": targets.get("status"),
            "rebalance_due": rebalance, "equity": equity,
            "orders": orders, "intents": sorted({o["intent"] for o in orders}),
            "entry_batch": bool(normal) and entries == len(normal) and not any(current.values()),
            "counts": {"orders": len(orders), "entries": entries,
                       "exits": sum(1 for o in normal if o["intent"] == EXIT),
                       "rebalances": sum(1 for o in normal if o["intent"] == REBALANCE),
                       "emergency_exits": sum(1 for o in orders if o["intent"] == EMERGENCY),
                       "skipped": len(skipped), "positions_after": positions_after,
                       "order_budget": budget, "budget_left_today": available},
            "skipped": skipped, "target_shares": want, "final_shares": final,
            "protected": attribution, "risk": {key: (risk or {}).get(key) for key in
                                               ("halt", "flatten", "delever_scale", "share_changes",
                                                "margin_after", "slack_report")},
            "eligibility": {"blocked": sorted((eligibility or {}).get("blocked") or ()),
                            "skipped": list((eligibility or {}).get("skipped") or ())},
            "slack_report": [*((risk or {}).get("slack_report") or ()),
                             *((eligibility or {}).get("slack_report") or ())],
            "targets_checksum": targets.get("checksum"), "plan_checksum": digest(orders)}


def _intent(current: int, target: int, halt: bool, risk_driven: bool) -> str:
    if halt:
        return EMERGENCY
    if risk_driven:
        return EXIT                       # a trim or a margin cut is always a reduce-only exit
    if current == 0:
        return ENTRY
    return EXIT if target == 0 else REBALANCE
