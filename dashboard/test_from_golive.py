import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import from_golive as converter


class SnapshotTests(unittest.TestCase):
    def test_current_snapshot_not_old_standalone_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / '2026-11-20' / '5832812'
            new = root / '2026-11-23' / '5832812'
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            (old / 'positions.json').write_text(json.dumps({'data': {'positions': [
                {'symbol': 'IWM', 'qty': 30, 'cur_price': 250}]}}))
            snapshot = {'as_of': '2026-11-23T15:29:00Z', 'equity': 100240,
                        'previous_close_equity': 100001.62,
                        'positions': [{'symbol': 'AAOI', 'quantity': -2, 'price': 40}],
                        'open_orders': [{'order_id': '1', 'symbol': 'AAOI', 'side': 'SELL',
                                         'quantity': 2, 'filled': 2, 'state': 'FILLED'}]}
            (new / 'snapshot-new.json').write_text(json.dumps(snapshot))
            with patch.object(converter, 'GOLIVE', root):
                result = converter.build('5832812')
                self.assertEqual(result['positions'][0]['quantity'], -2)
                self.assertEqual(result['positions'][0]['symbol'], 'AAOI')
                self.assertEqual(result['orders'][0]['status'], 'FILLED')
                self.assertEqual(result['previous_close_equity'], 100001.62)
                # A flat snapshot must stay flat, not revive previous holdings.
                snapshot['positions'] = []
                snapshot['open_orders'] = []
                (new / 'snapshot-new.json').write_text(json.dumps(snapshot))
                self.assertEqual(converter.build('5832812')['positions'], [])
                del snapshot['positions']
                (new / 'snapshot-new.json').write_text(json.dumps(snapshot))
                self.assertIsNone(converter.build('5832812')['positions'])

    def test_timestamp_beats_file_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'snapshot-new.json').write_text(json.dumps(
                {'as_of': '2026-11-23T15:29:00Z', 'equity': 100240}))
            (root / 'snapshot-old.json').write_text(json.dumps(
                {'as_of': '2026-11-23T15:07:00Z', 'equity': 99911}))
            self.assertEqual(converter.day_close(root)[1], 100240)


if __name__ == '__main__':
    unittest.main()
