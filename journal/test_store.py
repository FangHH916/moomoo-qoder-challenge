import json
import tempfile
import unittest
from pathlib import Path

from journal.store import Journal, reconcile_order


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name))

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def test_fill_deduplication_and_conflict(self):
        row = {"event_id": "evt", "mode": "test", "account_id": "5322438", "order_id": "o1", "fill_id": "f1", "quantity": 1, "price": 100}
        self.assertTrue(self.journal.append("fills", row))
        self.assertFalse(self.journal.append("fills", row))
        row["price"] = 101
        with self.assertRaises(ValueError):
            self.journal.append("fills", row)

    def test_rejected_account_is_loggable_not_fillable(self):
        row = {"event_id": "r1", "mode": "test", "account_id": "999", "reason": "R1"}
        self.assertTrue(self.journal.append("decisions", row))
        with self.assertRaises(ValueError):
            self.journal.append("fills", row)

    def test_defaults_halted_and_halt_persists(self):
        self.assertTrue(self.journal.control("5322438")["halted"])
        self.journal.set_halt("5322438", "R9")
        self.assertEqual(self.journal.control("5322438")["reason"], "R9")

    def test_account_lock_and_one_use_approval(self):
        self.assertTrue(self.journal.acquire("5322438", "run1"))
        self.assertFalse(self.journal.acquire("5322438", "run2"))
        self.journal.release("5322438", "run2")
        self.assertFalse(self.journal.acquire("5322438", "run2"))
        self.journal.release("5322438", "run1")
        self.assertTrue(self.journal.acquire("5322438", "run2"))
        self.assertTrue(self.journal.consume_approval("a1", "fp"))
        self.assertFalse(self.journal.consume_approval("a1", "fp"))

    def test_secrets_rejected(self):
        for row in ({"api_key": "value"}, {"reason": "Bearer abc"}):
            with self.assertRaises(ValueError):
                self.journal.append("decisions", row)

    def test_partial_cancellation_requires_actual_fill_details(self):
        state = reconcile_order({"cumulative_quantity": 2}, {"order_id": "o", "quantity": 10, "cumulative_quantity": 5, "status": "CANCELLED"})
        self.assertEqual(state["status"], "CANCELLED_PARTIAL")
        self.assertEqual(state["new_filled_quantity"], 3)
        self.assertTrue(state["requires_fill_details"])
        with self.assertRaises(ValueError):
            reconcile_order({"cumulative_quantity": 5}, {"order_id": "o", "quantity": 10, "cumulative_quantity": 4, "status": "OPEN"})


if __name__ == "__main__":
    unittest.main()
