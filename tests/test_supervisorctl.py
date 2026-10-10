from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/supervisorctl.py"
THREAD_ID = "00000000-0000-4000-8000-000000000001"
TASK = "示例评估任务"
BINDING = {
    "codex_pid": 1201,
    "shell_pid": 1200,
    "tty": "pts/91",
    "backend": "terminal",
    "terminal_name": "pts/91",
    "token": "binding-token",
    "binding_source": "automatic",
}


def load_module():
    spec = importlib.util.spec_from_file_location("codex_thread_supervisor_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def setUp(self):
        # Controller tests isolate notification delivery; real outbox behavior
        # has its own filesystem-backed integration coverage.
        patch = mock.patch.object(self.module, "queue_report")
        patch.start()
        self.addCleanup(patch.stop)

    def write_transcript(self, root: Path, events: list[dict]) -> Path:
        path = root / "rollout.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return path

    def state(self, root: Path, **overrides) -> dict:
        payload = {
            "version": 4,
            "thread_id": THREAD_ID,
            "instance_id": "instance-test",
            "active": True,
            "interval_seconds": 900,
            "task": TASK,
            "codex_home": str(root / "codex"),
            "frontend_binding": dict(BINDING),
            "awaiting_nonce": "",
            "activity_turn_active": False,
            "activity_turn_id": "",
            "activity_generation": 0,
            "next_trigger_at": 0,
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def nonce_events(nonce: str, *, completed: str | None = None) -> list[dict]:
        turn_id = "turn-supervision"
        events = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"[TLS_SUPERVISION_NONCE:{nonce}]"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                },
            },
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
        ]
        if completed is not None:
            events.append(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": completed},
                }
            )
        return events

    def test_interval_bounds(self) -> None:
        self.assertEqual(self.module.parse_interval("15min"), 900)
        self.assertEqual(self.module.parse_interval("30m"), 1800)
        self.assertEqual(self.module.parse_interval("1h"), 3600)
        for invalid in ("1min", "4m", "8d", "soon"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.module.parse_interval(invalid)

    def test_cli_default_and_explicit_interval(self) -> None:
        for options, seconds in (([], 900), (["--interval", "30m"], 1800)):
            with self.subTest(options=options), mock.patch.object(
                sys, "argv", ["supervisorctl.py", "start", "--thread", THREAD_ID, "--task", TASK, *options]
            ), mock.patch.object(self.module, "start_supervisor", return_value={}) as start, mock.patch.object(
                self.module, "public_state", return_value={}
            ), mock.patch("builtins.print"):
                self.assertEqual(self.module.main(), 0)
                self.assertEqual(start.call_args.args[1], seconds)

    def test_start_rejects_missing_original_frontend_without_writing_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "UNIT_ROOT", root / "units"
            ), mock.patch.object(self.module, "transcript_for", return_value=transcript), mock.patch.object(
                self.module, "frontend_for", return_value=None
            ), mock.patch.object(self.module, "run_systemctl") as systemctl:
                with self.assertRaisesRegex(RuntimeError, "original Codex frontend"):
                    self.module.start_supervisor(THREAD_ID, 900, root / "codex", TASK)
            systemctl.assert_not_called()
            self.assertFalse((root / "state" / f"{THREAD_ID}.json").exists())
            self.assertFalse((root / "units").exists())

    def test_start_rejects_live_frontend_outside_tls_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "UNIT_ROOT", root / "units"
            ), mock.patch.object(self.module, "transcript_for", return_value=transcript), mock.patch.object(
                self.module, "tls_monitors_thread", return_value=False
            ), mock.patch.object(
                self.module, "active_session_terminals", return_value={THREAD_ID: dict(BINDING)}
            ), mock.patch.object(self.module, "run_systemctl") as systemctl:
                with self.assertRaisesRegex(RuntimeError, "original Codex frontend"):
                    self.module.start_supervisor(THREAD_ID, 900, root / "codex", TASK)
            systemctl.assert_not_called()
            self.assertFalse((root / "state" / f"{THREAD_ID}.json").exists())
            self.assertFalse((root / "units").exists())

    def test_start_persists_binding_and_writes_delayed_timer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "UNIT_ROOT", root / "units"
            ), mock.patch.object(self.module, "transcript_for", return_value=transcript), mock.patch.object(
                self.module, "frontend_for", return_value=dict(BINDING)
            ), mock.patch.object(self.module, "run_systemctl") as systemctl:
                state = self.module.start_supervisor(THREAD_ID, 900, root / "codex", TASK)
            service_name, timer_name = self.module.unit_names(THREAD_ID)
            guard_name = self.module.guard_unit_name(THREAD_ID)
            service = (root / "units" / service_name).read_text(encoding="utf-8")
            timer = (root / "units" / timer_name).read_text(encoding="utf-8")
            guard = (root / "units" / guard_name).read_text(encoding="utf-8")
            self.assertEqual(state["frontend_binding"], BINDING)
            self.assertEqual(state["task"], TASK)
            self.assertEqual(state["delivery_status"], "waiting")
            self.assertIn("run --thread " + THREAD_ID, service)
            self.assertNotIn("app-server", service)
            self.assertNotIn("codex queue", service)
            self.assertIn("OnActiveSec=900s", timer)
            self.assertIn("OnUnitActiveSec=900s", timer)
            self.assertIn("guard --thread " + THREAD_ID, guard)
            self.assertEqual(
                [call.args for call in systemctl.call_args_list],
                [("daemon-reload",), ("enable", timer_name), ("restart", timer_name), ("restart", guard_name)],
            )

    def test_terminal_delivery_uses_bound_cursor_bridge(self) -> None:
        runtime = {"socket_path": "/tmp/bridge.sock"}
        with mock.patch.object(self.module, "cursor_terminal_runtime", return_value=runtime) as resolve, mock.patch.object(
            self.module, "deliver_cursor_turn"
        ) as deliver:
            transport = self.module.deliver_to_frontend(dict(BINDING), "hello")
        resolve.assert_called_once_with(BINDING["shell_pid"], codex_pid=BINDING["codex_pid"])
        deliver.assert_called_once_with(runtime, "hello")
        self.assertEqual(transport, "cursor_terminal_bridge")

    def test_multiline_delivery_protects_newlines_and_nonce(self) -> None:
        runtime = {"socket_path": "/tmp/bridge.sock"}
        message = self.module.supervision_prompt("a" * 24, TASK)
        with mock.patch.object(self.module, "cursor_terminal_runtime", return_value=runtime), mock.patch.object(
            self.module, "deliver_cursor_turn"
        ) as deliver:
            self.module.deliver_to_frontend(dict(BINDING), message)
        sent = deliver.call_args.args[1]
        self.assertTrue(sent.startswith("\x1b[200~"))
        self.assertTrue(sent.endswith("\x1b[201~"))
        self.assertEqual(sent[6:-6], message)
        self.assertIn("[TLS_SUPERVISION_NONCE:" + "a" * 24 + "]", sent)

    def test_multiline_delivery_rejects_truncation(self) -> None:
        with mock.patch.object(self.module, "cursor_terminal_runtime", return_value={}), mock.patch.object(
            self.module, "deliver_cursor_turn"
        ) as deliver:
            with self.assertRaisesRegex(ValueError, "limit"):
                self.module.deliver_to_frontend(dict(BINDING), "a\n" + "x" * 4000)
        deliver.assert_not_called()

    def test_prompt_has_visible_task_and_timestamp_with_hidden_protocol(self) -> None:
        prompt = self.module.supervision_prompt("a" * 24, TASK, now=1788062400)
        visible = prompt.split("<!--", 1)[0]
        self.assertEqual(
            visible,
            f"继续监督完成{TASK}。\n时间：2026-08-30 12:00:00 CST\n",
        )
        self.assertNotIn("TLS_SUPERVISION", visible)
        self.assertNotIn("supervisorctl.py stop", visible)
        self.assertIn("TLS_SUPERVISION_NONCE:" + "a" * 24, prompt)
        self.assertIn("TLS_SUPERVISION_COMPLETE", prompt)
        self.assertIn("先停监督", prompt)
        self.assertNotIn(str(self.module.SCRIPT_PATH), prompt)
        self.assertNotIn(THREAD_ID, prompt)
        self.assertIn("TLS_SUPERVISION_STATE:", prompt)

    def test_peer_notification_is_hidden_and_does_not_ack(self):
        notice = {"updated": 100, "threads": {THREAD_ID: {"peer_id": "peer-a", "pending": 2}}}
        with mock.patch.object(Path, "read_text", return_value=json.dumps(notice)):
            prompt = self.module.supervision_prompt("a" * 24, TASK, now=100, state={"thread_id": THREAD_ID})
        self.assertNotIn("zen inbox", prompt.split("<!--", 1)[0])
        self.assertIn("zen inbox --peer-id peer-a --mark-read --json", prompt)
        self.assertIn("处理后", prompt)
        self.assertNotIn(THREAD_ID, prompt)

    def test_stale_or_invalid_peer_notification_is_ignored(self):
        for updated, identity in ((0, "peer-a"), (100, "peer;command")):
            notice = {"updated": updated, "threads": {THREAD_ID: {"peer_id": identity, "pending": 2}}}
            with mock.patch.object(Path, "read_text", return_value=json.dumps(notice)):
                self.assertEqual(self.module.peer_notification_direction({"thread_id": THREAD_ID}, now=200), "")

    def test_agent_mail_reminder_is_hidden_and_read_only(self):
        notice = {"updated": 100, "threads": {THREAD_ID: {"peer_id": "example-peer", "pending": 2}}}
        with mock.patch.object(Path, "read_text", return_value=json.dumps(notice)):
            prompt = self.module.supervision_prompt("a" * 24, TASK, now=100, state={"thread_id": THREAD_ID})
        self.assertIn("zen-mail inbox --peer-id example-peer", prompt)
        self.assertNotIn("zen-mail", prompt.split("<!--", 1)[0])
        self.assertIn("处理后", prompt)

    def test_agent_mail_stale_invalid_or_empty_notice_is_ignored(self):
        for updated, peer, pending in [(0, "example-peer", 1), (200, "bad;cmd", 1), (200, "example-peer", 0)]:
            notice = {"updated": updated, "threads": {THREAD_ID: {"peer_id": peer, "pending": pending}}}
            with mock.patch.object(Path, "read_text", return_value=json.dumps(notice)):
                self.assertEqual(self.module.agent_mail_notification_direction({"thread_id": THREAD_ID}, 200), "")

    def test_progress_requires_structured_observation(self) -> None:
        parse = self.module.progress_report
        self.assertIsNone(parse("TLS_SUPERVISION_CONTINUE\nStill stuck"))
        self.assertIsNone(parse('TLS_SUPERVISION_STATE: {"status":"fault","fault":"","evidence":"x"}'))
        self.assertIsNone(parse('TLS_SUPERVISION_STATE: {"status":"unknown","fault":"x","evidence":"y"}'))
        self.assertEqual(
            parse('TLS_SUPERVISION_CONTINUE\nTLS_SUPERVISION_STATE: {"status":"fault","fault":"arc192 timeout","evidence":"retry 1 exhausted"}'),
            {"status": "fault", "fault": "arc192 timeout", "evidence": "retry 1 exhausted"},
        )

    def test_same_fault_and_evidence_escalates_after_second_report(self) -> None:
        state = {}
        report = 'TLS_SUPERVISION_CONTINUE\nTLS_SUPERVISION_STATE: {"status":"fault","fault":"arc192 timeout","evidence":"600s retry exhausted"}'
        self.module.record_progress(state, report)
        self.assertEqual(state["stall_count"], 0)
        self.module.record_progress(state, report)
        self.assertEqual(state["stall_count"], 1)
        prompt = self.module.supervision_prompt("a" * 24, TASK, state=state)
        self.assertIn("故障无进展", prompt)
        self.assertIn("诊断或修复并验证", prompt)
        self.module.record_progress(state, report.replace("600s retry exhausted", "timeout traced to transport"))
        self.assertEqual(state["stall_count"], 0)

    def test_gpu_capacity_wait_triples_base_without_compounding_or_stalling(self) -> None:
        state = {"interval_seconds": 300, "active": True}
        final = 'TLS_SUPERVISION_STATE: {"status":"fault","fault":"sparse_gpu_capacity","evidence":"no exclusive card"}'
        for _ in range(5):
            self.module.record_progress(state, final)
            self.assertEqual(state["interval_seconds"], 900)
            self.assertEqual(state["base_interval_seconds"], 300)
            self.assertEqual(state["stall_count"], 0)
            self.assertTrue(state["active"])
        self.module.record_progress(state, 'TLS_SUPERVISION_STATE: {"status":"running","fault":"none","evidence":"model PID GPU forward verified"}')
        self.assertEqual(state["interval_seconds"], 300)
        self.assertFalse(state["gpu_capacity_wait"])

    def test_interval_refresh_updates_real_timer_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, timer = self.module.unit_names(THREAD_ID)
            path = root / timer
            path.write_text('[Timer]\nOnActiveSec=300s\nOnUnitActiveSec=300s\n')
            with mock.patch.object(self.module, 'UNIT_ROOT', root), mock.patch.object(self.module, 'run_systemctl') as systemctl:
                self.module.refresh_timer_interval(THREAD_ID, 900)
                self.assertIn('OnActiveSec=900s', path.read_text())
                self.assertIn('OnUnitActiveSec=900s', path.read_text())
                systemctl.assert_any_call('restart', timer)

    def test_running_and_missing_reports_do_not_invent_stalled_fault(self) -> None:
        state = {}
        fault = 'TLS_SUPERVISION_STATE: {"status":"fault","fault":"arc192 timeout","evidence":"600s"}'
        self.module.record_progress(state, fault)
        self.module.record_progress(state, fault)
        self.module.record_progress(state, 'TLS_SUPERVISION_STATE: {"status":"running","fault":"arc192 timeout","evidence":"job 42 forward verified"}')
        self.assertEqual(state["stall_count"], 0)
        self.module.record_progress(state, "TLS_SUPERVISION_CONTINUE\nno structured report")
        self.assertEqual(state["stall_count"], 0)
        self.assertEqual(state["missing_progress_reports"], 1)
        self.assertIn("上轮缺状态", self.module.supervision_prompt("a" * 24, TASK, state=state))

    def test_completed_nonce_updates_stall_state_without_interrupting_active_turn(self) -> None:
        nonce = "e" * 24
        completed = 'TLS_SUPERVISION_CONTINUE\nTLS_SUPERVISION_STATE: {"status":"fault","fault":"arc192 timeout","evidence":"600s"}'
        events = self.nonce_events(nonce, completed=completed)
        events[-1]["timestamp"] = "2026-09-10T12:00:00Z"
        events.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "ordinary-active"}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver:
                self.module.write_state(THREAD_ID, self.state(root, awaiting_nonce=nonce, last_fault="arc192 timeout", last_evidence="600s"))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["stall_count"], 1)
            self.assertEqual(result["delivery_status"], "activity_busy")

    def test_repeated_fault_reports_retire_timer_without_another_delivery(self) -> None:
        nonce = "e" * 24
        completed = 'TLS_SUPERVISION_CONTINUE\nTLS_SUPERVISION_STATE: {"status":"fault","fault":"arc192 timeout","evidence":"600s"}'
        events = self.nonce_events(nonce, completed=completed)
        events[-1]["timestamp"] = "2026-09-10T12:00:00Z"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver, mock.patch.object(self.module, "run_systemctl") as systemctl:
                self.module.write_state(THREAD_ID, self.state(
                    root, awaiting_nonce=nonce, last_fault="arc192 timeout", last_evidence="600s", stall_count=2,
                ))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertFalse(result["active"])
            self.assertEqual(result["stop_reason"], "stalled_no_progress")
            self.assertEqual(systemctl.call_count, 2)

    def test_missing_reports_retire_after_second_completed_turn(self) -> None:
        nonce = "e" * 24
        events = self.nonce_events(nonce, completed="TLS_SUPERVISION_CONTINUE\nno report")
        events[-1]["timestamp"] = "2026-09-10T12:00:00Z"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver, mock.patch.object(self.module, "run_systemctl"):
                self.module.write_state(THREAD_ID, self.state(root, awaiting_nonce=nonce, missing_progress_reports=1))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["stop_reason"], "progress_report_missing")

    def test_generated_prompt_nonce_is_parseable_from_transcript(self) -> None:
        nonce = "a" * 24
        events = self.nonce_events(nonce)
        events[0]["payload"]["content"][0]["text"] = self.module.supervision_prompt(nonce, TASK)
        with tempfile.TemporaryDirectory() as temporary:
            transcript = self.write_transcript(Path(temporary), events)
            snapshot = self.module.thread_snapshot(transcript)
        self.assertEqual(snapshot["nonce_turns"][nonce], "turn-supervision")

    def test_snapshot_records_latest_completion_time(self) -> None:
        events = [
            {"type": "event_msg", "timestamp": "2026-09-10T12:00:00Z", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {"type": "event_msg", "timestamp": "2026-09-10T12:03:00Z", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"}},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            transcript = self.write_transcript(Path(temporary), events)
            snapshot = self.module.thread_snapshot(transcript)
        self.assertEqual(snapshot["last_completed_at"], 1789041780)
        self.assertFalse(snapshot["has_unverified_completion"])

    def test_task_identifier_is_normalized_and_rejects_comment_injection(self) -> None:
        self.assertEqual(self.module.validate_task("  EXP42   正式实验。 "), "EXP42 正式实验")
        with self.assertRaises(ValueError):
            self.module.validate_task("<!-- hidden -->")

    def test_tmux_delivery_uses_named_original_session(self) -> None:
        binding = dict(BINDING, backend="tmux", terminal_name="tls-test-session")
        with mock.patch.object(self.module, "deliver_tmux_session_turn") as deliver:
            transport = self.module.deliver_to_frontend(binding, "hello")
        deliver.assert_called_once_with("tls-test-session", "hello")
        self.assertEqual(transport, "tmux_frontend")

    def test_tmux_custom_socket_is_retained_in_binding_delivery_and_units(self):
        binding = dict(BINDING, backend="tmux", terminal_name="%3", tmux_socket="/tmp/custom-server/socket")
        with mock.patch.object(self.module, "tls_monitors_thread", return_value=True), mock.patch.object(
            self.module, "active_session_terminals", return_value={THREAD_ID: binding}
        ):
            self.assertEqual(self.module.frontend_for(THREAD_ID), binding)
        with mock.patch.object(self.module, "deliver_tmux_session_turn") as deliver:
            self.module.deliver_to_frontend(binding, "hello")
            deliver.assert_called_once_with("%3", "hello", socket=binding['tmux_socket'])
        self.assertFalse(self.module.binding_matches(binding, dict(binding, tmux_socket="/tmp/other")))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / 'state'), mock.patch.object(
                self.module, "UNIT_ROOT", root / 'units'
            ), mock.patch.object(self.module, "frontend_for", return_value=binding), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "run_systemctl"):
                self.module.start_supervisor(THREAD_ID, 900, root / 'codex', TASK)
            service, _ = self.module.unit_names(THREAD_ID)
            guard = self.module.guard_unit_name(THREAD_ID)
            for name in [service, guard]:
                self.assertIn('SUPERVISOR_TMUX_SOCKET=/tmp/custom-server/socket', (root / 'units' / name).read_text())

    def test_frontend_accepts_tmux_without_shell_pid(self) -> None:
        binding = dict(BINDING, backend="tmux", terminal_name="tls-test-session", shell_pid=0)
        with mock.patch.object(self.module, "is_session_excluded", return_value=False), mock.patch.object(
            self.module, "active_session_terminals", return_value={THREAD_ID: binding}
        ):
            self.assertEqual(self.module.frontend_for(THREAD_ID), binding)

    def test_frontend_rejects_terminal_without_shell_pid(self) -> None:
        binding = dict(BINDING, shell_pid=0)
        with mock.patch.object(self.module, "is_session_excluded", return_value=False), mock.patch.object(
            self.module, "active_session_terminals", return_value={THREAD_ID: binding}
        ):
            self.assertIsNone(self.module.frontend_for(THREAD_ID))

    def test_frontend_rejects_live_session_outside_tls_policy(self) -> None:
        with mock.patch.object(self.module, "tls_monitors_thread", return_value=False), mock.patch.object(
            self.module, "active_session_terminals", return_value={THREAD_ID: dict(BINDING)}
        ) as active:
            self.assertIsNone(self.module.frontend_for(THREAD_ID))
        active.assert_not_called()

    def test_frontend_uses_live_watcher_policy_when_caller_lacks_environment(self) -> None:
        with mock.patch.object(self.module, "is_session_excluded", return_value=True), mock.patch.object(
            self.module, "tls_watcher_environment", return_value={"TLS_MONITOR_ALL_SESSIONS": "1"}
        ), mock.patch.object(
            self.module, "active_session_terminals", return_value={THREAD_ID: dict(BINDING)}
        ):
            self.assertEqual(self.module.frontend_for(THREAD_ID), BINDING)

    def test_live_watcher_policy_still_honors_explicit_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            excluded = Path(temporary) / "excluded-sessions"
            excluded.write_text(THREAD_ID + "\n", encoding="utf-8")
            environment = {
                "TLS_MONITOR_ALL_SESSIONS": "1",
                "TLS_EXCLUDED_SESSIONS_FILE": str(excluded),
            }
            self.assertFalse(self.module.policy_monitors_thread(THREAD_ID, environment))

    def test_missing_live_watcher_does_not_bypass_policy(self) -> None:
        with mock.patch.object(self.module, "is_session_excluded", return_value=True), mock.patch.object(
            self.module, "tls_watcher_environment", return_value={}
        ):
            self.assertFalse(self.module.tls_monitors_thread(THREAD_ID))

    def test_confirmation_requires_nonce_and_unchanged_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, self.nonce_events("a" * 24))
            with mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)):
                turn_id = self.module.confirm_visible_delivery(THREAD_ID, transcript, dict(BINDING), "a" * 24, timeout=0)
            self.assertEqual(turn_id, "turn-supervision")

    def test_confirmation_rejects_frontend_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, self.nonce_events("a" * 24))
            changed = dict(BINDING, token="different")
            with mock.patch.object(self.module, "frontend_for", return_value=changed), self.assertRaisesRegex(
                RuntimeError, "frontend changed"
            ):
                self.module.confirm_visible_delivery(THREAD_ID, transcript, dict(BINDING), "a" * 24, timeout=0)

    def test_confirmation_retries_enter_once_before_accepting_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = self.write_transcript(Path(temporary), [])
            missing = {"nonce_turns": {}, "completions": {}}
            accepted = {"nonce_turns": {"a" * 24: "turn-retried"}, "completions": {}}
            retry = mock.Mock()
            with mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "thread_snapshot", side_effect=[missing, accepted]
            ):
                turn_id = self.module.confirm_visible_delivery(
                    THREAD_ID,
                    transcript,
                    dict(BINDING),
                    "a" * 24,
                    timeout=0,
                    submit_retry=retry,
                )
        retry.assert_called_once_with()
        self.assertEqual(turn_id, "turn-retried")

    def test_confirmation_allows_delayed_transcript_without_resubmit(self) -> None:
        missing = {"nonce_turns": {}, "completions": {}}
        accepted = {"nonce_turns": {"a" * 24: "turn-delayed"}, "completions": {}}
        retry = mock.Mock()
        with mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
            self.module, "thread_snapshot", side_effect=[missing, missing, accepted]
        ), mock.patch.object(self.module.time, "monotonic", side_effect=[0, 61, 156]), mock.patch.object(
            self.module.time, "sleep"
        ):
            turn_id = self.module.confirm_visible_delivery(
                THREAD_ID, Path("unused"), dict(BINDING), "a" * 24, submit_retry=retry
            )
        self.assertEqual(turn_id, "turn-delayed")
        retry.assert_not_called()

    def test_confirmation_retries_enter_only_once_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transcript = self.write_transcript(Path(temporary), [])
            retry = mock.Mock()
            with mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "thread_snapshot", return_value={"nonce_turns": {}, "completions": {}}
            ), self.assertRaisesRegex(RuntimeError, "did not record"):
                self.module.confirm_visible_delivery(
                    THREAD_ID,
                    transcript,
                    dict(BINDING),
                    "a" * 24,
                    timeout=0,
                    submit_retry=retry,
                )
        retry.assert_called_once_with()

    def test_run_submits_once_and_records_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend", return_value="cursor_terminal_bridge"
            ) as deliver, mock.patch.object(
                self.module, "confirm_visible_delivery", return_value="turn-2"
            ) as confirm, mock.patch.object(self.module.secrets, "token_hex", return_value="b" * 24):
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_called_once()
            self.assertEqual(confirm.call_args.kwargs["timeout"], 15.0)
            self.assertIn("TLS_SUPERVISION_NONCE:" + "b" * 24, deliver.call_args.args[1])
            self.assertIn("先停监督", deliver.call_args.args[1])
            self.assertNotIn(THREAD_ID, deliver.call_args.args[1])
            self.assertTrue(deliver.call_args.args[1].startswith(f"继续监督完成{TASK}。"))
            self.assertRegex(deliver.call_args.args[1], r"\n时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \S+\n")
            self.assertEqual(result["awaiting_nonce"], "b" * 24)
            self.assertEqual(result["delivery_status"], "visible_submitted")
            self.assertEqual(result["delivery_turn"], "turn-2")

    def test_run_does_not_stack_while_previous_turn_is_in_flight(self) -> None:
        nonce = "c" * 24
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, self.nonce_events(nonce))
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver:
                self.module.write_state(THREAD_ID, self.state(root, awaiting_nonce=nonce))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["delivery_status"], "awaiting_completion")

    def test_run_does_not_interrupt_an_ordinary_active_turn(self) -> None:
        events = [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-user"}}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver:
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["delivery_status"], "activity_busy")

    def test_run_waits_full_interval_after_latest_completed_turn(self) -> None:
        events = [
            {"type": "event_msg", "timestamp": "2026-09-10T12:00:00Z", "payload": {"type": "task_complete", "turn_id": "turn-user", "last_agent_message": "done"}}
        ]
        now = 1789042200
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver, mock.patch.object(self.module.time, "time", return_value=now):
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["delivery_status"], "waiting_after_activity")
            self.assertEqual(result["next_trigger_at"], 1789042500)

    def test_defer_supervision_restarts_timer_from_turn_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "run_systemctl"
            ) as systemctl:
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.defer_supervision(THREAD_ID, completed_at=1000)
            self.assertEqual(result["next_trigger_at"], 1900)
            self.assertEqual(result["delivery_status"], "waiting_after_activity")
            systemctl.assert_called_once_with("restart", self.module.unit_names(THREAD_ID)[1])

    def test_old_stop_cannot_clear_a_newer_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "run_systemctl"
            ):
                self.module.write_state(
                    THREAD_ID,
                    self.state(root, activity_turn_active=True, activity_turn_id="turn-new"),
                )
                result = self.module.defer_supervision(
                    THREAD_ID,
                    completed_at=1000,
                    completed_turn_id="turn-old",
                )
            self.assertTrue(result["activity_turn_active"])
            self.assertEqual(result["activity_turn_id"], "turn-new")

    def test_prompt_submission_moves_deadline_before_turn_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"):
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.note_prompt_submitted(THREAD_ID, submitted_at=1000)
            self.assertEqual(result["next_trigger_at"], 1900)
            self.assertEqual(result["last_prompt_submitted_at"], 1000)

    def test_old_state_schema_never_delivers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver:
                self.module.write_state(THREAD_ID, self.state(root, version=3))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["delivery_status"], "schema_unverified")

    def test_prompt_activity_fully_migrates_old_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_state = self.state(root, version=3)
            old_state.pop("instance_id")
            old_state.pop("activity_generation")
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"):
                self.module.write_state(THREAD_ID, old_state)
                result = self.module.note_prompt_submitted(THREAD_ID, submitted_at=1000)
            self.assertEqual(result["version"], 4)
            self.assertRegex(result["instance_id"], r"^[0-9a-f]{24}$")
            self.assertEqual(result["activity_generation"], 1)

    def test_commit_cancels_reserved_delivery_after_new_prompt(self) -> None:
        nonce = "a" * 24
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"):
                self.module.write_state(
                    THREAD_ID,
                    self.state(
                        root,
                        delivery_status="delivery_reserved",
                        reservation_nonce=nonce,
                        reservation_generation=0,
                        activity_generation=1,
                    ),
                )
                result = self.module.commit_supervision(THREAD_ID, nonce)
            self.assertEqual(result["delivery_status"], "waiting_after_activity")
            self.assertEqual(result["reservation_nonce"], "")

    def test_old_guard_cannot_retire_replacement_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "run_systemctl"
            ) as systemctl:
                self.module.write_state(THREAD_ID, self.state(root, instance_id="instance-new"))
                result = self.module.retire_if_current(
                    THREAD_ID,
                    "instance-old",
                    "frontend_exited",
                    stop_guard=False,
                )
            self.assertTrue(result["active"])
            self.assertEqual(result["instance_id"], "instance-new")
            systemctl.assert_not_called()

    def test_run_does_not_stack_after_intermediate_task_completion(self) -> None:
        nonce = "a" * 24
        events = self.nonce_events(nonce, completed="DeepSeek v5 is still running")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver:
                self.module.write_state(THREAD_ID, self.state(root, awaiting_nonce=nonce))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["awaiting_nonce"], nonce)
            self.assertEqual(result["delivery_status"], "awaiting_completion")
            self.assertEqual(result.get("last_completed_nonce", ""), "")

    def test_continue_completion_without_timestamp_fails_closed(self) -> None:
        nonce = "d" * 24
        events = self.nonce_events(nonce, completed="TLS_SUPERVISION_CONTINUE\nworking")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver:
                self.module.write_state(THREAD_ID, self.state(root, awaiting_nonce=nonce))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertEqual(result["last_marker"], "TLS_SUPERVISION_CONTINUE")
            self.assertEqual(result["awaiting_nonce"], "")
            self.assertEqual(result["delivery_status"], "timing_unverified")

    def test_mark_started_blocks_delivery_before_transcript_task_started(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver:
                self.module.write_state(THREAD_ID, self.state(root))
                self.module.mark_turn_started(THREAD_ID, "turn-user", started_at=1000)
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertTrue(result["activity_turn_active"])
            self.assertEqual(result["delivery_status"], "activity_busy")

    def test_complete_marker_retires_without_sending(self) -> None:
        nonce = "f" * 24
        events = self.nonce_events(nonce, completed="TLS_SUPERVISION_COMPLETE\ndone")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, events)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver, mock.patch.object(self.module, "run_systemctl") as systemctl:
                self.module.write_state(THREAD_ID, self.state(root, awaiting_nonce=nonce))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertFalse(result["active"])
            self.assertEqual(result["stop_reason"], "completed")
            self.assertEqual(
                [call.args for call in systemctl.call_args_list],
                [
                    ("disable", "--now", self.module.unit_names(THREAD_ID)[1]),
                    ("stop", self.module.guard_unit_name(THREAD_ID)),
                ],
            )

    def test_binding_loss_retires_without_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=None), mock.patch.object(
                self.module, "deliver_to_frontend"
            ) as deliver, mock.patch.object(self.module, "run_systemctl") as systemctl:
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.run_supervision(THREAD_ID)
            deliver.assert_not_called()
            self.assertFalse(result["active"])
            self.assertEqual(result["stop_reason"], "window_unavailable")
            self.assertEqual(systemctl.call_count, 2)

    def test_delivery_failure_retires_instead_of_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = self.write_transcript(root, [])
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "transcript_for", return_value=transcript
            ), mock.patch.object(self.module, "frontend_for", return_value=dict(BINDING)), mock.patch.object(
                self.module, "deliver_to_frontend", side_effect=RuntimeError("bridge unavailable")
            ), mock.patch.object(self.module, "run_systemctl") as systemctl:
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.run_supervision(THREAD_ID)
            self.assertFalse(result["active"])
            self.assertEqual(result["stop_reason"], "delivery_failed")
            self.assertEqual(result["delivery_status"], "delivery_failed")
            self.assertEqual(systemctl.call_count, 2)

    def test_guard_retires_immediately_when_bound_pid_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "frontend_for", return_value=dict(BINDING)
            ), mock.patch.object(self.module.os, "pidfd_open", side_effect=ProcessLookupError), mock.patch.object(
                self.module, "run_systemctl"
            ) as systemctl:
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.guard_supervision(THREAD_ID)
            self.assertFalse(result["active"])
            self.assertEqual(result["stop_reason"], "frontend_exited")
            systemctl.assert_called_once_with(
                "disable", "--now", self.module.unit_names(THREAD_ID)[1], check=False
            )

    def test_guard_waits_for_pid_exit_without_polling_loop(self) -> None:
        watcher = mock.Mock()
        watcher.poll.return_value = [(99, self.module.select.POLLIN)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, "STATE_ROOT", root / "state"), mock.patch.object(
                self.module, "frontend_for", return_value=dict(BINDING)
            ), mock.patch.object(self.module.os, "pidfd_open", return_value=99), mock.patch.object(
                self.module.os, "close"
            ) as close, mock.patch.object(self.module.select, "poll", return_value=watcher), mock.patch.object(
                self.module, "run_systemctl"
            ) as systemctl:
                self.module.write_state(THREAD_ID, self.state(root))
                result = self.module.guard_supervision(THREAD_ID)
            watcher.register.assert_called_once_with(99, self.module.select.POLLIN)
            watcher.poll.assert_called_once_with()
            close.assert_called_once_with(99)
            self.assertFalse(result["active"])
            self.assertEqual(result["stop_reason"], "frontend_exited")
            systemctl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
