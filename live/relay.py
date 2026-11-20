from __future__ import annotations

from decimal import Decimal

from rules.gate import evaluate_order


def paper_payload(order: dict, market_id: int) -> dict:
    if market_id != 100:
        raise ValueError("Verified US paper-margin market ID 100 required")
    if order["order_type"] != "LIMIT" or order["session"] != "RTH":
        raise ValueError("Only regular-hours paper limit orders supported")
    return {"acc_id": str(order["account_id"]), "market": market_id, "symbol": "US." + order["symbol"],
            "order_type": 1, "order_side": 1 if order["side"] == "BUY" else 2,
            "qty": str(order["quantity"]), "price": format(Decimal(str(order["limit_price"])), "f"),
            "order_trade_time_type": 1, "text": order["client_order_id"]}


def inspect_pending_call(event: dict, order: dict, context: dict, config: dict) -> dict:
    reasons = []
    if event.get("type") != "agent.mcp_tool_use" or event.get("evaluated_permission") != "ask":
        reasons.append("An enforced MCP confirmation event is required")
    if event.get("name") != "sim_trade_input_order" or event.get("mcp_server_name") != context.get("mcp_server_name"):
        reasons.append("Unexpected server or non-paper-order capability")
    try:
        expected = paper_payload(order, context["snapshot"]["market_id"])
        if event.get("input") != expected:
            reasons.append("Raw arguments differ from the approved order")
    except (ValueError, KeyError):
        reasons.append("Unverified account/market mapping")
    if context.get("broker_day_tif_verified") is not True:
        reasons.append("MCP has no TIF parameter; broker DAY semantics require verification")
    result = evaluate_order(order, context, config)
    if not result["eligible"]:
        reasons.extend(result["failed_rules"])
    if config.get("execution_enabled") is not True:
        reasons.append("Execution is inactive")
    return {"event_id": event.get("id"), "would_allow": not reasons, "reasons": reasons, "gate": result,
            "note": "Offline inspection only. No cloud confirmation, approval consumption or broker call is performed."}
