import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import supervision_notification_policy as policy

THREAD = '00000000-0000-4000-8000-000000000001'


class SupervisionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'TLS_SUPERVISOR_STATE_DIR': str(root / 'supervisors'),
                                               'TLS_SUPERVISOR_NOTIFY_DIR': str(root / 'notifications')})
        self.env.start()
        self.addCleanup(self.env.stop)
        (root / 'supervisors').mkdir()
        self.state = root / 'supervisors' / (THREAD + '.json')
        self.state.write_text(json.dumps({'active': True, 'instance_id': 'episode-a'}))

    def test_five_cycles_and_terminal_once(self):
        args = (THREAD, '继续监督完成Muse。', 'TLS_SUPERVISION_CONTINUE\n进度80%')
        for n in range(1, 5):
            self.assertTrue(policy.suppress_supervision_notification(*args, cycle_key=n))
            self.assertTrue(policy.suppress_supervision_notification(*args, cycle_key=n))
        self.assertTrue(policy.suppress_supervision_notification(*args))  # Retry probe cannot count.
        self.assertFalse(policy.suppress_supervision_notification(*args, cycle_key=5))
        self.assertFalse(policy.suppress_supervision_notification(*args, cycle_key=5))
        policy.record_supervision_notification(*args)
        self.assertTrue(policy.suppress_supervision_notification(*args, cycle_key=5))
        for n in range(6, 10):
            self.assertTrue(policy.suppress_supervision_notification(*args, cycle_key=n))
        self.assertFalse(policy.suppress_supervision_notification(*args, cycle_key=10))
        terminal = (THREAD, args[1], 'TLS_SUPERVISION_COMPLETE')
        self.assertFalse(policy.suppress_supervision_notification(*terminal))
        policy.record_supervision_notification(*terminal)
        self.assertTrue(policy.suppress_supervision_notification(*terminal))

    def test_activation_is_quiet_and_regular_active_turn_is_quiet(self):
        self.assertTrue(policy.suppress_supervision_notification(THREAD, '开启监督器，15min', '已启用监督器'))
        self.assertTrue(policy.suppress_supervision_notification(THREAD, '继续检查', '工作完成'))

    def test_real_reminder_complete_instruction_does_not_end_episode(self):
        reminder = ('继续监督完成Muse。 时间：2026-10-09 22:19:51 CST '
                    '<!-- 验收完成先停监督并首行写 TLS_SUPERVISION_COMPLETE；'
                    '否则首行 TLS_SUPERVISION_CONTINUE。 -->')
        args = (THREAD, reminder, 'TLS_SUPERVISION_CONTINUE 任务继续运行')
        for cycle in range(1, 6):
            self.assertEqual(policy.suppress_supervision_notification(*args, cycle_key=cycle), cycle < 5)
        policy.record_supervision_notification(*args)
        entry = json.loads((policy._ledger() / 'state.json').read_text())[THREAD]
        self.assertFalse(entry['terminal_sent'])

    def test_inline_completion_marker_is_not_a_terminal_report(self):
        self.assertTrue(policy.suppress_supervision_notification(
            THREAD, '继续监督完成Muse。', '未完成，不能报告 TLS_SUPERVISION_COMPLETE', cycle_key=1))

    def test_old_poisoned_twenty_cycle_ledger_is_retired(self):
        path = policy._ledger() / 'state.json'
        path.write_text(json.dumps({THREAD: {'episode': 'episode-a', 'cadence': 'cycles20',
                                             'cycles': 0, 'seen': [], 'terminal_sent': True}}))
        args = (THREAD, '继续监督完成Muse。', 'TLS_SUPERVISION_CONTINUE')
        for cycle in range(1, 6):
            self.assertEqual(policy.suppress_supervision_notification(*args, cycle_key=cycle), cycle < 5)

    def test_remote_sender_allows_new_admitted_progress_after_completion(self):
        self.state.unlink()
        calls = []

        @policy.supervised_delivery
        def deliver(session_id, user_message, answer, event_key):
            calls.append(event_key)
            return True

        deliver(THREAD, '继续监督完成Muse。', 'TLS_SUPERVISION_COMPLETE', 'episode-a-final')
        deliver(THREAD, '继续监督完成Muse。', 'TLS_SUPERVISION_CONTINUE', 'episode-b-cycle5')
        self.assertEqual(calls, ['episode-a-final', 'episode-b-cycle5'])

    def test_stopped_supervisor_final_and_normal_conversation(self):
        self.state.write_text(json.dumps({'active': False, 'instance_id': 'episode-a', 'stopped_at': 500}))
        self.assertFalse(policy.suppress_supervision_notification(THREAD, '继续监督完成Muse。', '状态：停止'))
        policy.record_supervision_notification(THREAD, '继续监督完成Muse。', '状态：停止')
        self.assertTrue(policy.suppress_supervision_notification(THREAD, '继续监督完成Muse。', '状态：停止'))
        self.assertFalse(policy.suppress_supervision_notification(THREAD, '普通问题', '正常回复'))

    def test_new_instance_resets_terminal_dedup(self):
        args = (THREAD, '继续监督完成Muse。', 'TLS_SUPERVISION_COMPLETE')
        policy.record_supervision_notification(*args)
        self.state.write_text(json.dumps({'active': True, 'instance_id': 'episode-b'}))
        self.assertFalse(policy.suppress_supervision_notification(*args))

    def test_actual_sender_drops_catchup_and_retries_failed_delivery(self):
        self.state.unlink()  # Remote sender has no local supervisor lifecycle file.
        sender = mock.Mock(return_value=False)

        @policy.supervised_delivery
        def deliver(session_id, user_message, answer):
            return sender(session_id, user_message, answer)

        args = (THREAD, '继续监督完成Muse。', 'TLS_SUPERVISION_CONTINUE')
        with mock.patch.object(policy.time, 'time', return_value=100000):
            self.assertFalse(deliver(*args))
            sender.return_value = True
            self.assertTrue(deliver(*args))
            self.assertTrue(deliver(*args))
            self.assertEqual(sender.call_count, 2)
            self.assertTrue(deliver(THREAD, args[1], 'TLS_SUPERVISION_COMPLETE'))
            self.assertEqual(sender.call_count, 3)
        with mock.patch.object(policy.time, 'time', return_value=121600):
            self.assertTrue(deliver(*args))
            self.assertEqual(sender.call_count, 3)  # Finished episodes remain quiet.

    def test_distinct_admitted_events_with_identical_progress_both_deliver(self):
        self.state.unlink()
        calls = []

        @policy.supervised_delivery
        def deliver(session_id, user_message, answer, event_key):
            calls.append(event_key)
            return True

        args = (THREAD, '继续监督完成Muse。', 'TLS_SUPERVISION_CONTINUE 80%')
        self.assertTrue(deliver(*args, 'cycle5'))
        self.assertTrue(deliver(*args, 'cycle5'))
        self.assertTrue(deliver(*args, 'cycle10'))
        self.assertEqual(calls, ['cycle5', 'cycle10'])
