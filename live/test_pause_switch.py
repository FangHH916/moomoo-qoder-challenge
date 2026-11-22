"""Tests for the shared pause switch transport (`live/pause_switch.py`).

Every case is written against the deployed `live/ops.json` registry rather than a copy of it, because that
registry is the thing the two executors have to agree on: a fixture that renamed a sentinel would pass while
the real switch stayed broken. The MCP is never called -- these cover the parser and the payload builder, the
same relay half `live/test_relay.py` covers for orders. `codes_in` yields bare codes, so the `current` sets
handed to `write_plan` are bare too, exactly as the runbook passes them.
"""
import unittest
from datetime import datetime, timedelta

from live.pause_switch import (ALWAYS_ALLOWED_INTENTS, AMBIGUOUS, EXPIRED, FREE, LEASED, PAUSED, RUNNING,
                               SwitchError, UNKNOWN, codes_in, load_ops, may_trade, read_payload, registry,
                               ring_token, slack_text, switch_state, validate, write_plan)

OPS = load_ops()
REG = registry(OPS)
# 2026-11-23 09:35 ET: the scheduled local entry day, before its 16:00 ET lease deadline.
NOW = datetime.fromisoformat("2026-11-23T14:35:00+00:00")
LOCAL_DEADLINE = "2026-11-23T16:00:00-05:00"
BROKER = REG["full"]


def raw(*codes):
    return {"ret_code": 0, "data": {"security_list": [{"code": code} for code in codes]}}


def bare(code):
    return str(code).removeprefix("US.")


class RegistryTests(unittest.TestCase):
    def test_the_registry_is_the_shared_table_both_executors_read(self):
        self.assertEqual(REG["group"], OPS["pause_switch"]["group"])
        self.assertEqual(set(REG["state"]), {RUNNING, PAUSED})
        self.assertEqual(set(REG["lease"]), {"local-entry", "cloud-executor"})
        self.assertEqual(REG["hours"], {"local-entry": OPS["pause_switch"]["lease_hours_local_entry"],
                                        "cloud-executor": OPS["pause_switch"]["lease_hours_cloud_executor"]})
        self.assertTrue(REG["block_on_unknown"])
        self.assertEqual(REG["never_blocks"], list(ALWAYS_ALLOWED_INTENTS))
        self.assertTrue(REG["ring"])
        self.assertEqual(sorted(REG["full"]), sorted({*REG["state"].values(), *REG["lease"].values(),
                                                      *REG["ring"]}))

    def test_the_sentinels_can_never_be_symbols_the_strategy_trades(self):
        config = {"etf_universe": ["TLT"], "etf_sleeve": ["GLD"], "spy_route": ["IEF"],
                  "discretionary_symbols": ["NVDA"]}
        self.assertEqual(validate(OPS, config, {"DIA": 0.1}), [])
        # A sentinel that is also in the book makes the flag ambiguous with a real holding: refuse the config.
        for field, name in (("etf_universe", REG["state"]["running"]), ("spy_route", REG["lease"]["local-entry"]),
                            ("discretionary_symbols", REG["ring"][0])):
            with self.subTest(field=field):
                problems = validate(OPS, {**config, field: [bare(name)]})
                self.assertEqual(len(problems), 1)
                self.assertIn("switch sentinel and a tradable symbol", problems[0])
        self.assertIn("switch sentinel and a tradable symbol",
                      validate(OPS, config, {bare(REG["state"]["paused"]): 0.1})[0])

    def test_a_shared_or_missing_sentinel_is_an_error_not_a_warning(self):
        broken = {"pause_switch": {**OPS["pause_switch"], "lease": {
            **OPS["pause_switch"]["lease"], "cloud-executor": OPS["pause_switch"]["state"]["paused"]}}}
        self.assertIn("used twice", validate(broken, {})[0])
        for key in ("state", "lease", "lease_day_ring"):
            with self.subTest(missing=key):
                with self.assertRaises(SwitchError):
                    registry({"pause_switch": {k: v for k, v in OPS["pause_switch"].items() if k != key}})

    def test_the_read_is_the_only_call_a_run_needs_before_it_touches_the_broker(self):
        self.assertEqual(read_payload(OPS), {"tool": "quote_user_security",
                                             "payload": {"group_name": REG["group"]}})


