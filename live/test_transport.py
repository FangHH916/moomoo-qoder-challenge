import copy
import unittest

from live.transport import ForwardFiles, prepare_schedule_update
from rules.test_gate import fixture


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.targets = fixture()[1]["targets"]
        self.schedule = {"id": "sched_test", "identity_id": "idn_test", "metadata": {"target_file_id": "old"},
            "resources": [{"type": "file", "file_id": "old"}, {"type": "file", "file_id": "rules", "mount_path": "/data/rules.zip"}],
            "initial_events": [{"type": "user.message", "content": "Run readiness only. No new orders."}]}

    def test_preserve_other_resources_and_instructions(self):
        result = prepare_schedule_update(self.schedule, self.targets, "new")
        self.assertEqual([r["file_id"] for r in result["resources"]], ["rules", "new"])
        self.assertIn("Run readiness only.", result["initial_events"][0]["content"])
        self.assertIn(self.targets["checksum"], result["initial_events"][0]["content"])
        self.assertNotIn("trigger_policy", result)

    def test_repeated_payload_does_not_accumulate_instructions(self):
        first = prepare_schedule_update(self.schedule, self.targets, "new")
        second = prepare_schedule_update({**self.schedule, **first}, self.targets, "new")
        self.assertEqual(first, second)

    def test_checksum_mismatch_rejected(self):
        self.targets["weights"]["AAA"] = 1
        with self.assertRaises(ValueError):
            prepare_schedule_update(self.schedule, self.targets, "new")

    def test_writes_are_inactive(self):
        api = ForwardFiles("not-a-credential", "idn_test")
        with self.assertRaises(PermissionError):
            api.upload("unread-file")
        with self.assertRaises(PermissionError):
            api.update_existing(self.schedule, self.targets, "new")

    def test_conflicting_mount_rejected(self):
        self.schedule["resources"].append({"type": "file", "file_id": "conflict", "mount_path": "/data/workspace/targets_20261119.json"})
        with self.assertRaises(ValueError):
            prepare_schedule_update(self.schedule, self.targets, "new")

    def test_session_style_message_rejected(self):
        self.schedule["initial_events"][0]["content"] = [{"type": "text", "text": "Read"}]
        with self.assertRaises(ValueError):
            prepare_schedule_update(self.schedule, self.targets, "new")


if __name__ == "__main__":
    unittest.main()
