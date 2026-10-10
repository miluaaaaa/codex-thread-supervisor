"""Real tmux transport smoke test with cat, never a live Codex conversation."""
import os
import secrets
import shutil
import subprocess
import time
import unittest
from unittest import mock

import frontends


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class TmuxTransportSmoke(unittest.TestCase):
    def test_isolated_real_tmux_receives_verified_buffer(self):
        socket = 'supervisor-test-' + secrets.token_hex(8)

        def isolated(*args, input=None):
            return subprocess.run(['tmux', '-L', socket, *args], input=input, check=True,
                                  capture_output=True, text=True, timeout=5).stdout

        try:
            isolated('new-session', '-d', '-s', 'transport-test', '/bin/cat')
            pane = isolated('list-panes', '-t', 'transport-test', '-F', '#{pane_id}').strip()
            text = 'supervisor-transport-' + secrets.token_hex(8)
            actual_socket = isolated('display-message', '-p', '-t', pane, '#{socket_path}').strip()
            with mock.patch.dict(os.environ, {}, clear=True):
                # Emulate the fresh systemd environment, without an inherited TMUX.
                frontends.deliver_tmux_session_turn(pane, text, socket=actual_socket)
            deadline = time.monotonic() + 2
            while True:
                captured = isolated('capture-pane', '-p', '-t', pane)
                if text in captured:
                    break
                if time.monotonic() > deadline:
                    self.fail('named-buffer paste was not visible in the isolated pane')
                time.sleep(0.02)
            self.assertEqual(isolated('list-buffers').strip(), '')
        finally:
            subprocess.run(['tmux', '-L', socket, 'kill-server'], capture_output=True, timeout=5)
