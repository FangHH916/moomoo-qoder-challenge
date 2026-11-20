from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from live.contracts import (ACTIVE_STRATEGY, digest, finite, frozen_factor_names, hedge_keys, load_config,
                            load_strategy_config, timestamp, validate_targets)
from rules.trading_calendar import ET, is_session, session_bounds, target_age, trading_window

ACCOUNTS = {"test": "5322438", "competition": "5832812"}
ACTIVE_STATES = {"SUBMITTED", "OPEN", "PARTIAL", "CANCEL_PENDING", "UNKNOWN"}


def order_fingerprint(order: dict, context: dict) -> str:
    return digest({"order": order, "mode": context["mode"], "account_id": str(context["account_id"]),
                   "target_checksum": context["targets"]["checksum"],
                   "snapshot_checksum": digest(context["snapshot"]), "plan_checksum": context["plan_checksum"]})


def _recent(value: str, now, limit: int) -> bool:
    age = (now - timestamp(value)).total_seconds()
    return 0 <= age <= limit


def _approval(record: dict, now, ttl: int, expected: dict) -> bool:
    return (record.get("verified_by_relay") is True and record.get("decision") == "APPROVE"
            and all(record.get(k) == v for k, v in expected.items())
            and timestamp(record["issued_at"]) <= now < timestamp(record["expires_at"])
            and timestamp(record["expires_at"]) - timestamp(record["issued_at"]) <= timedelta(seconds=ttl))


