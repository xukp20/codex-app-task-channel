---
name: codex-app-task-channel
description: Create, resume, fork, find, inspect, or message Codex desktop tasks through the App-owned App Server when built-in task tools are unavailable or cannot express an explicitly requested per-task session configuration such as context window. Use only for App-visible task coordination; keep built-in task tools as the default for ordinary operations and do not use this for nested subagents.
---

# Codex App Task Channel

Use the normal task tools first: `create_thread`, `fork_thread`, `read_thread`,
`wait_threads`, and `send_message_to_thread`. Enter this fallback only when the
needed tool is absent, its exact call reports that the handler is unavailable,
or its schema cannot carry an explicitly requested session setting such as
`model_context_window`.

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

## Choose the operation by intent

| Intent | Operation | Semantics |
| --- | --- | --- |
| Create independent work visible in the App | `create` | Create, name, and immediately start a new task |
| Branch completed history into a new task | `fork` | Copy history, name the child, and start its first new turn |
| Affect work that is running now | `send --mode steer` | Append input to the current in-flight turn |
| Send an ordinary state-aware follow-up | `send --mode followup` | Steer when active; start a new turn when idle |
| Start work on a known-idle task | `send --mode start` | Start a new turn immediately |
| Deliver only after the task becomes idle | `send --mode queue` | Store a durable later message through `codex queue` |
| Reopen or reconfigure an existing task | `resume` | Require idle/not-loaded state, resolve Session configuration, then start a new turn |
| Inspect without loading or messaging | `read` | Return compact task status and recent user/agent messages |
| Block until a task's turn ends | `wait` | Read-only; see "Wait for a task's turn" |
| Find a task by name, directory, or provider | `list` | List tasks across all model providers, newest first |

Do not treat `resume` as immediate messaging or as a durable queue. It works at
an idle Session boundary and rejects active tasks. If new input must affect the
current run, use `steer`; if it must wait durably for the current run to finish,
use `queue`; use `resume` when reopening or Session configuration is the reason
for the next turn.

## Find a task

App Server's `thread/list` returns only the current default provider's tasks
unless told otherwise, so tasks running on another provider look missing.
`list` asks for every provider, merges repeated rows, and sorts by update time:

```bash
python scripts/task_channel.py list --search 'Build Trace' --format text
python scripts/task_channel.py list --cwd /path/to/project
python scripts/task_channel.py list --provider krill-code --limit 10
```

`read`, `send`, and `resume` take `--name` instead of `--thread`, and `fork`
takes `--from-name` instead of `--from-thread`. An exact task name wins;
otherwise a case-insensitive substring must match exactly one task. An
ambiguous or missing name fails before any message is sent and lists the
candidates. The receipt reports the resolved task under `resolvedByName`. Use
`--thread` when names are duplicated or a mutation must target a known id.

## Create or fork a task

Use `create` for a fresh task and `fork` when conversation lineage is required.
Always provide a user-readable `--title`, the intended `--cwd`, and any
explicitly requested `--model`/`--effort`. Omit configuration overrides that
the user did not request.

`create` and `fork` accept `--service-tier`, `--context-window`, and
`--auto-compact-token-limit` in addition to model, effort, and cwd. The context
window is the nominal configuration value. The JSON receipt reports the
requested nominal window, the expected effective window from the local models
cache, the effective window observed in the new turn's `task_started` event,
and `contextWindowVerified`. Treat a false verification as a failed
configuration application and do not blindly resend the message.

```bash
python scripts/task_channel.py create \
  --title 'Coverage Monitor' \
  --cwd /path/to/project \
  --model gpt-5.6-sol \
  --effort high \
  --message 'Read the task package and report readiness.'

python scripts/task_channel.py fork \
  --from-thread SOURCE_THREAD_ID \
  --title 'Coverage Monitor / Reviewer' \
  --message 'Review the completed work.'
```

After success:

1. verify the JSON receipt and, when needed, read the returned task;
2. retain `threadId`, `turnId`, and `clientUserMessageId` in the execution or
   coordination receipt;
3. emit `::created-thread{threadId="..."}` so the app surfaces the task.

