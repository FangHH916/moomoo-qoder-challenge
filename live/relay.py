from __future__ import annotations

from decimal import Decimal

from rules.gate import evaluate_order


def paper_payload(order: dict, market_id: int) -> dict:
    """The exact `sim_trade_input_order` arguments for one planned ticket.

    LIMIT carries the marketable price the plan computed. MARKET carries no price at all: the tool schema
    says price is optional for a market order and the server prices it, and a stale quote here would only
    make the ticket look like a limit. The gate (R7) is what allows MARKET at all, and it allows it only for
    a FINAL_EXIT reduction inside the flatten window.
    """
    if market_id != 100:
        raise ValueError("Verified US paper-margin market ID 100 required")
    if order["session"] != "RTH":
        raise ValueError("Only regular-hours paper orders supported")
    order_type = str(order["order_type"]).upper()
    if order_type not in ("LIMIT", "MARKET"):
        raise ValueError("Only LIMIT and MARKET tickets relay")
    payload = {"acc_id": str(order["account_id"]), "market": market_id, "symbol": "US." + order["symbol"],
               "order_type": 1 if order_type == "LIMIT" else 3,
               "order_side": 1 if order["side"] == "BUY" else 2, "qty": str(order["quantity"]),
               "order_trade_time_type": 1, "text": order["client_order_id"]}
    if order_type == "LIMIT":
        payload["price"] = format(Decimal(str(order["limit_price"])), "f")
    return payload


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
