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


if __name__ == "__main__":
    unittest.main()