## Resume an existing task

Use `resume` only when an idle App-visible task must keep the same task id and
the next turn needs explicit configuration. Keep the ordinary path simple:

- `notLoaded`: resume directly with the requested configuration.
- loaded and idle, with only model/effort/service-tier/cwd changes: rejoin the
  existing Session and apply those fields on `turn/start`; no replacement.
- loaded and idle, with `--context-window` or
  `--auto-compact-token-limit`: reject unless the caller explicitly adds
  `--cold-replace`.
- active: reject; use ordinary followup/steer semantics instead.

The helper does not send an empty config map or attempt replacement during an
ordinary loaded-task resume.

```bash
python scripts/task_channel.py resume \
  --thread THREAD_ID \
  --context-window 526316 \
  --cold-replace \
  --effort high \
  --message 'Continue the verification.' \
  --source-thread SOURCE_THREAD_ID
```

`--cold-replace` is deliberately opt-in. It first records the active effort,
removes only this helper connection's subscription, and resumes with a distinct
supported effort as a replacement canary. If another client such as Desktop is
still subscribed, App Server preserves the loaded Session; the canary then
fails and the helper stops before sending the real message. On success, the
real turn restores the requested effort, or the prior/default effort when none
was requested. Context is verified after turn start through the matching
`task_started` event; inspect `contextWindowVerified` and the observed value.

Native persistence does not retain an arbitrary Session config map. For
configured tasks the helper saves references to their configuration sources;
see the next section. Ordinary Desktop messages and `send` remain native: they
do not reapply those references or rewrite global config. Use helper `resume`
to reload bound configuration.

## Provider and persistent session configuration

`create`, `fork`, and `resume` accept `--provider ID`, `--agent-config ROLE_ID`,
and `--config-file /absolute/session.toml`. Read the protocol reference for the
supported field subset and verification limits. Importing a role config here
configures the independent sidebar Session itself; it is not native subagent
role application, which may ignore provider, credential, and context settings.

```bash
python scripts/task_channel.py create \
  --title 'Vendor Luna check' --cwd /path/to/project \
  --agent-config vendor-luna --message 'Perform the bounded check.' --wait

# The binding is read automatically; no key or role argument needs repeating.
python scripts/task_channel.py resume --thread THREAD_ID \
  --message 'Continue the check.' --wait
```

Precedence: registered role settings, then explicit TOML file, then CLI settings.
Provider definitions are read locally and role-specific authentication overrides
are merged. Never put keys in the message or CLI arguments. Model/provider are
checked against the App Server response before sending work; a mismatch stops
without sending the task. Context verification remains post-start evidence.
A READY reply alone proves neither routing nor credential selection.

Bindings live in `$CODEX_HOME/task-channel/threads/THREAD_ID.json` (files 0600,
directory 0700). They contain role/file references, explicit non-secret settings
and socket identity, not resolved credentials. Resume re-reads sources, so key
rotation takes effect; missing references fail rather than falling back.
The receipt exposes `configBinding`, `modelProvider`, and `providerVerified`.
Binding is saved as soon as creation returns an ID, even if subsequent validation
fails; inspect that task instead of blindly retrying creation.

Bound resumes require a not-loaded Session or explicit `--cold-replace` when
already loaded. Another Desktop subscriber may prevent replacement; the existing
canary then rejects before sending work. Direct Desktop reopen does not invoke
this helper and cannot guarantee reapplication of custom keys/context. A binding
is local helper persistence, not a native permanent thread-config feature.

## Select message delivery deliberately

- `followup`: active task → `turn/steer`; idle task → `turn/start`. Prefer this
  for ordinary coordination when either state is acceptable.
- `steer`: require an active turn and append to that same turn immediately.
- `start`: require an idle task and create a new turn.
- `queue`: invoke `codex queue`; the message is durable and starts only when
  the task becomes idle. Never describe queue delivery as steer.

