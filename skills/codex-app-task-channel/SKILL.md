---
name: codex-app-task-channel
description: Create, fork, inspect, or message Codex desktop tasks through the App-owned App Server when built-in task tools are missing or explicitly unavailable. Use only as a fallback for App-visible task coordination; keep built-in task tools as the default and do not use this for nested subagents.
---

# Codex App Task Channel

Use the normal task tools first: `create_thread`, `fork_thread`, `read_thread`,
`wait_threads`, and `send_message_to_thread`. Enter this fallback only when the
needed tool is absent or its exact call explicitly reports that the handler is
unavailable.

If a built-in mutation returned an error, first read or list tasks when
possible. Treat its outcome as uncertain until verification proves that no task
or message was created; do not blindly retry and create duplicates.

## Preflight

Run the helper relative to this Skill directory:

```bash
python scripts/task_channel.py doctor
```

It must reach the desktop app's existing Unix socket. Do not launch a separate
`codex app-server` as a substitute. Read
[references/app-server-protocol.md](references/app-server-protocol.md) when
selecting a delivery mode, diagnosing protocol drift, or scripting a fork.

## Create or fork a task

Use `create` for a fresh task and `fork` when conversation lineage is required.
Always provide a user-readable `--title`, the intended `--cwd`, and any
explicitly requested `--model`/`--effort`. Omit configuration overrides that
the user did not request.

After success:

1. verify the JSON receipt and, when needed, read the returned task;
2. retain `threadId`, `turnId`, and `clientUserMessageId` in the execution or
   coordination receipt;
3. emit `::created-thread{threadId="..."}` so the app surfaces the task.

## Select message delivery deliberately

- `followup`: active task → `turn/steer`; idle task → `turn/start`. Prefer this
  for ordinary coordination when either state is acceptable.
- `steer`: require an active turn and append to that same turn immediately.
- `start`: require an idle task and create a new turn.
- `queue`: invoke `codex queue`; the message is durable and starts only when
  the task becomes idle. Never describe queue delivery as steer.

`turn/steer` cannot change the active turn's model or reasoning effort. If a
configuration-only liveness message must enforce new settings, wait for idle
and use `start`, or create/fork a correctly configured task.

Use `--source-thread` for inter-task dispatches that need model-visible source
provenance. This produces a `codex_delegation` envelope; it does not recreate
private built-in telemetry.

## Boundaries

- This Skill manages sidebar tasks, not nested subagents.
- It does not auto-approve, broaden permissions, or infer authorization for a
  new task, external action, or destructive operation.
- The current implementation targets the local Unix socket. Run it on the host
  that owns the desktop app; do not silently connect to a different daemon.
- On ambiguous transport failure, use the receipt ID and task readback before
  retrying. Preserve one `clientUserMessageId` across the helper's race-aware
  `followup` retry.
- If protocol validation fails after a Codex upgrade, stop and prefer restored
  built-in tools until this fallback is updated.
