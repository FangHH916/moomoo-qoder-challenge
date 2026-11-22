import unittest

from live.relay import inspect_pending_call, paper_payload
from rules.test_gate import fixture


class RelayTests(unittest.TestCase):
    def test_raw_payload_mapping_no_execution(self):
        order, context, config = fixture()
        context["snapshot"]["market_id"] = 100
        context["mcp_server_name"] = "moomoo"
        event = {"id": "evt_fixture", "type": "agent.mcp_tool_use", "evaluated_permission": "ask",
                 "name": "sim_trade_input_order", "mcp_server_name": "moomoo", "input": paper_payload(order, 100)}
        result = inspect_pending_call(event, order, context, config)
        self.assertFalse(result["would_allow"])
        self.assertIn("Execution is inactive", result["reasons"])
        self.assertIn("MCP has no TIF parameter; broker DAY semantics require verification", result["reasons"])
        event["input"]["acc_id"] = "5832812"
        self.assertIn("Raw arguments differ from the approved order", inspect_pending_call(event, order, context, config)["reasons"])

    def test_live_order_tool_and_allow_policy_are_rejected(self):
        order, context, config = fixture()
        result = inspect_pending_call({"type": "agent.mcp_tool_use", "name": "trading_order_place", "evaluated_permission": "allow"}, order, context, config)
        self.assertFalse(result["would_allow"])
        self.assertIn("Unexpected server or non-paper-order capability", result["reasons"])

    def test_a_limit_ticket_carries_its_price_and_a_market_ticket_carries_none(self):
        """`sim_trade_input_order` takes order_type 1 with a price or 3 without one.

        Sending a price with a market order would make the broker treat it as a limit, so the relay omits
        the field even when the ticket dict still carries the stale limit it was repriced from.
        """
        limit = {"account_id": "5832812", "symbol": "SPY", "side": "SELL", "quantity": 4,
                 "limit_price": 611.23, "order_type": "LIMIT", "session": "RTH",
                 "client_order_id": "pstar-2026-11-27-SPY-SELL-4-0123456789ab"}
        market = {**limit, "order_type": "MARKET", "limit_price": None}
        self.assertEqual(paper_payload(limit, 100),
                         {"acc_id": "5832812", "market": 100, "symbol": "US.SPY", "order_type": 1,
                          "order_side": 2, "qty": "4", "order_trade_time_type": 1,
                          "text": limit["client_order_id"], "price": "611.23"})
        payload = paper_payload(market, 100)
        self.assertEqual(payload["order_type"], 3)
        self.assertNotIn("price", payload)
        self.assertEqual(payload["text"], limit["client_order_id"])

    def test_only_limit_and_market_relay(self):
        base = {"account_id": "5832812", "symbol": "SPY", "side": "SELL", "quantity": 4,
                "limit_price": 611.23, "order_type": "LIMIT", "session": "RTH",
                "client_order_id": "pstar-2026-11-27-SPY-SELL-4-0123456789ab"}
        for mutation in ({"order_type": "STOP"}, {"order_type": None}, {"session": "RTH+PRE+POST"}):
            with self.subTest(**mutation), self.assertRaises(ValueError):
                paper_payload({**base, **mutation}, 100)
        self.assertRaises(ValueError, paper_payload, base, 101)


if __name__ == "__main__":
    unittest.main()