```bash
# State-aware ordinary delivery: active → steer, idle → start.
python scripts/task_channel.py send \
  --thread THREAD_ID --mode followup --message 'Continue with the next check.'

# Immediate same-turn intervention.
python scripts/task_channel.py send \
  --thread THREAD_ID --mode steer --message 'Stop after this tool call and reassess.'

# Durable later delivery, intentionally not same-turn intervention.
python scripts/task_channel.py send \
  --thread THREAD_ID --mode queue --message 'When idle, continue from GOAL.md.'
```

Use `--wait` when the caller needs delivery evidence rather than only an
accepted turn receipt. A completed wait result includes the target turn's
conversation messages and `lastAgentMessage`:

```bash
python scripts/task_channel.py send \
  --thread THREAD_ID --mode followup --wait --message 'Reply READY.'
```

## Wait for a task's turn

An orchestrating task can block its own turn on a sub-task with `wait`, or with
`send --wait` right after sending. `wait` is read-only: it never loads or
resumes the task and never starts a turn.

```bash
python scripts/task_channel.py wait --thread THREAD_ID --turn TURN_ID --timeout 7200
python scripts/task_channel.py wait --name 'Build Trace' --next --format text
```

- `--turn ID` waits for that turn; otherwise the running turn; with `--next`
  and none running, the next turn; with neither, the last result returns at
  once with `"alreadyIdle": true`. A `notLoaded` task has no running turn.
- `--timeout` (`--wait-timeout` on sending commands) defaults to 3600 seconds;
  `0` waits without a limit. A timeout leaves the other task running; the
  output names `threadId` and `turnId` to continue with `wait --turn`.
- Disconnects and App restarts after the first connection are retried with
  backoff until the deadline; an unreachable App at start, unknown targets, and
  protocol errors fail at once.
- Exit codes for `wait` and every `--wait`: 0 completed, 1 helper error,
  2 usage error, 3 turn failed, 4 turn interrupted, 5 wait timed out.
- The caller's own shell-command timeout must exceed the wait, or the caller
  cuts the command off first. Set it above `--timeout`, or use `--timeout 0`
  with a tool that allows an unlimited command.

`turn/steer` cannot change the active turn's model, reasoning effort, service
tier, or context window. Model, effort, and service tier can be selected on a
new `start`; context-window and auto-compaction changes require idle `resume`.

Use `--source-thread` for inter-task dispatches that need model-visible source
provenance. This produces a `codex_delegation` envelope; it does not recreate
private built-in telemetry.

## Read task content

`read` returns the latest turn by default, including user and agent messages
but excluding bulky tool and command items. Increase the page size with
`--turn-limit`; add `--include-items` only when raw tool-level evidence is
needed.

```bash
python scripts/task_channel.py read --thread THREAD_ID
python scripts/task_channel.py read --thread THREAD_ID --turn-limit 3
python scripts/task_channel.py read --thread THREAD_ID --include-items
python scripts/task_channel.py read --name 'Build Trace' --turn-limit 3 --format text
```

`--format text` prints the returned turns oldest first as a readable transcript
with full message text; JSON stays the default for scripts and receipts.

The helper reads content through `thread/turns/list` with `itemsView: "full"`.
Do not infer failed delivery from an empty higher-level task summary when this
native readback contains the user and agent messages.

## Boundaries

- This Skill manages sidebar tasks, not nested subagents.
- Do not use `resume` merely to send an ordinary message; prefer the built-in
  message tool or `send`. Use `--cold-replace` only when a loaded Session must
  adopt Session-only configuration or deliberately reload current config
  layers.
- It does not auto-approve, broaden permissions, or infer authorization for a
  new task, external action, or destructive operation.
- The current implementation targets the local Unix socket. Run it on the host
  that owns the desktop app; do not silently connect to a different daemon.
- On ambiguous transport failure, use the receipt ID and task readback before
  retrying. Preserve one `clientUserMessageId` across the helper's race-aware
  `followup` retry.
- When verifying a sent message, prefer `send --wait` or `read` and require the
  expected agent text in `lastAgentMessage` or `turns[].messages`.
- If protocol validation fails after a Codex upgrade, stop and prefer restored
  built-in tools until this fallback is updated.
- Agents outside the Codex app, such as Claude Code, use the optional
  `codex-app-task-channel-external` skill in this repository instead of this one.
