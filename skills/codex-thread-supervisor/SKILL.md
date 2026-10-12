---
name: codex-thread-supervisor
description: Start, inspect, change or stop periodic supervision of the current or explicitly named Codex CLI conversation. Use for requests such as 开启监督器，15min, 监督器状态 or 关闭监督器.
---

# Codex Thread Supervisor

Use `codex-supervisor`. Requires a Linux systemd user session and an explicitly registered original tmux pane. This standalone skill does not include private TLS, Cursor, or Feishu infrastructure.

For the current conversation use only `CODEX_THREAD_ID`; never guess a thread from the newest transcript. For another conversation require its explicitly specified UUID.

```sh
codex-supervisor start --interval 15min --task "specified task"
codex-supervisor status
codex-supervisor stop
codex-supervisor list
```

`start`, `status` and `stop` accept `--thread UUID`. Starting supervision is authorized only by an explicit request; do not start it merely because a task is long. A failed original-frontend binding is a hard stop. Do not create a new conversation or hidden app-server turn.

The first continuation occurs after a full interval, measured as a quiet period after actual conversation activity. Never interrupt or queue behind an active turn. Preserve the nonce in the submitted prompt. Do not create a Goal or enable GPU monitoring.

A continuation reply begins with `TLS_SUPERVISION_CONTINUE` and ends with one `TLS_SUPERVISION_STATE:` JSON object containing `status` (`running`, `blocked`, `fault`), `fault`, and observable `evidence`. Plans and idle allocations are not execution evidence. GPU work must have model-sized memory and nonzero GPU compute belonging to the same model PID on the allocated GPU; never fall back to CPU or treat allocation alone as a result.

After two identical non-capacity fault reports, diagnose or repair and verify one concrete outcome. The controller stops after four identical reports or two missing/invalid state reports. Capacity waiting uses exactly three times the base interval, without compounding; restore the base interval when actual work resumes. An explicit `--fixed-interval` overrides capacity backoff.

When the task is verified complete, stop the supervisor before replying with `TLS_SUPERVISION_COMPLETE`. A completion integration should call `codex-supervisor activity --event completed --turn-id TURN --thread UUID` after the reply; this preserves the final notification after an explicit stop. Without a completion integration, active supervisors collect replies on their next timer tick.

Notifications are durable outbox records: every five distinct completed supervision turns, plus a final report. Chat delivery needs a separate transport adapter. Do not fabricate a successful Feishu/webhook delivery from an outbox file.

For an explicit request to optimize GPU supervision, enable `tune --mode adaptive`
only on the requested existing supervisor(s). The automatic range is 60–900
seconds; ready work with a target shortfall uses 60 seconds, confirmed capacity
shortage 180 seconds. Stable work uses observed runtime and remaining duration.
This explicit mode replaces fixed cadence/legacy 3x backoff; `tune --mode base`
restores the original base and fixed setting. Include the documented `gpu` object
in progress reports, or call `observe --metrics-file` at task events. Never invent
GPU targets, verified counts, capacity or ETAs. No Slurm poller is installed.