class ReadTests(unittest.TestCase):
    def test_a_running_flag_with_a_live_local_lease_blocks_a_cloud_entry(self):
        write = write_plan({"state": "running", "lease": "local-entry"}, OPS, NOW, set())
        state = switch_state(raw(*write["expected_codes"]), OPS, NOW)
        self.assertEqual(state["state"], RUNNING)
        self.assertEqual(state["lease_holder"], "local-entry")
        self.assertEqual(state["lease_status"], LEASED)
        self.assertEqual(state["lease_taken_et_day"], "2026-11-23")
        self.assertEqual(state["lease_expires_at"], write["lease_expires_at"])
        self.assertEqual(state["lease_expires_at"], LOCAL_DEADLINE)
        self.assertEqual(state["reasons"], [])
        held = may_trade(state, OPS, "ENTRY", "cloud-executor")
        self.assertFalse(held["allow"])
        self.assertIn(f"the live lease belongs to local-entry until {LOCAL_DEADLINE}", held["reasons"][0])

    def test_a_paused_flag_blocks_entries_and_never_blocks_reductions(self):
        state = switch_state(raw(REG["state"]["paused"]), OPS, NOW)
        self.assertEqual(state["state"], PAUSED)
        self.assertEqual(state["lease_status"], FREE)
        self.assertEqual(may_trade(state, OPS, "ENTRY", "cloud-executor"),
                         {"allow": False, "intent": "ENTRY", "switch": PAUSED,
                          "reasons": ["the switch is paused: send no new order"]})
        for intent in ALWAYS_ALLOWED_INTENTS:
            allowed = may_trade(state, OPS, intent, "cloud-executor")
            self.assertTrue(allowed["allow"], intent)
            self.assertIn("it passes whatever the switch says", allowed["reasons"][0])
            self.assertIn("reduction", allowed["reasons"][0])

    def test_an_empty_group_has_never_been_written_so_the_state_is_unknown(self):
        state = switch_state(raw("US.SPY"), OPS, NOW)
        self.assertEqual(state["state"], UNKNOWN)
        self.assertEqual(state["lease_status"], FREE)
        self.assertIn("never been written", state["reasons"][0])
        decision = may_trade(state, OPS, "ENTRY", "local-entry")
        self.assertFalse(decision["allow"])
        self.assertIn("the switch is unknown", decision["reasons"][0])

    def test_both_state_sentinels_present_is_a_contradiction_not_a_default(self):
        state = switch_state(raw(REG["state"]["running"], REG["state"]["paused"]), OPS, NOW)
        self.assertEqual(state["state"], UNKNOWN)
        self.assertIn("contradictory", state["reasons"][0])
        self.assertFalse(may_trade(state, OPS, "ENTRY")["allow"])

    def test_a_lease_day_token_with_no_holder_is_an_ambiguous_lease(self):
        state = switch_state(raw(REG["state"]["running"], REG["ring"][0]), OPS, NOW)
        self.assertEqual(state["state"], RUNNING)
        self.assertEqual(state["lease_status"], AMBIGUOUS)
        self.assertIn("with no lease holder", state["reasons"][0])
        self.assertFalse(may_trade(state, OPS, "ENTRY", "cloud-executor")["allow"])

    def test_two_lease_tokens_cannot_be_resolved_into_a_holder(self):
        state = switch_state(raw(REG["state"]["running"], REG["lease"]["local-entry"],
                                 REG["lease"]["cloud-executor"]), OPS, NOW)
        self.assertEqual(state["lease_status"], AMBIGUOUS)
        self.assertIn("more than one lease token", state["reasons"][0])
        self.assertFalse(may_trade(state, OPS, "ENTRY", "cloud-executor")["allow"])

    def test_a_lease_taken_yesterday_has_expired_at_its_own_16(self):
        write = write_plan({"state": "running", "lease": "cloud-executor"}, OPS,
                           NOW - timedelta(days=1), set())
        self.assertEqual(write["lease_expires_at"],
                         f"2026-11-22T{REG['hours']['cloud-executor']}:00-05:00")
        flag = switch_state(raw(*write["expected_codes"]), OPS, NOW)
        self.assertEqual(flag["lease_status"], EXPIRED)
        self.assertIn("it has expired", flag["reasons"][0])
        # A cloud entry may take the book once the other lease has aged out of the ring.
        self.assertTrue(may_trade(flag, OPS, "ENTRY", "cloud-executor")["allow"])
        self.assertEqual(switch_state(raw(*write["expected_codes"]), OPS,
                                      NOW - timedelta(days=1))["lease_status"], LEASED)

    def test_a_flag_older_than_the_window_is_not_evidence_about_the_present(self):
        state = switch_state(raw(REG["state"]["running"]), OPS, NOW,
                             (NOW - timedelta(seconds=600)).isoformat())
        self.assertFalse(state["available"])
        self.assertEqual(state["state"], UNKNOWN)
        self.assertIn("re-read it", state["reasons"][0])
        self.assertIn("could not be read", may_trade(state, OPS, "ENTRY")["reasons"][0])

    def test_a_broker_error_or_a_missing_list_never_reads_as_running(self):
        for bad, why in (({"ret_code": 300400, "ret_msg": "group not found"}, "group not found"),
                         ({"ret_code": 0, "data": {}}, "not the switch group")):
            with self.subTest(why=why):
                state = switch_state(bad, OPS, NOW)
                self.assertFalse(state["available"])
                self.assertEqual(state["state"], UNKNOWN)
                self.assertIn(why, state["reasons"][0])
                self.assertIn("could not be read", may_trade(state, OPS, "ENTRY")["reasons"][0])
        with self.assertRaises(SwitchError):
            codes_in(None)

    def test_a_running_flag_with_no_lease_is_takeable_by_either_executor(self):
        state = switch_state(raw(REG["state"]["running"]), OPS, NOW)
        self.assertEqual(state["lease_status"], FREE)
        for holder in ("local-entry", "cloud-executor"):
            decision = may_trade(state, OPS, "ENTRY", holder)
            self.assertTrue(decision["allow"], holder)
            self.assertIn("may take it", decision["reasons"][0])
        self.assertTrue(may_trade(state, OPS, "ENTRY")["allow"])