def evaluate_order(order: dict, context: dict, config: dict | None = None) -> dict:
    """Evaluate one order; without a configuration the deployed strategy's rules apply."""
    config = config or load_strategy_config()
    if config.get("strategy") == "pstar":
        # gate_pstar imports the primitives below, so the dependency is resolved at call time.
        from rules.gate_pstar import evaluate_pstar_order
        return evaluate_pstar_order(order, context, config)
    checks = []

    def check(rule, passed, threshold, observed, action="BLOCK"):
        checks.append({"id": rule, "passed": bool(passed), "threshold": threshold,
                       "observed": observed, "action": "PASS" if passed else action})

    try:
        now = timestamp(context["now"])
        mode, account = context["mode"], str(context["account_id"])
        snap, targets = context["snapshot"], context["targets"]
        equity, initial = snap["equity"], snap["initial_equity"]
        if not all(finite(x) and x > 0 for x in [equity, initial, snap["previous_close_equity"], snap["peak_equity"]]):
            raise ValueError("Positive finite equity required")
        if not isinstance(order["quantity"], int) or isinstance(order["quantity"], bool) or order["quantity"] <= 0:
            raise ValueError("Positive whole-share quantity required")
        if order["side"] not in ("BUY", "SELL") or not finite(order["limit_price"]) or order["limit_price"] <= 0:
            raise ValueError("Invalid side or price")
        if order["intent"] not in ("REBALANCE", "ENTRY", "EXIT", "EMERGENCY_EXIT", "FINAL_EXIT"):
            raise ValueError("Invalid order intent")
        if not isinstance(order.get("client_order_id"), str) or not order["client_order_id"]:
            raise ValueError("Client order identity required")
        symbol = order["symbol"]
        positions = {}
        prices, betas = {}, {}
        for row in snap["positions"]:
            s = row["symbol"]
            if s in positions or not finite(row["quantity"]) or not finite(row["price"]) or row["price"] <= 0:
                raise ValueError("Invalid or duplicate position")
            positions[s], prices[s] = row["quantity"], row["price"]
            betas[s] = row.get("beta")
        quote = context.get("quotes", {}).get(symbol, {})
        quote_ok = (quote.get("verified_by_relay") is True and finite(quote.get("price")) and quote["price"] > 0
                    and _recent(quote["as_of"], now, config["quote_max_age_seconds"]))
        prices[symbol] = quote.get("price") if quote_ok else order["limit_price"]
        betas[symbol] = 1.0 if symbol == "SPY" else quote.get("beta", betas.get(symbol))
        delta = order["quantity"] * (1 if order["side"] == "BUY" else -1)
        old, new = positions.get(symbol, 0), positions.get(symbol, 0) + delta
        reducing = old != 0 and old * delta < 0 and abs(delta) <= abs(old)
        reversal = old * new < 0
        emergency = order["intent"] in ("EMERGENCY_EXIT", "FINAL_EXIT") and reducing
        day = now.astimezone(ET).date().isoformat()
        daily_loss = equity / snap["previous_close_equity"] - 1
        drawdown = equity / snap["peak_equity"] - 1
        final_exit = now >= timestamp(config["final_exit_start"])
        halted = context.get("kill_switch", {}).get("halted") is True or drawdown <= config["drawdown_limit"] + 1e-12
        pending = [o for o in context["open_orders"] if o["status"] in ACTIVE_STATES]
        pending_ids = [o["order_id"] for o in pending]
        if len(set(pending_ids)) != len(pending_ids):
            raise ValueError("Duplicate broker order identity")
        longs = {s: max(q, 0) * prices[s] for s, q in positions.items()}
        shorts = {s: max(-q, 0) * prices[s] for s, q in positions.items()}
        pending_increase = set()
        for o in pending:
            s = o["symbol"]
            if not isinstance(o["remaining_quantity"], int) or o["remaining_quantity"] < 0 or o["side"] not in ("BUY", "SELL"):
                raise ValueError("Invalid pending order")
            if not finite(o["limit_price"]) or o["limit_price"] <= 0:
                raise ValueError("Invalid pending price")
            px = max(o["limit_price"], prices.get(s, o["limit_price"]))
            # Reserve the unfilled side without assuming offsetting orders will fill.
            if not o.get("reduce_only_verified", False):
                book = longs if o["side"] == "BUY" else shorts
                book[s] = book.get(s, 0) + o["remaining_quantity"] * px
                pending_increase.add(s)
        before_long, before_short = sum(longs.values()), sum(shorts.values())
        projected = dict(positions)
        projected[symbol] = new
        if reducing:
            book = longs if old > 0 else shorts
            book[symbol] = max(0.0, book.get(symbol, 0) - abs(delta) * prices[symbol])
        else:
            book = longs if delta > 0 else shorts
            book[symbol] = book.get(symbol, 0) + abs(delta) * max(order["limit_price"], prices[symbol])
        long_gross, short_gross = sum(longs.values()) / equity, sum(shorts.values()) / equity
        names = {s for s, q in projected.items() if q != 0} | {o["symbol"] for o in pending}
        executed = set(context["executed_order_ids"])
        pending_unexecuted = {o["order_id"] for o in pending if o["order_id"] not in executed}
        used_plus_pending = len(executed) + len(pending_unexecuted) + 1
        reserved_exits = len(names) if not reducing else 0
        budget = used_plus_pending + reserved_exits
        account_ok = (mode in ACCOUNTS and account == ACCOUNTS.get(mode) and mode == config["mode"]
                      and str(snap["account_id"]) == account and snap["mode"] == mode
                      and str(order["account_id"]) == account and snap["account_type"] == "SIMULATE")
        check("R1", account_ok, ACCOUNTS.get(mode, "invalid mode"), account, "BLOCK_ALERT")
        eligible = context.get("eligibility", {}).get(symbol, {})
        hedges = set(config["hedge_instruments"])
        instrument_ok = ((symbol in hedges and eligible.get("security_type") == "ETF"
                          and order.get("purpose") == "BETA_HEDGE")
                         or (symbol not in hedges and eligible.get("security_type") == "COMMON_STOCK"))
        eligibility_ok = (instrument_ok and eligible.get("market") == "US" and eligible.get("verified_by_relay") is True)
        if not reducing:
            eligibility_ok = (eligibility_ok and quote_ok and quote["price"] >= config["min_price"]
                              and finite(eligible.get("market_cap")) and eligible["market_cap"] >= config["min_market_cap"]
                              and finite(eligible.get("median_dollar_volume_20d"))
                              and eligible["median_dollar_volume_20d"] >= config["min_dollar_volume_20d"]
                              and eligible.get("halted") is False and eligible.get("earnings_clear_2_sessions") is True
                              and _recent(eligible["as_of"], now, config["quote_max_age_seconds"]))
            if new < 0:
                eligibility_ok = (eligibility_ok and eligible.get("shortable") is True
                                  and eligible.get("easy_to_borrow") is True
                                  and eligible["market_cap"] >= config["min_short_market_cap"])
            if symbol in hedges and order.get("purpose") != "BETA_HEDGE":
                eligibility_ok = False
        check("R2", eligibility_ok,
              "US common stock or SPY/QQQ BETA_HEDGE; $1/$500M/$5M; shorts $1B + verified borrow", symbol, "HOLD")
        caps = {s: (config["hedge_cap_of_initial_equity"] * initial if s in hedges else
                    min(config["max_name_weight"] * equity,
                        config["competition_name_initial_equity_cap"] * initial))
                for s in set(longs) | set(shorts)}
        per_name = all(max(longs.get(s, 0), shorts.get(s, 0)) <= caps[s] + 1e-7 for s in caps)
        check("R3", per_name or reducing,
              "stocks 10% equity AND 40% initial; hedge ETFs 38% initial; reduce-only relief",
              max((max(longs.get(symbol, 0), shorts.get(symbol, 0)) / equity), 0.0))
        mult = targets["vol_multiplier"]
        if not finite(mult) or not 0 < mult <= 1:
            raise ValueError("Invalid volatility multiplier")
        hedge_capacity = len(hedges) * config["hedge_cap_of_initial_equity"] * initial / equity
        gross_ok = (short_gross <= config["gross_short"] * mult + 1e-9
                    and long_gross <= hedge_capacity + 1e-9)
        check("R4", gross_ok or reducing,
              "Short <=35% * multiplier; longs only inside 38%-of-initial hedge ETF caps; pending counted",
              {"long": long_gross, "short": short_gross})
        beta_known = all(finite(betas.get(s)) for s, q in projected.items() if q != 0)
        book_beta = sum(q * prices[s] / equity * betas[s] for s, q in projected.items() if q != 0) if beta_known else None
        beta_risk = abs(book_beta) if book_beta is not None else None
        for o in pending:
            if not o.get("reduce_only_verified", False):
                b = context.get("quotes", {}).get(o["symbol"], {}).get("beta")
                if not finite(b):
                    beta_known = False
                    break
                if beta_risk is not None:
                    beta_risk += abs(o["remaining_quantity"] * o["limit_price"] / equity * b)
        check("R5", emergency or (beta_known and beta_risk is not None and beta_risk <= config["max_abs_beta"] + 1e-9),
              "Absolute beta <=0.10; emergency reduce-only liquidation may pass transiently", beta_risk, "HOLD")
        check("R6", len(names) <= config["max_positions"] and budget <= config["max_executed_orders"]
              and used_plus_pending <= config["competition_order_cap"],
              "100 names; 60 executed+pending+exit reserve; competition 200",
              {"names": len(names), "orders_with_reserve": budget})
        check("R7", order["order_type"] == "LIMIT" and order.get("time_in_force") == "DAY"
              and order.get("session") == "RTH" and trading_window(now, emergency)
              and quote_ok and abs(order["limit_price"] / quote["price"] - 1) * 10000 <= config["limit_band_bps"] + 1e-8,
              "RTH limit DAY, 09:35-15:45; 25bps; emergency regular-hours exits", order["limit_price"], "HOLD")
        check("R8", (daily_loss > config["daily_loss_limit"] + 1e-12 and context.get("daily_entry_halt") is False) or reducing,
              "At -2% day P&L: no new exposure until next ET session", daily_loss, "HALT_ENTRIES")
        check("R9", not halted or emergency, "At -8% peak drawdown: flatten and latch halt", drawdown, "FLATTEN_HALT")
        try:
            validate_targets(targets, config)
            fresh = target_age(targets["signal_date"], now) <= config["max_target_age_sessions"]
            data_time = timestamp(targets["data_timestamp"])
            fresh = fresh and data_time <= timestamp(targets["generated_at"]) <= now
            fresh = fresh and data_time.astimezone(ET).date().isoformat() == targets["signal_date"]
            fresh = fresh and data_time >= session_bounds(targets["signal_date"])[1]
            fresh = fresh and targets["checksum"] == context["expected_target_checksum"]
            fresh = fresh and targets["config_checksum"] == digest(config) and targets["mode"] == mode
        except (ValueError, KeyError, TypeError):
            fresh = False
        check("R10", fresh or emergency, "Verified checksum/config; <=2 completed sessions old; independent emergency path", targets.get("signal_date"), "HOLD")
        check("R11", quote_ok and context.get("snapshot_verified_by_relay") is True
              and _recent(snap["as_of"], now, config["account_max_age_seconds"]), "Quote/account <=60s, not future", quote.get("as_of"), "HOLD")
        stale_pending = any(o["status"] in ("UNKNOWN", "CANCEL_PENDING") or
                            not _recent(o["submitted_at"], now, config["order_ttl_seconds"]) for o in pending)
        same_pending = any(o["symbol"] == symbol for o in pending)
        duplicate = order["client_order_id"] in context["seen_client_order_ids"]
        check("R12", not stale_pending and not same_pending and not duplicate and not reversal
              and context.get("reconciled") is True, "Reconcile first; no unknown/stale/duplicate/crossing orders", {"stale": stale_pending, "same_symbol": same_pending, "duplicate": duplicate}, "RECONCILE_HOLD")
        calendar_ok = is_session(day) and (day != "2026-11-27" or emergency)
        if mode == "competition":
            calendar_ok = calendar_ok and timestamp(config["competition_start"]) <= now <= timestamp(config["competition_stop"])
        check("R13", calendar_ok and (not final_exit or emergency), "Nov26 closed; Nov27 exit-only 12:15->12:45 ET; bounded window", day, "HOLD")
        fp = order_fingerprint(order, context)
        audit_expected = {"target_checksum": targets["checksum"], "snapshot_checksum": digest(snap),
                          "plan_checksum": context["plan_checksum"], "account_id": account, "mode": mode,
                          "config_checksum": digest(config)}
        audit = context.get("auditor_approval", {})
        audit_ok = _approval(audit, now, config["approval_ttl_seconds"], audit_expected)
        audit_ok = audit_ok and audit.get("session_id") != context.get("guard_session_id") and bool(audit.get("session_id"))
        check("R14", audit_ok, "Independent auditor approval bound to plan/targets/snapshot/account/config; <=5min", audit.get("approval_id"), "HOLD")
        human_ok = mode == "competition" or any(_approval(a, now, config["approval_ttl_seconds"],
                       {"order_fingerprint": fp, "account_id": account, "mode": mode}) and a.get("consumed") is False
                       for a in context.get("human_approvals", []))
        check("R15", human_ok, "Test: exact one-use human approval <=5min; competition: rules cannot be overridden", mode, "AWAIT_APPROVAL")
        switch = context.get("kill_switch", {})
        check("R16", switch.get("verified_by_relay") is True and switch.get("halted") is False or
              (switch.get("verified_by_relay") is True and emergency), "Manual halt permits only independently approved liquidation", switch.get("halted"), "HALT")
        check("R17", context.get("release_verified") is True and context.get("lease_verified") is True
              and order.get("workflow") == "paper-trading-gate"
              and context.get("permission_policy_verified") is True,
              "Frozen release, account-wide lease, only paper workflow, enforced always_ask", context.get("release_verified"), "HALT")
        proposed = context["approved_plan_orders"]
        plan_ok = digest(proposed) == context["plan_checksum"] and order in proposed
        fields = hedge_keys(config)
        allowed_target = (targets.get(fields[symbol], 0.0) if symbol in fields
                          else targets["weights"].get(symbol, 0.0))
        destination = allowed_target * equity / prices[symbol]
        toward_target = (abs(new - destination) <= abs(old - destination) + 1e-9
                         and (new == 0 or destination * new >= 0))
        scout = context.get("scout", {}).get(symbol)
        check("R18", plan_ok and (emergency or (targets["status"] == "READY" and toward_target
              and (scout == "PASS" or reducing))), "Exact plan; do not invent target or increase a vetoed name", scout, "HOLD")
        long_stocks = sorted(s for s, q in projected.items() if q > 0 and s not in hedges)
        check("R19", emergency or not long_stocks,
              "Short-only book: only SPY/QQQ hedge ETFs may be long", long_stocks, "BLOCK_ALERT")
        margin_used = (config["margin_long_factor"] * long_gross + config["margin_short_factor"] * short_gross)
        check("R20", emergency or margin_used <= config["margin_cap"] + 1e-9,
              "0.5*long gross + 0.6*short gross <= 90% equity, pending orders included", margin_used, "HOLD")
        coefficients = targets.get("model_coefficients", {})
        provenance = (targets["status"] != "READY" or
                      (set(coefficients) == set(frozen_factor_names(config))
                       and any(v != 0 for v in coefficients.values())
                       and type(targets.get("model_carried_forward")) is bool
                       and targets.get("signal_guard", {}).get("passed") is True
                       and len(targets["weights"]) <= config["n_shorts"]))
        check("R21", emergency or provenance,
              "READY needs the frozen non-zero 56-factor vector (carried forward is allowed), passed signal guard and <=20 shorts",
              {"nonzero_factors": sum(1 for v in coefficients.values() if v != 0),
               "carried_forward": targets.get("model_carried_forward"),
               "guard": targets.get("signal_guard", {}).get("passed")}, "HOLD")
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError) as exc:
        check("R0", False, "Complete, finite, trusted input contract", type(exc).__name__, "HOLD")
        fp = None
    failed = [c for c in checks if not c["passed"]]
    return {"eligible": not failed, "execution_allowed": not failed and config.get("execution_enabled") is True,
            "action": "ALLOW" if not failed else failed[0]["action"],
            "failed_rules": [c["id"] for c in failed], "checks": checks, "order_fingerprint": fp,
            "execution_note": "Pure validation only; this module has no broker or cloud client."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--strategy", choices=("pstar", "legacy-a"), default=None,
                        help="defaults to the deployed strategy; --config wins when given")
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    config = (load_config(args.config) if args.config
              else load_strategy_config(args.strategy or ACTIVE_STRATEGY))
    result = evaluate_order(data["order"], data["context"], config)
    print(json.dumps(result, allow_nan=False, indent=2))
    return 0 if result["eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
