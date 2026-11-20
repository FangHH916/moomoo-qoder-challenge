"""The deterministic rule gate for the deployed P* portfolio (prompt 06 item 5 and 6).

`evaluate_order` dispatches here whenever the configuration says ``strategy: pstar``; the plan-A rules
above it keep running unchanged for ``strategy: legacy-a``. Every check keeps its plan-A rule id, so an
auditor sees one familiar R1-R21 list, but the thresholds are the frozen P* ones:

===  =============================================  =====================================================
Id   frozen P* rule                                 why it is this rule and not another
===  =============================================  =====================================================
R2   instrument eligibility                         US listing, relay-verified, ETF vs common stock
R3   any ticker <= 38% of initial assets            the competition's $38,000 name cap
R4   composition of the book                        longs only the 10 sleeve ETFs + whitelist, shorts only
                                                    names in the current target book, <= 77 shorts
R5   borrow and size of a short                     shortable + easy-to-borrow + >= $1B; a skipped short is
                                                    never replaced
R6   100 positions, 100 strategy orders/day         the other 100 of the 200-order cap stay in reserve
R8   39% trim                                       a name above 39% of initial assets may only be cut to 38%
R19  reduce-only between rebalances                 outside a rebalance session nothing may add exposure
R20  0.5*long + 0.6*short <= 90% equity             margin cap, discretionary positions included
R21  frozen variant provenance                      the target book carries the configured factor set + K
===  =============================================  =====================================================

Pure validation: this module has no broker or cloud client and never places an order.
"""
from __future__ import annotations

from live.contracts import (PSTAR_STRATEGY, digest, finite, pstar_variant_config, timestamp,
                            validate_targets)
from rules.gate import ACCOUNTS, ACTIVE_STATES, _approval, _recent, order_fingerprint
from rules.trading_calendar import ET, is_session, session_bounds, target_age, trading_window

REDUCE_ONLY_RELIEF = "reduce-only or emergency order"