class WriteTests(unittest.TestCase):
    def test_a_pause_writes_one_add_and_one_scoped_move_out(self):
        write = write_plan({"state": "paused", "lease": "cloud-executor"}, OPS, NOW,
                           {bare(REG["state"]["running"]), "SPY", "QQQ"})
        self.assertEqual([row["payload"]["op"] for row in write["writes"]], ["ADD", "MOVE_OUT"])
        add, move_out = (row["payload"] for row in write["writes"])
        self.assertEqual(add["group_name"], REG["group"])
        self.assertEqual(move_out["group_name"], REG["group"])
        self.assertEqual(move_out["code_list"], [BROKER[REG["state"]["running"]]])
        self.assertEqual(set(add["code_list"]),
                         {BROKER[REG["state"]["paused"]], BROKER[REG["lease"]["cloud-executor"]],
                          BROKER[ring_token(REG, NOW)]})
        self.assertIn("never use op=DEL", write["remove_from_all_groups"])

    def test_the_write_plan_only_touches_switch_tokens(self):
        write = write_plan({"state": "paused", "lease": None}, OPS, NOW,
                           {"SPY", "QQQ", "TLT", bare(REG["state"]["running"])})
        self.assertEqual([row["payload"]["op"] for row in write["writes"]], ["ADD", "MOVE_OUT"])
        for row in write["writes"]:
            for code in row["payload"]["code_list"]:
                self.assertIn(code, set(REG["full"].values()), code)
        self.assertEqual(write["writes"][1]["payload"]["code_list"], [BROKER[REG["state"]["running"]]])

    def test_a_flag_that_already_agrees_produces_no_write(self):
        write = write_plan({"state": "running", "lease": None}, OPS, NOW, {bare(REG["state"]["running"])})
        self.assertEqual(write["writes"], [])
        self.assertFalse(write["changed"])
        self.assertEqual(write["expected_codes"], [BROKER[REG["state"]["running"]]])
        self.assertIn("already reads as intended", write["note"])

    def test_an_unknown_state_or_lease_is_refused_before_any_payload_exists(self):
        for desired in ({"state": "PAUSE"}, {"state": "running", "lease": "the-user"}, {"state": None}):
            with self.subTest(desired=desired):
                with self.assertRaises(SwitchError):
                    write_plan(desired, OPS, NOW, set())

    def test_the_local_lease_deadline_is_the_local_one(self):
        write = write_plan({"state": "running", "lease": "local-entry"}, OPS, NOW, set())
        self.assertEqual(write["lease_expires_at"], LOCAL_DEADLINE)
        state = switch_state(raw(*write["expected_codes"]), OPS, NOW)
        self.assertEqual(set(state["codes"]), {bare(code) for code in write["expected_codes"]})
        self.assertTrue(may_trade(state, OPS, "ENTRY", "local-entry")["allow"])
        self.assertFalse(may_trade(state, OPS, "ENTRY", "cloud-executor")["allow"])

    def test_the_ring_token_recovers_the_et_day_the_lease_was_taken(self):
        write = write_plan({"state": "paused", "lease": "cloud-executor"}, OPS, NOW, set())
        self.assertEqual(write["et_day"], "2026-11-23")
        self.assertIn(BROKER[ring_token(REG, NOW)], write["expected_codes"])
        state = switch_state(raw(*write["expected_codes"]), OPS, NOW)
        self.assertEqual(state["lease_taken_et_day"], write["et_day"])
        self.assertEqual(state["lease_status"], LEASED)
        self.assertEqual(state["lease_ring_token"], ring_token(REG, NOW))


class SlackTextTests(unittest.TestCase):
    def test_the_thread_reply_states_the_group_state_codes_and_lease(self):
        state = switch_state(raw(REG["state"]["paused"]), OPS, NOW)
        self.assertEqual(slack_text(state, "user-09012345678"),
                         f"switch {REG['group']}: {PAUSED} (codes {' '.join(state['codes'])}, "
                         f"lease {FREE}) - from user-09012345678")

    def test_an_unreadable_flag_still_answers_so_the_user_is_not_left_in_silence(self):
        self.assertIn(UNKNOWN, slack_text(switch_state(None, OPS, NOW), "user-09012345678"))


if __name__ == "__main__":
    unittest.main()
