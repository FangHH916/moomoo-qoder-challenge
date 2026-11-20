"""Shared P* fixture: a real target book, a real order plan and a complete gate context.

Everything comes from the committed research run files in `reports/live/` through the production
conversion path, so a gate test that passes here also proves the run file, the contract, the planner and
the rules agree with each other. Prices are synthetic but deterministic: each name is priced so that its
target is exactly `lots` whole shares, which keeps caps, margin and the trade band readable straight off
the fixture. The session and clock come from the newest run file, so a gate test exercises the book the
live path would actually publish today; `rebalance_due` stays True because most gate tests need the
go-live rebalance book even when the newest run is a drift session.

Tests only. Nothing here places an order, and `execution_enabled` is false in the shipped configuration.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path

from live.contracts import digest, load_strategy_config, seal, timestamp
from live.eligibility import plan_eligibility
from live.order_planner import plan
from live.pstar import classify, make_targets
from live.risk_monitor import actions, measure
from rules.gate import evaluate_order, order_fingerprint

# Each variant trades the newest run file of its own prefix, so the fixture session and clock come from
# those files rather than from a hard-coded date: a gate test then exercises the book the live path would
# publish today, and reports/live/ gaining a session cannot silently freeze the fixtures on an old one.
RUN_DIR = Path(__file__).resolve().parents[1] / "reports/live"
RUN_PREFIX = {"pstar": "r10", "pstar15": "r14"}


def committed_session(variant: str) -> str:
    """The session of the newest committed run file for that variant."""
    files = sorted(RUN_DIR.glob(f"{RUN_PREFIX[variant]}_targets_*.json"))
    if not files:
        raise FileNotFoundError(f"no {RUN_PREFIX[variant]}_targets_*.json run file in {RUN_DIR}")
    return files[-1].name.split("_targets_")[1][:10]


def tradeable_clock(session: str) -> str:
    """09:35 ET on the day after a research run, i.e. the first session that run is tradeable in."""
    return (datetime.strptime(session, "%Y-%m-%d") + timedelta(days=1)).replace(
        hour=14, minute=35, microsecond=0).isoformat() + "+00:00"


SESSION = committed_session("pstar15")
NOW = tradeable_clock(SESSION)
ACCOUNTS = {"test": "5322438", "competition": "5832812"}
EQUITY = 100_000.0


def config_for(variant: str | None = None) -> dict:
    """The deployed configuration with one frozen variant selected, which is the only line a variant
    switch needs (`variant` is the switch; the variant block itself is validated against the run)."""
    config = load_strategy_config()
    return config if variant is None or variant == config["variant"] else dict(config, variant=variant)


def owned(symbols, strategy_id: str, action: str = "ALLOW") -> list[dict]:
    """Journal decisions saying that this strategy version put those names on the book."""
    return [{"symbol": name, "strategy_id": strategy_id, "action": action} for name in symbols]


def prices_for(weights: dict, equity: float = EQUITY, lots: int = 1) -> dict[str, float]:
    """Price each name so that its target is exactly `lots` whole shares."""
    return {name: round(abs(weight) * equity / lots, 2) or 0.01 for name, weight in weights.items()}


def broker_records(config: dict, weights: dict, prices: dict, now: str, blocked=()) -> dict:
    """Relay-verified broker rows: the sleeve ETFs as ETFs, everything else as shortable common stock."""
    etfs = set(config["etf_universe"])
    records = {}
    for name in weights:
        stock = name not in etfs
        records[name] = {"verified_by_relay": True, "market": "US",
                         "security_type": "COMMON_STOCK" if stock else "ETF",
                         "halted": False, "as_of": now, "price": prices[name],
                         "market_cap": 5_000_000_000.0, "market_value": 5_000_000_000.0,
                         "shortable": name not in blocked, "easy_to_borrow": name not in blocked}
    return records


def approve(order: dict, context: dict, config: dict) -> None:
    """Bind an independent auditor approval and a one-use human approval to this exact plan."""
    expires = (timestamp(context["now"]) + timedelta(seconds=300)).isoformat()
    context["auditor_approval"] = {"approval_id": "audit-pstar", "verified_by_relay": True,
                                   "decision": "APPROVE", "issued_at": context["now"], "expires_at": expires,
                                   "session_id": "independent-auditor", "mode": context["mode"],
                                   "account_id": str(context["account_id"]),
                                   "target_checksum": context["targets"]["checksum"],
                                   "snapshot_checksum": digest(context["snapshot"]),
                                   "plan_checksum": context["plan_checksum"],
                                   "config_checksum": digest(config)}
    context["human_approvals"] = [{"approval_id": "human-pstar", "verified_by_relay": True,
                                   "decision": "APPROVE", "issued_at": context["now"], "expires_at": expires,
                                   "account_id": str(context["account_id"]), "mode": context["mode"],
                                   "order_fingerprint": order_fingerprint(order, context), "consumed": False}]


class Harness:
    """One account state, the plan it produces, and the gate context that evaluates a plan line."""

    def __init__(self, positions=(), journal=(), blocked=(), equity=EQUITY, initial=EQUITY,
                 peak=None, previous_close=None, now=None, mode="test", variant=None, config=None,
                 rebalance_due=True, lots: int = 1):
        self.config = config or config_for(variant)
        self.now = now or tradeable_clock(committed_session(self.config["variant"]))
        self.lots = lots
        account = ACCOUNTS[mode]
        self.snapshot = {"mode": mode, "account_id": account, "account_type": "SIMULATE",
                         "equity": equity, "initial_equity": initial,
                         "previous_close_equity": equity if previous_close is None else previous_close,
                         "peak_equity": max(equity, initial) if peak is None else peak,
                         "as_of": self.now, "positions": [dict(row) for row in positions]}
        self.targets = make_targets(self.config, self.snapshot, timestamp(self.now), no_run=True)
        if rebalance_due is not None:
            self.targets = self.reseal(rebalance_due=rebalance_due)
        self.journal = list(journal)
        self.prices = prices_for(self.targets["weights"], equity, lots)
        self.quotes = {name: {"verified_by_relay": True, "price": price, "as_of": self.now}
                       for name, price in self.prices.items()}
        self.eligibility = broker_records(self.config, self.targets["weights"], self.prices, self.now,
                                          blocked)
        self.context = {"now": self.now, "mode": mode, "account_id": account, "snapshot": self.snapshot,
                        "targets": self.targets, "expected_target_checksum": self.targets["checksum"],
                        "guard_session_id": "guard-pstar", "run_id": "fixture-only",
                        "quotes": self.quotes, "eligibility": self.eligibility,
                        "open_orders": [], "executed_order_ids": [], "seen_client_order_ids": [],
                        "reconciled": True, "snapshot_verified_by_relay": True, "release_verified": True,
                        "lease_verified": True, "permission_policy_verified": True,
                        "daily_entry_halt": False, "scout": {},
                        "kill_switch": {"halted": False, "verified_by_relay": True}}
        self.rebuild_plan()

    # ------------------------------------------------------------------ rebuilding
    def reseal(self, **updates) -> dict:
        body = {k: v for k, v in self.targets.items() if k != "checksum"}
        return seal({**body, **updates})

    def protected(self) -> set[str]:
        attribution = classify(self.snapshot, self.config, self.targets["strategy_id"], self.journal)
        return set(attribution["discretionary"]) | set(attribution["unclassified"])

    def rebuild_plan(self) -> dict:
        """Re-run eligibility, the risk monitor and the planner against the current fixture state."""
        self.eligibility_result = plan_eligibility(self.config, self.targets, self.eligibility,
                                                   self.quotes, datetime.fromisoformat(self.now))
        self.state = measure(self.config, self.snapshot["positions"], self.snapshot["equity"],
                             self.snapshot["initial_equity"], self.snapshot["peak_equity"])
        self.risk = actions(self.config, self.state, sorted(self.protected()))
        self.plan = plan(self.config, self.targets, self.snapshot, self.quotes,
                         journal_records=self.journal, eligibility=self.eligibility_result, risk=self.risk)
        self.context["targets"] = self.targets
        self.context["expected_target_checksum"] = self.targets["checksum"]
        return self.plan

    def set_targets(self, **updates) -> dict:
        self.targets = self.reseal(**updates)
        self.context["targets"] = self.targets
        self.context["expected_target_checksum"] = self.targets["checksum"]
        return self.targets

    # ------------------------------------------------------------------ evaluating
    def order(self, index: int = 0, symbol: str | None = None) -> dict:
        if symbol is not None:
            for row in self.plan["orders"]:
                if row["symbol"] == symbol:
                    return copy.deepcopy(row)
            raise KeyError(f"{symbol} is not in the plan; skipped: "
                           f"{[row['symbol'] for row in self.plan['skipped']]}")
        return copy.deepcopy(self.plan["orders"][index])

    def rebind(self, order: dict | None = None, orders=None) -> dict:
        """Refresh the plan and both approvals so they bind to whatever the fixture state is now."""
        order = copy.deepcopy(self.plan["orders"][0] if order is None else order)
        self.context["approved_plan_orders"] = copy.deepcopy(
            self.plan["orders"] if orders is None else orders)
        self.context["plan_checksum"] = digest(self.context["approved_plan_orders"])
        approve(order, self.context, self.config)
        return order

    def result(self, index: int = 0, symbol: str | None = None, config: dict | None = None) -> dict:
        order = self.rebind(self.order(index, symbol))
        return evaluate_order(order, self.context, config or self.config)

    def check_order(self, order: dict, config: dict | None = None) -> dict:
        """Evaluate a hand-written order as the only line of an otherwise consistent plan."""
        self.rebind(order, orders=[order])
        return evaluate_order(copy.deepcopy(order), self.context, config or self.config)

    def plan_symbols(self) -> set[str]:
        return {row["symbol"] for row in self.plan["orders"]}

    def target_shares(self, symbol: str) -> int:
        """The whole-share magnitude the planner assigned to that name."""
        return abs(self.plan["target_shares"].get(symbol, 0))

    def entry_order(self, symbol: str, quantity: int | None = None, intent: str = "ENTRY") -> dict:
        """A plan-shaped order opening a target name, for rules the planner itself never reaches."""
        from live.order_planner import client_order_id
        shares = abs(self.plan["target_shares"].get(symbol, 0)) if quantity is None else abs(quantity)
        side = "BUY" if self.targets["weights"][symbol] > 0 else "SELL"
        order = {"account_id": self.context["account_id"], "symbol": symbol, "side": side,
                 "quantity": shares, "limit_price": self.prices[symbol], "order_type": "LIMIT",
                 "time_in_force": "DAY", "session": "RTH", "intent": intent,
                 "sleeve": self.targets["sleeves"].get(symbol), "weight": self.targets["weights"][symbol],
                 "reason": "fixture order", "workflow": "paper-trading-gate",
                 "target_checksum": self.targets["checksum"]}
        order["client_order_id"] = client_order_id(self.targets, symbol, side, shares)
        return order

    def position(self, symbol: str, quantity: int, price: float | None = None) -> dict:
        return {"symbol": symbol, "quantity": quantity,
                "price": self.prices.get(symbol, 100.0) if price is None else price}

    def place(self, holdings, rebalance_due: bool | None = None, unowned=()) -> "Harness":
        """Put the account in these positions and re-plan from them.

        Names in `unowned` have no journaled strategy order, so the strategy must treat them as
        discretionary or unclassified and never trade them.
        """
        self.snapshot["positions"] = [dict(self.position(name, quantity)) for name, quantity in holdings]
        owned_names = [name for name, _ in holdings if name not in set(unowned)]
        self.journal = owned(owned_names, self.targets["strategy_id"])
        if rebalance_due is not None:
            self.set_targets(rebalance_due=rebalance_due)
        self.rebuild_plan()
        return self