def evaluate_pstar_order(order: dict, context: dict, config: dict) -> dict:
    checks = []

    def check(rule, passed, threshold, observed, action="BLOCK"):
        checks.append({"id": rule, "passed": bool(passed), "threshold": threshold,
                       "observed": observed, "action": "PASS" if passed else action})

    try:
        now = timestamp(context["now"])
        mode, account = context["mode"], str(context["account_id"])
        snap, targets = context["snapshot"], context["targets"]
        equity, initial = snap["equity"], snap["initial_equity"]
        if not all(finite(x) and x > 0 for x in (equity, initial, snap["previous_close_equity"],
                                                 snap["peak_equity"])):
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
        positions, prices = {}, {}
        for row in snap["positions"]:
            name = str(row["symbol"])
            if name in positions or not finite(row["quantity"]) or not finite(row["price"]) or row["price"] <= 0:
                raise ValueError("Invalid or duplicate position")
            if float(row["quantity"]) != int(row["quantity"]):
                raise ValueError("Fractional share holding requires manual reconciliation")
            positions[name], prices[name] = int(row["quantity"]), float(row["price"])
        quote = (context.get("quotes") or {}).get(symbol, {})
        quote_ok = (quote.get("verified_by_relay") is True and finite(quote.get("price"))
                    and quote["price"] > 0 and _recent(quote["as_of"], now, config["quote_max_age_seconds"]))
        prices[symbol] = quote.get("price") if quote_ok else order["limit_price"]
        delta = order["quantity"] * (1 if order["side"] == "BUY" else -1)
        old, new = positions.get(symbol, 0), positions.get(symbol, 0) + delta
        reducing = old != 0 and old * delta < 0 and abs(delta) <= abs(old)
        reversal = old * new < 0
        emergency = order["intent"] in ("EMERGENCY_EXIT", "FINAL_EXIT") and reducing
        day = now.astimezone(ET).date().isoformat()
        drawdown = equity / snap["peak_equity"] - 1
        final_exit = now >= timestamp(config["final_exit_start"])
        halted = (context.get("kill_switch", {}).get("halted") is True
                  or drawdown <= config["drawdown_limit"] + 1e-12)
        pending = [o for o in context["open_orders"] if o["status"] in ACTIVE_STATES]
        pending_ids = [o["order_id"] for o in pending]
        if len(set(pending_ids)) != len(pending_ids):
            raise ValueError("Duplicate broker order identity")
        longs = {s: max(q, 0) * prices[s] for s, q in positions.items()}
        shorts = {s: max(-q, 0) * prices[s] for s, q in positions.items()}
        for o in pending:
            name = o["symbol"]
            if (not isinstance(o["remaining_quantity"], int) or o["remaining_quantity"] < 0
                    or o["side"] not in ("BUY", "SELL") or not finite(o["limit_price"])
                    or o["limit_price"] <= 0):
                raise ValueError("Invalid pending order")
            if o.get("reduce_only_verified") is True:
                continue
            px = max(o["limit_price"], prices.get(name, o["limit_price"]))
            book = longs if o["side"] == "BUY" else shorts
            book[name] = book.get(name, 0) + o["remaining_quantity"] * px
        projected = dict(positions)
        projected[symbol] = new
        if reducing:
            book = longs if old > 0 else shorts
            book[symbol] = max(0.0, book.get(symbol, 0) - abs(delta) * prices[symbol])
        else:
            book = longs if delta > 0 else shorts
            book[symbol] = book.get(symbol, 0) + abs(delta) * max(order["limit_price"], prices[symbol])
        long_gross, short_gross = sum(longs.values()), sum(shorts.values())
        names = {s for s, q in projected.items() if q != 0} | {o["symbol"] for o in pending}
        executed = set(context["executed_order_ids"])
        pending_unexecuted = {o["order_id"] for o in pending if o["order_id"] not in executed}
        used_plus_pending = len(executed) + len(pending_unexecuted) + 1
        etfs, whitelist = set(config["etf_universe"]), set(config.get("discretionary_symbols") or [])
        target_weights = dict(targets.get("weights") or {})
        account_ok = (mode in ACCOUNTS and account == ACCOUNTS.get(mode) and mode == config["mode"]
                      and str(snap["account_id"]) == account and snap["mode"] == mode
                      and str(order["account_id"]) == account and snap["account_type"] == "SIMULATE")
        check("R1", account_ok, ACCOUNTS.get(mode, "invalid mode"), account, "BLOCK_ALERT")
        flags = (context.get("eligibility") or {}).get(symbol, {})
        instrument_ok = (flags.get("verified_by_relay") is True and flags.get("market") == "US"
                         and flags.get("security_type") == ("ETF" if symbol in etfs else "COMMON_STOCK"))
        if not reducing:
            instrument_ok = (instrument_ok and flags.get("halted") is False
                             and _recent(flags.get("as_of", ""), now, config["quote_max_age_seconds"])
                             and quote_ok and quote["price"] >= config["min_price"])
            if symbol in etfs:
                size = flags.get("market_value", flags.get("market_cap"))
                instrument_ok = instrument_ok and finite(size) and size >= config["min_etf_market_value"]
        check("R2", instrument_ok or reducing,
              "US, relay-verified, right instrument type; ETF $500M fund size; $1 price; " + REDUCE_ONLY_RELIEF,
              {"type": flags.get("security_type"), "market": flags.get("market")})
        cap = config["ticker_cap_of_initial"] * initial
        over_cap = sorted(s for s in set(longs) | set(shorts)
                          if max(longs.get(s, 0), shorts.get(s, 0)) > cap + 1e-7)
        check("R3", not over_cap or reducing,
              f"any ticker, this order included, <= {config['ticker_cap_of_initial']:.0%} of initial assets "
              f"(${cap:,.0f}); SPY overflow routes to DIA then QQQ in the research run",
              {"over_cap": over_cap,
               "this_ticker": max(longs.get(symbol, 0), shorts.get(symbol, 0)) / initial})
        shorts_in_book = [s for s, w in target_weights.items() if w < 0]
        composition_ok = ((new <= 0 or symbol in etfs or symbol in whitelist)
                          and (new >= 0 or symbol in target_weights)
                          and len([s for s, q in projected.items() if q < 0]) <= config["n_short"])
        check("R4", composition_ok or reducing,
              "longs only the 10 sleeve ETFs or the discretionary whitelist; shorts only names in the "
              f"current target book; <= {config['n_short']} shorts; {REDUCE_ONLY_RELIEF}",
              {"long": symbol if new > 0 else None, "shorts": len([s for s, q in projected.items() if q < 0]),
               "requested_shorts": len(shorts_in_book)}, "BLOCK_ALERT")
        borrow_ok = True
        if new < 0 and not reducing:
            borrow_ok = (flags.get("shortable") is True and flags.get("easy_to_borrow") is True
                         and finite(flags.get("market_cap"))
                         and flags["market_cap"] >= config["min_short_market_cap"])
        check("R5", borrow_ok or reducing,
              f"a short is confirmed shortable and easy to borrow with >= "
              f"${config['min_short_market_cap']:,.0f} market cap; an un-borrowable name is skipped, "
              f"never replaced; {REDUCE_ONLY_RELIEF}",
              {"shortable": flags.get("shortable"), "easy_to_borrow": flags.get("easy_to_borrow"),
               "market_cap": flags.get("market_cap")}, "HOLD")
        budget_ok = (used_plus_pending <= config["max_orders_per_day"] or reducing) and (
            used_plus_pending + len(names) <= config["competition_order_cap"])
        check("R6", budget_ok and len(names) <= config["max_positions"],
              f"{config['max_positions']} positions; {config['max_orders_per_day']} strategy orders/day, "
              f"reductions exempt; used+pending+one exit per position <= "
              f"{config['competition_order_cap']} (the {config['emergency_flatten_reserve']}-order "
              "emergency reserve stays inside the cap)",
              {"names": len(names), "orders_used_or_pending": used_plus_pending}, "HOLD")
        check("R7", order["order_type"] == "LIMIT" and order.get("time_in_force") == "DAY"
              and order.get("session") == "RTH" and trading_window(now, emergency)
              and quote_ok and abs(order["limit_price"] / quote["price"] - 1) * 10000
              <= config["limit_band_bps"] + 1e-8,
              "RTH limit DAY, 09:35-15:45 ET; "
              f"{config['limit_band_bps']}bps of the live quote; emergency exits all day",
              order["limit_price"], "HOLD")
        trim_at = config["trim_ticker_above_of_initial"] * initial
        offending = sorted(s for s in set(longs) | set(shorts)
                           if max(longs.get(s, 0), shorts.get(s, 0)) > trim_at + 1e-7)
        trim_to = max(0, int(cap / max(prices[symbol], 1e-12)))
        check("R8", not offending or emergency
              or (reducing and symbol in offending and abs(new) <= trim_to),
              f"a name above {config['trim_ticker_above_of_initial']:.0%} of initial assets is only traded "
              f"down, to at most {config['ticker_cap_of_initial']:.0%} of it",
              {"above_trim": offending, "this_ticker_shares_after": new, "trim_shares": trim_to},
              "BLOCK_ALERT")
        check("R9", not halted or emergency,
              f"at {config['drawdown_limit']:.0%} of the peak: flatten and latch the halt", drawdown,
              "FLATTEN_HALT")
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
        check("R10", fresh or emergency,
              "verified P* target checksum, variant and config; not older than "
              f"{config['max_target_age_sessions']} completed session(s); independent emergency path",
              targets.get("signal_date"), "HOLD")
        check("R11", quote_ok and context.get("snapshot_verified_by_relay") is True
              and _recent(snap["as_of"], now, config["account_max_age_seconds"]),
              "quote and account snapshot <= 60s old, relay-verified, not in the future",
              quote.get("as_of"), "HOLD")
        stale_pending = any(o["status"] in ("UNKNOWN", "CANCEL_PENDING")
                            or not _recent(o.get("submitted_at", "2000-01-01T00:00:00+00:00"), now,
                                           config["order_ttl_seconds"]) for o in pending)
        check("R12", not stale_pending and not any(o["symbol"] == symbol for o in pending)
              and order["client_order_id"] not in context["seen_client_order_ids"] and not reversal
              and context.get("reconciled") is True,
              "reconcile first; no unknown, stale, duplicate, same-symbol or reversing order",
              {"stale": stale_pending, "duplicate": order["client_order_id"] in context["seen_client_order_ids"],
               "reversal": reversal}, "RECONCILE_HOLD")
        calendar_ok = is_session(day) and (day != "2026-11-27" or emergency)
        if mode == "competition":
            calendar_ok = (calendar_ok and timestamp(config["competition_start"]) <= now
                           <= timestamp(config["competition_stop"]))
        check("R13", calendar_ok and (not final_exit or emergency),
              "exchange session only; 2026-11-26 closed; from 2026-11-27 exits only; bounded window", day,
              "HOLD")
        fp = order_fingerprint(order, context)
        audit_expected = {"target_checksum": targets["checksum"], "snapshot_checksum": digest(snap),
                          "plan_checksum": context["plan_checksum"], "account_id": account, "mode": mode,
                          "config_checksum": digest(config)}
        audit = context.get("auditor_approval", {})
        check("R14", _approval(audit, now, config["approval_ttl_seconds"], audit_expected)
              and bool(audit.get("session_id")) and audit.get("session_id") != context.get("guard_session_id"),
              "independent auditor approval bound to plan/targets/snapshot/account/config, <= 5 min",
              audit.get("approval_id"), "HOLD")
        check("R15", mode == "competition" or any(_approval(a, now, config["approval_ttl_seconds"],
              {"order_fingerprint": fp, "account_id": account, "mode": mode})
              and a.get("consumed") is False for a in context.get("human_approvals", [])),
              "test mode: one exact-use human approval <= 5 min; competition: the rules cannot be overridden",
              mode, "AWAIT_APPROVAL")
        switch = context.get("kill_switch", {})
        check("R16", switch.get("verified_by_relay") is True
              and (switch.get("halted") is False or emergency),
              "a manual halt allows only an independently approved liquidation", switch.get("halted"), "HALT")
        check("R17", context.get("release_verified") is True and context.get("lease_verified") is True
              and order.get("workflow") == "paper-trading-gate"
              and context.get("permission_policy_verified") is True,
              "frozen release, account-wide lease, paper-trading workflow only, always_ask enforced",
              context.get("release_verified"), "HALT")
        proposed = context["approved_plan_orders"]
        destination = target_weights.get(symbol, 0.0) * equity / prices[symbol]
        check("R18", digest(proposed) == context["plan_checksum"] and order in proposed
              and (emergency or reducing
                   or (targets.get("status") == "READY"
                       and abs(new - destination) <= abs(old - destination) + 1e-9
                       and (new == 0 or destination * new >= 0))),
              "the order is one exact line of the approved plan and moves toward that plan's whole-share "
              "target; a vetoed name is never increased; " + REDUCE_ONLY_RELIEF,
              {"intent": order.get("intent"), "target_shares": destination}, "HOLD")
        drift = not targets.get("rebalance_due")
        check("R19", emergency or not drift or reducing,
              "between rebalances only the risk rules trade: no order may add exposure on a drift session",
              {"rebalance_due": targets.get("rebalance_due"), "reducing": reducing}, "BLOCK_ALERT")
        margin_used = (config["margin_long_factor"] * long_gross
                       + config["margin_short_factor"] * short_gross) / equity
        check("R20", margin_used <= config["margin_cap"] + 1e-9 or reducing,
              f"{config['margin_long_factor']}*long gross + {config['margin_short_factor']}*short gross <= "
              f"{config['margin_cap']:.0%} of equity, pending increases and discretionary positions included",
              margin_used, "HOLD")
        variant_config = pstar_variant_config(config)
        provenance = (targets.get("status") != "READY"
                      or (set(targets.get("factors") or ()) == set(variant_config["factors"])
                          and targets.get("k_scale") == variant_config["k_scale"]
                          and targets.get("strategy_id") == variant_config["strategy_id"]
                          and targets.get("variant") == config["variant"]
                          and targets.get("diagnostics", {}).get("ok") is True))
        check("R21", provenance,
              f"READY carries the {config['variant']} frozen factor set, K, strategy_id and passing "
              "competition diagnostics",
              {"variant": targets.get("variant"), "factors": len(targets.get("factors") or ()),
               "k_scale": targets.get("k_scale"), "diagnostics_ok": targets.get("diagnostics", {}).get("ok")},
              "HOLD")
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError) as exc:
        check("R0", False, "Complete, finite, trusted input contract", type(exc).__name__, "HOLD")
        fp = None
    failed = [c for c in checks if not c["passed"]]
    return {"strategy": PSTAR_STRATEGY, "variant": config.get("variant"),
            "eligible": not failed, "execution_allowed": not failed and config.get("execution_enabled") is True,
            "action": "ALLOW" if not failed else failed[0]["action"],
            "failed_rules": [c["id"] for c in failed], "checks": checks, "order_fingerprint": fp,
            "execution_note": "Pure validation only; this module has no broker or cloud client."}
