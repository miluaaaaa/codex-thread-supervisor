# Codex Thread Supervisor

[中文说明](README.zh-CN.md) · [MIT license](LICENSE)

Keep an existing Codex CLI conversation working through periodic, visible continuations in its **original tmux pane**. The controller never creates a hidden conversation or changes the model. Extracted from a privately deployed supervisor and adapted into a standalone implementation.

## Features

- Independent systemd user timers, from five minutes to seven days.
- A full quiet period after conversation activity; no stacking behind active turns.
- Exact conversation / process / pane binding, including PID start-time identity. A PID exit guard stops the timer without polling.
- A fresh nonce must appear in the original transcript after submission. Missing confirmation stops supervision.
- Structured progress reports, repeated-fault retirement, and non-compounding GPU-capacity backoff.
- A private, durable notification outbox: every **five completed supervision turns**, plus a final report. Duplicate completion events do not increment the counter.
- Optional Zen / Agent Mail notice integration through metadata files. Their tools are not bundled.

## Requirements

Linux with `/proc`, Python 3.10+, tmux, and a working `systemctl --user` session. Python dependencies are standard-library only. The existing Codex process must have its original `rollout-…-UUID.jsonl` transcript open; the transcript must contain `task_started`, `task_complete`, and user-message turn IDs. Unsupported transcript formats fail closed.

The public backend supports **local Codex CLI in tmux**. Private TLS session discovery, Cursor window bridges, Feishu credentials/delivery, existing transcripts, experiment state, and GPU schedulers are not included. Existing sessions outside tmux are not automatically migrated.

## Install

```sh
git clone https://github.com/miluaaaaa/codex-thread-supervisor.git
cd codex-thread-supervisor
./install.sh
export PATH="$HOME/.local/bin:$PATH"
```

The installer links the commands and, when absent, the Codex skill. It preserves an existing skill and refuses to overwrite unrelated commands. Keep the checkout in place. Installation never binds or starts a supervisor.

## Bind an existing conversation

From an operator terminal, identify the exact pane, Codex PID, and thread UUID belonging to the same existing conversation:

```sh
tmux list-panes -a -F '#{pane_id} #{pane_pid} #{pane_tty} #{pane_current_command}'
ps -t pts/REPLACE -o pid,comm,args
codex-supervisor-bind bind --thread UUID --pane %PANE --pid CODEX_PID
codex-supervisor-bind list
```

`UUID`, `%PANE`, `CODEX_PID`, and `pts/REPLACE` are placeholders. Binding validates the actual opened transcript, process owner, TTY, and pane ancestry. The actual tmux socket is persisted, including custom sockets. For binding from outside that server, set `SUPERVISOR_TMUX_SOCKET=/path/to/socket`; it is also captured in generated timer/guard service environments. It refuses wrong threads, remote clients, and duplicate pane registrations. Do not infer a thread from the newest file. Within a Codex tool shell, `CODEX_THREAD_ID` identifies only that current conversation.

```sh
codex-supervisor start --thread UUID --interval 15min --task "finish the specified evaluation"
codex-supervisor status --thread UUID
codex-supervisor list
codex-supervisor stop --thread UUID
```

For a nondefault transcript home, add `--codex-home /path/to/codex-home` to `start`. `--gpu-wait` starts at three times the base interval; `--fixed-interval` explicitly keeps the requested interval during capacity waits. GPU waiting never relaxes execution verification.

## Progress protocol

A continuation carries a nonce and requests a bounded progress check. Its reply starts with:

```text
TLS_SUPERVISION_CONTINUE
```

and ends with a checkable observation:

```text
TLS_SUPERVISION_STATE: {"status":"running","fault":"","evidence":"verified new output and real execution"}
```

Use `blocked` for a concrete external blocker, or `fault` with a stable fault ID. Four identical non-capacity faults or two missing/invalid reports stop the timer. For GPU work, scheduler allocation alone does not prove inference: verify the actual model PID has model-sized GPU memory and nonzero compute on its allocated GPU.

When verified complete, stop supervision in that conversation before replying with `TLS_SUPERVISION_COMPLETE`. Retirement never establishes scientific validity.

## Activity integration and delivery limits

The controller reads completed-turn timestamps and active turns from the transcript. Optional frontend integrations should additionally call:

```sh
codex-supervisor activity --thread UUID --event prompt
codex-supervisor activity --thread UUID --event started --turn-id TURN_ID
codex-supervisor activity --thread UUID --event completed --turn-id TURN_ID
```

