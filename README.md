<h1 align="center">Codex App Task Channel</h1>

<p align="center">
  <strong>English</strong> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <strong>A fallback task creation and messaging channel for the Codex desktop app.</strong>
</p>

<p align="center">
  <a href="skills/codex-app-task-channel/SKILL.md">
    <img alt="Codex Skill" src="https://img.shields.io/badge/Codex-Skill-2563eb?style=flat-square">
  </a>
  <a href="https://www.python.org/">
    <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-172554?style=flat-square">
  </a>
  <img alt="Transport" src="https://img.shields.io/badge/transport-App%20Server-0f8f88?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/status-experimental-d97706?style=flat-square">
</p>

<p align="center">
  <a href="#why-this-exists">Why</a>
  &middot;
  <a href="#capabilities">Capabilities</a>
  &middot;
  <a href="#delivery-modes">Delivery modes</a>
  &middot;
  <a href="#install">Install</a>
  &middot;
  <a href="#agents-outside-the-codex-app">External agents</a>
  &middot;
  <a href="skills/codex-app-task-channel/SKILL.md">Skill Reference</a>
</p>

Codex App Task Channel provides the `codex-app-task-channel` skill for two
related purposes:

1. **Compatibility fallback.** It replaces unavailable App task operations
   such as `create_thread` and `send_message_to_thread` when they are missing,
   or when their advertised dynamic handler rejects the actual call.
2. **App Server capability extension.** It exposes lower-level thread, turn,
   and Session controls that the built-in task tools may not offer. Most
   importantly, callers can select configuration per task instead of changing
   one global Codex default for every task.

Per-task controls include model, reasoning effort, service tier, working
directory, context window, and auto-compaction threshold. This makes it
possible, for example, to run a long-lived Sol monitoring task with one context
window while creating Luna workers or independent tasks with a different
window. Context settings are applied when creating/forking a task or at an
eligible resume boundary, and remain subject to the selected model's supported
maximum and effective-window adjustment.

The built-in task tools remain the default for ordinary operations. Use this
skill when those tools are unavailable or when they cannot express the required
per-task configuration. The repository does not start a separate app-server
daemon: its helper connects to the Unix socket owned by the running Codex
desktop app, so tasks remain visible in the App sidebar and share the App's
task state.

