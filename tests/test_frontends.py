import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import frontends

THREAD = '00000000-0000-4000-8000-000000000001'


class FrontendTests(unittest.TestCase):
    def test_binding_rejects_wrong_thread_or_reused_pid(self):
        record = {'terminal_name': '%3', 'codex_pid': 1201}
        pane = {'pane': '%3', 'pid': 1200, 'tty': 'pts/9'}
        process = {'threads': {THREAD}, 'tty': 'pts/9', 'start': '12'}
        with mock.patch.object(frontends, 'pane_info', return_value=pane), mock.patch.object(
            frontends, 'process_info', return_value=process
        ), mock.patch.object(frontends, 'descendant', return_value=True):
            good = frontends.verify_record(THREAD, record)
            process['start'] = '13'
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                frontends.verify_record(THREAD, good)
            process['threads'] = {'00000000-0000-4000-8000-000000000002'}
            with self.assertRaises(RuntimeError):
                frontends.verify_record(THREAD, record)

    def test_one_pane_cannot_bind_two_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(frontends, 'CONFIG_ROOT', root), mock.patch.object(frontends, 'REGISTRY', root / 'frontends.json'), mock.patch.object(
                frontends, 'DEFAULT_INCLUDED_SESSIONS_FILE', root / 'included'
            ), mock.patch.object(frontends, 'verify_record', return_value={'terminal_name': '%3'}):
                frontends.bind(THREAD, '%3', 1)
                with self.assertRaisesRegex(RuntimeError, 'already bound'):
                    frontends.bind('00000000-0000-4000-8000-000000000002', '%3', 2)
                self.assertEqual(frontends.session_ids(root / 'included'), {THREAD})

    def test_named_buffer_is_verified_before_any_keys_are_sent(self):
        with mock.patch.object(frontends, 'pane_info'), mock.patch.object(frontends, 'tmux') as call:
            call.return_value = 'corrupted buffer'
            with self.assertRaisesRegex(RuntimeError, 'verification'):
                frontends.deliver_tmux_session_turn('%3', 'hello')
            self.assertFalse(any(c.args[0] == 'send-keys' for c in call.call_args_list))

    def test_delivery_clears_then_bracketed_pastes_and_recaptures(self):
        def reply(*args, **kwargs):
            return 'hello' if args[0] == 'save-buffer' else ''
        with mock.patch.object(frontends, 'pane_info'), mock.patch.object(frontends, 'tmux', side_effect=reply) as call:
            frontends.deliver_tmux_session_turn('%3', 'hello')
            actions = [c.args[0] for c in call.call_args_list]
            self.assertEqual(actions, ['load-buffer', 'save-buffer', 'send-keys', 'paste-buffer', 'send-keys', 'capture-pane', 'delete-buffer'])
            self.assertIn('-p', call.call_args_list[3].args)
            self.assertEqual(call.call_args_list[2].args[-1], 'C-u')
            self.assertEqual(call.call_args_list[4].args[-1], 'Enter')

    def test_controls_and_ambiguous_pane_names_are_rejected(self):
        for pane in ['session', '%3;command', '-t']:
            with self.assertRaises(ValueError):
                frontends.pane_info(pane)
        with mock.patch.object(frontends, 'pane_info'), mock.patch.object(frontends, 'tmux') as call:
            with self.assertRaises(ValueError):
                frontends.deliver_tmux_session_turn('%3', 'hello\x1b[31m')
            call.assert_not_called()

    def test_excluded_or_missing_bindings_are_not_live(self):
        with mock.patch.object(frontends, '_registry', return_value={THREAD: {'terminal_name': '%3'}}), mock.patch.object(
            frontends, 'is_session_excluded', return_value=True
        ), mock.patch.object(frontends, 'verify_record') as verify:
            self.assertEqual(frontends.active_session_terminals(), {})
            verify.assert_not_called()