These are adapter entry points, **not a claim that all Codex versions expose a particular native hook schema**. Prompt/start callbacks provide earlier race protection. The completed callback collects the exact nonce-bound reply even after an explicit stop. Without callbacks, collection happens on the next active timer tick; an explicit stop before the final reply needs a completed callback to enqueue that final report. Input can still race between the last transcript check and terminal paste; avoid composing unsent drafts in a supervised pane because the safe handoff clears the composer.

## Notifications

Admitted reports are written to `~/.local/state/codex-supervisor/outbox/*.json`, with directories `0700` and files `0600`. Each contains `event_id`, `thread_id`, `instance_id`, `task`, and `answer`.

Your transport adapter can deliver these to Feishu, Slack, email, or another destination. Deduplicate by `event_id`; remove/archive a record **only after successful delivery** and retain failures for retry. Outbox acceptance is not chat delivery. `scripts/supervision_notification_policy.py` also exports the admission/delivery policy used by the original TLS integration. Notifications are counted per supervisor instance, not globally or by wall-clock time.

Only actual completion markers in answers end an episode. A reminder explaining “write TLS_SUPERVISION_COMPLETE when finished” does not mute future progress.

## Configuration

| Variable | Purpose |
|---|---|
| `SUPERVISOR_TMUX_SOCKET` | Optional socket used while registering a pane; the binding persists the actual socket |
| `SUPERVISOR_CONFIG_DIR` | Explicit bindings and allow/exclude lists; default `~/.config/codex-supervisor` |
| `SUPERVISOR_STATE_DIR` | Supervisor state; default `~/.local/state/codex-supervisor` |
| `SUPERVISOR_UNIT_DIR` | systemd user unit directory |
| `SUPERVISOR_OUTBOX_DIR` | Optional alternate notification outbox |
| `TLS_SUPERVISOR_NOTIFY_DIR` | Optional alternate notification-counter storage |
| `SUPERVISOR_PEER_NOTICES` | Optional Zen peer-notification metadata file |
| `SUPERVISOR_MAIL_NOTICES` | Optional Agent Mail notification metadata file |

Nondefault settings should be available in the systemd user environment as well as the invoking shell (`systemctl --user import-environment VARIABLE_NAME`). No credentials are required by this repository.

## Verification

```sh
TZ=Asia/Shanghai PYTHONPATH=scripts python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
```

Tests cover controller state transitions, nonce confirmation, busy-turn gates, PID/pane identity, fault retirement, five-cycle notification admission, durable outbox retries, and a real isolated tmux transport smoke test using `cat`. The smoke test does not run a model or submit a turn to your live conversations. Private production deployment is not a substitute for verifying this extracted backend against your installed Codex transcript format.

## Adaptive GPU task supervision

```sh
codex-supervisor tune --thread UUID --mode adaptive
codex-supervisor observe --thread UUID --metrics-file task-gpu.json
codex-supervisor tune --thread UUID --mode base
```

Only existing active supervisors can be tuned; the original frontend, instance,
nonce and busy-turn guards remain intact. Adaptive mode explicitly overrides a
fixed interval and legacy 3x capacity backoff, without creating a new conversation.
Ordinary timers still accept 5 minutes–7 days. Adaptive timers choose 60–900 seconds:
ready work below its GPU target gets 60s, confirmed capacity shortage gets 180s,
and stable work uses `2 * 0.05 * job_runtime_seconds`, bounded by the next
completion horizon / 4. These are polling-delay heuristics, not a guarantee of
5% idle time or maximum hardware utilization. Active turns are never interrupted.

Task owners append a `gpu` object to their normal progress JSON or send it through
`observe` immediately after a dispatch/completion/failure event:

```json
{"observed_at": 0, "target_gpus": 8, "verified_gpus": 6, "ready_lanes": 2,
 "capacity": "available", "evidence": "path to model PID/memory/compute evidence",
 "job_runtime_seconds": 180, "next_completion_seconds": 60}
```

Replace `observed_at` with the current Unix timestamp and all counts with actual
owner observations. Reports older than 600 seconds, future timestamps, unordered
observations and nonfinite values are rejected. The evidence reference is owner
input; this module cannot independently prove execution and never authorizes a
GPU launch or result acceptance. Duplicate observations do not inflate counters.
Per-task `gpu_task_stats` integrates reported target/verified/shortfall GPU seconds
only between samples at most 600 seconds apart; longer gaps are unknown time.
These sampled counts are not continuous SM utilization or benchmark throughput.

A completion adapter must actively call `observe`; the module does not subscribe
to Slurm or install an HPC poller. Dispatchers should refill compatible, validated
work on job completion immediately, without waiting for a Codex supervision turn.
A shorter chat timer cannot repair missing runnable work, invalid adapters, model
CPU/I/O bottlenecks or a failed frontend. The public tmux backend and private TLS
backend remain separate; adding this feature does not migrate private sessions.