Agents that run outside the Codex app, such as Claude Code, can use the same
helper through the optional `codex-app-task-channel-external` skill; see
[Agents outside the Codex app](#agents-outside-the-codex-app).

## Why This Exists

This project addresses two practical gaps:

- **Availability gap:** some Codex builds advertise task tools such as
  `create_thread` or `send_message_to_thread`, but the dynamic handler is absent
  or rejects the call.
- **Configuration gap:** built-in task tools may support ordinary creation and
  messaging without exposing Session settings such as a task-specific context
  window or auto-compaction threshold.

The desktop App Server already provides the underlying thread/turn lifecycle
and configuration-override protocol. This skill packages those capabilities
into a guarded helper that preserves App visibility and history, supports
state-aware messaging, and verifies task-specific context configuration from
runtime telemetry.

The protocol and its thread/turn methods are documented in the
[official Codex App Server guide](https://developers.openai.com/codex/app-server).

## Capabilities

| Capability | What it provides |
| --- | --- |
| App-visible task creation | Create and name a new sidebar task, then start its first turn |
| History-aware forks | Branch completed task history into a separately named App task |
| State-aware messaging | Automatically steer an active turn or start an idle turn |
| Immediate intervention | Append user input to the currently running turn with steer |
| Durable deferred delivery | Queue a message that starts only after the task becomes idle |
| Existing-task resume | Reopen a not-loaded task or start the next configured turn on an idle task |
| Per-task configuration | Select model, reasoning effort, service tier, working directory, context window, and auto-compaction threshold where the protocol supports them |
| Provider and role configuration | Run a task on a chosen model provider, or import settings from a registered role or a TOML file; helper `resume` reloads them later |
| Safe loaded-Session replacement | Explicit, fail-closed cold replacement for Session-only settings such as context window |
| Configuration receipts | Report requested and observed effective context windows and stable message/turn identifiers |
| Read-only inspection | Check endpoint health, task status, and recent user/agent messages without sending a message |
| Task lookup | List tasks across all model providers and target a task by name instead of id |
| Readable transcripts | Print recent turns oldest first as text for a person or another agent |
| Dispatch provenance | Wrap inter-task messages with a model-visible source task id |

The skill does not replace nested subagents or change global Codex configuration.
Configured tasks keep local, secret-free references to their configuration
sources, which helper `resume` reloads. Reopening a task in Desktop or sending an
ordinary message does not reapply them.

## Delivery Modes

| Mode | Behavior | Use when |
| --- | --- | --- |
| `steer` | Appends input to the current active turn | The running Agent should see the message immediately |
| `start` | Starts a new turn on an idle task | The task is idle and should begin work now |
| `followup` | Steers when active, otherwise starts | You want the closest fallback to ordinary task messaging |
| `queue` | Stores a durable message; dispatch occurs when the task is idle | Delivery must survive an active turn or temporary disconnect |

`queue` is not steer. It intentionally does not alter the current active turn.
`steer` cannot change the model or reasoning effort of an already-running
turn; wait for idle and use `start` when a configuration change is required.

`resume` is not queue and is not same-turn intervention. It requires an idle or
not-loaded task, resolves Session configuration, and starts a new turn. Use
steer for immediate control of active work and queue for durable later work.

Add `--wait` to `send`, `create`, `fork`, or `resume` when the caller needs a
completed-turn receipt containing conversation messages and
`lastAgentMessage`, rather than only an accepted turn id.

## Wait for a Turn

`wait` is read-only: it never loads, resumes, or subscribes to a task and never
starts a turn. It blocks until a turn finishes and reports `threadId`, `turnId`,
`status`, `error`, and `lastAgentMessage`.

```bash
python skills/codex-app-task-channel/scripts/task_channel.py wait \
  --name 'Build Trace' --timeout 7200 --format text
python skills/codex-app-task-channel/scripts/task_channel.py wait \
  --thread THREAD_ID --turn TURN_ID
```

- `--turn ID` waits for that turn. Otherwise a running turn is awaited; with
  `--next` and no running turn, the next turn to start and finish is awaited;
  with neither, the last turn's result returns at once with `"alreadyIdle": true`.
  A `notLoaded` task counts as having no running turn.
- `--timeout` and `--wait-timeout` default to 3600 seconds; `0` waits without a
  limit. A timeout only ends the wait: the other task keeps running, and the
  output names its `threadId` and `turnId` so `wait --turn` can continue.
- A lost connection or an App restart is not the end of a turn. The helper
  reconnects with backoff (2 s growing to 30 s) until the deadline. Logic errors
  such as an unknown task or a protocol error fail immediately.
- Waiting polls turn status without items, backing off to 15 s and checking at
  once on a notification; full items are fetched once, after the turn finishes.
- `--format text` prints a short summary; `--max-chars` (default 2000) truncates
  the last agent message.

Exit codes of `wait` and of every `--wait`:

| Code | Meaning |
| --- | --- |
| 0 | Turn completed |
| 1 | Helper error (bad target, protocol error, unreachable App Server) |
| 2 | Usage error |
| 3 | Turn failed |
| 4 | Turn interrupted |
| 5 | Wait timed out; the other task is still running |

## Read Task Content

```bash
python skills/codex-app-task-channel/scripts/task_channel.py read \
  --thread THREAD_ID --turn-limit 3
python skills/codex-app-task-channel/scripts/task_channel.py list \
  --search 'Build Trace' --format text
python skills/codex-app-task-channel/scripts/task_channel.py read \
  --name 'Build Trace' --turn-limit 3 --format text
```

App Server lists only the current default provider's tasks unless asked
otherwise; `list` and name resolution include every provider. `read`, `send`,
and `resume` accept `--name`, and `fork` accepts `--from-name`. An ambiguous
name fails with the candidates before anything is sent.

The default response includes the latest turn's user and agent messages while
omitting bulky tool items. Add `--include-items` for the complete raw items.
The helper uses App Server's native `thread/turns/list` endpoint with
`itemsView: "full"`, so it can verify content even when a higher-level task
summary returns an empty turn shell.

## Install

```bash
git clone https://github.com/xukp20/codex-app-task-channel.git
cd codex-app-task-channel
python -m pip install 'websockets>=14,<17'
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PWD/skills/codex-app-task-channel" \
  "${CODEX_HOME:-$HOME/.codex}/skills/codex-app-task-channel"
```

Reload Codex after installation so the skill is discovered. A linked install
can be updated with `git pull --ff-only`. The helper requires Python 3.11+.

## Verify the App-owned endpoint

```bash
python skills/codex-app-task-channel/scripts/task_channel.py doctor
```

The default socket is:

```text
${CODEX_HOME:-$HOME/.codex}/app-server-control/app-server-control.sock
```

Override it with `CODEX_APP_SERVER_SOCKET` or `--socket`. The helper currently
supports the Unix socket used by the local desktop app.

For operation selection, command examples, resume behavior, provider and role
configuration, and cold-replace requirements, read the
[Skill reference](skills/codex-app-task-channel/SKILL.md).

## Agents outside the Codex app

Agents that run outside the Codex app, such as Claude Code, install the optional
`codex-app-task-channel-external` skill from the same repository instead of the
main one. Its `scripts/external_channel.py` reuses the main helper with the same
commands, and requires `--sender NAME` on every command that sends a message;
the message is then prefixed with a line naming the sender, so the receiving
task does not mistake it for the user or for another Codex task. Reading,
listing, and `wait` need no sender; run `wait` as a background command to be
notified when a task's turn ends.

```bash
git clone https://github.com/xukp20/codex-app-task-channel.git \
  ~/.claude/tool-sources/codex-app-task-channel
ln -s ../tool-sources/codex-app-task-channel/skills/codex-app-task-channel-external \
  ~/.claude/skills/codex-app-task-channel-external
```

Keeping the clone inside the agent's home lets a backup of that home stay
self-contained. Update with `git -C ~/.claude/tool-sources/codex-app-task-channel pull --ff-only`.

## Safety Boundaries

- Try built-in task tools first. Enter fallback only after tool discovery fails
  or an exact call returns an unavailable-handler error.
- A failed built-in mutation has uncertain outcome. Read/list task state before
  retrying through this channel, so a delayed success does not create a duplicate.
- The helper never auto-approves requests and does not broaden sandbox or
  approval settings. New tasks inherit normal Codex configuration unless the
  caller explicitly selects configuration.
- Connect to the desktop app's existing socket. Starting a separate app-server
  can create tasks that the desktop app does not own or immediately display.
- `followup` performs one race-aware retry with the same
  `clientUserMessageId`; it does not silently turn a failed steer into a queued
  message.

## Repository Layout

```text
codex-app-task-channel/
├── README.md
├── README.zh-CN.md
├── LICENSE
├── requirements.txt
├── tests/
└── skills/
    ├── codex-app-task-channel/
    │   ├── SKILL.md
    │   ├── agents/openai.yaml
    │   ├── references/app-server-protocol.md
    │   └── scripts/
    │       ├── task_channel.py
    │       └── session_profiles.py
    └── codex-app-task-channel-external/
        ├── SKILL.md
        └── scripts/external_channel.py
```

The root README is repository documentation. Codex links
`skills/codex-app-task-channel`; agents outside the Codex app link
`skills/codex-app-task-channel-external`.

## Validation

```bash
python /path/to/skill-creator/scripts/quick_validate.py \
  skills/codex-app-task-channel
python /path/to/skill-creator/scripts/quick_validate.py \
  skills/codex-app-task-channel-external
python -m unittest discover -s tests -v
python skills/codex-app-task-channel/scripts/task_channel.py doctor
```

The unit suite exercises JSON-RPC response routing, active-turn selection,
content readback, completed-turn receipts, delegation envelopes,
delivery-mode behavior, provider and role configuration, provider-wide
listing, name resolution, and the external sender wrapper without mutating
App state.
The `doctor` command performs an initialized, read-only App Server handshake.

## Compatibility

This is an experimental fallback built against the current Codex App Server
v2 thread/turn protocol. Run `doctor` after Codex upgrades. If built-in task
tools work, prefer them even when this helper also passes.
