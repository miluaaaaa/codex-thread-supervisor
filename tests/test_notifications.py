import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import notifications

THREAD = '00000000-0000-4000-8000-000000000001'


class OutboxTests(unittest.TestCase):
    def test_fifth_report_durable_then_next_fifth_and_final(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {'thread_id': THREAD, 'instance_id': 'test', 'active': True, 'task': '示例任务'}
            (root / (THREAD + '.json')).write_text(json.dumps(state))
            with mock.patch.dict(os.environ, {'SUPERVISOR_STATE_DIR': str(root),
                                             'TLS_SUPERVISOR_STATE_DIR': str(root),
                                             'TLS_SUPERVISOR_NOTIFY_DIR': str(root / 'notify'),
                                             'SUPERVISOR_OUTBOX_DIR': str(root / 'outbox')}):
                for cycle in range(1, 11):
                    self.assertEqual(notifications.queue_report(state, str(cycle), 'TLS_SUPERVISION_CONTINUE'), cycle % 5 == 0)
                    self.assertFalse(notifications.queue_report(state, str(cycle), 'TLS_SUPERVISION_CONTINUE'))
                    self.assertEqual(len(list((root / 'outbox').glob('*.json'))), cycle // 5)
                self.assertTrue(notifications.queue_report(state, 'final', 'TLS_SUPERVISION_COMPLETE'))
                self.assertFalse(notifications.queue_report(state, 'final', 'TLS_SUPERVISION_COMPLETE'))
                self.assertEqual(len(list((root / 'outbox').glob('*.json'))), 3)
                self.assertEqual((root / 'outbox').stat().st_mode & 0o777, 0o700)
                for file in (root / 'outbox').glob('*.json'):
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_failed_enqueue_does_not_consume_fifth_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = {'thread_id': THREAD, 'instance_id': 'test', 'active': True, 'task': '示例任务'}
            (root / (THREAD + '.json')).write_text(json.dumps(state))
            with mock.patch.dict(os.environ, {'SUPERVISOR_STATE_DIR': str(root), 'TLS_SUPERVISOR_STATE_DIR': str(root),
                                             'TLS_SUPERVISOR_NOTIFY_DIR': str(root / 'notify'), 'SUPERVISOR_OUTBOX_DIR': str(root / 'outbox')}):
                for cycle in range(1, 5):
                    notifications.queue_report(state, str(cycle), 'TLS_SUPERVISION_CONTINUE')
                with mock.patch.object(notifications, 'atomic', side_effect=OSError('disk unavailable')):
                    with self.assertRaises(OSError):
                        notifications.queue_report(state, '5', 'TLS_SUPERVISION_CONTINUE')
                self.assertTrue(notifications.queue_report(state, '5', 'TLS_SUPERVISION_CONTINUE'))
                self.assertFalse(notifications.queue_report(state, '5', 'TLS_SUPERVISION_CONTINUE'))
