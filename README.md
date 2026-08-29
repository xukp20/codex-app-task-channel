<h1 align="center">Codex App Task Channel</h1>

<p align="center">
  <strong>A fallback task creation and messaging channel for the Codex desktop app.</strong>
</p>

<p align="center">
  <a href="skills/codex-app-task-channel/SKILL.md">
    <img alt="Codex Skill" src="https://img.shields.io/badge/Codex-Skill-2563eb?style=flat-square">
  </a>
  <a href="https://www.python.org/">
    <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-172554?style=flat-square">
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
  <a href="skills/codex-app-task-channel/SKILL.md">Skill Reference</a>
</p>

Codex App Task Channel provides the `codex-app-task-channel` skill. It is a
backup for creating App-visible Codex tasks and communicating with them when
the normal built-in task tools are absent or explicitly report that their
dynamic handler is unavailable.

The built-in task tools remain the default. This repository does not replace
them, and it does not start a separate app-server daemon. Its helper connects
to the Unix socket owned by the running Codex desktop app, so tasks remain
visible in the app sidebar and share the app's task state.

## Why This Exists

Some Codex builds can advertise task tools such as `create_thread` or
`send_message_to_thread` while the dynamic handler rejects the actual call.
The CLI still exposes `codex agents` and `codex queue`, and the desktop app's
own App Server supports the underlying thread and turn protocol.

This skill packages a narrow fallback around that protocol. It preserves task
visibility and history in the App while exposing explicit lifecycle,
configuration, and delivery controls.

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
| Safe loaded-Session replacement | Explicit, fail-closed cold replacement for Session-only settings such as context window |
| Configuration receipts | Report requested and observed effective context windows and stable message/turn identifiers |
| Read-only inspection | Check endpoint health and compact task status without sending a message |
| Dispatch provenance | Wrap inter-task messages with a model-visible source task id |

The skill does not replace nested subagents, change global Codex configuration,
or maintain a hidden per-task configuration registry. Session-only overrides
must be supplied again when a later cold resume still needs them.

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
can be updated with `git pull --ff-only`.

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

For operation selection, command examples, resume behavior, and cold-replace
requirements, read the [Skill reference](skills/codex-app-task-channel/SKILL.md).

## Safety Boundaries

- Try built-in task tools first. Enter fallback only after tool discovery fails
  or an exact call returns an unavailable-handler error.
- A failed built-in mutation has uncertain outcome. Read/list task state before
  retrying through this channel, so a delayed success does not create a duplicate.
- The helper never auto-approves requests and does not broaden sandbox or
  approval settings. New tasks inherit normal Codex configuration unless the
  caller explicitly selects model, effort, or working directory.
- Connect to the desktop app's existing socket. Starting a separate app-server
  can create tasks that the desktop app does not own or immediately display.
- `followup` performs one race-aware retry with the same
  `clientUserMessageId`; it does not silently turn a failed steer into a queued
  message.

## Repository Layout

```text
codex-app-task-channel/
├── README.md
├── LICENSE
├── requirements.txt
├── tests/
└── skills/
    └── codex-app-task-channel/
        ├── SKILL.md
        ├── agents/openai.yaml
        ├── references/app-server-protocol.md
        └── scripts/task_channel.py
```

The root README is repository documentation. Only the directory under
`skills/` is linked into the Codex skills directory.

## Validation

```bash
python /path/to/skill-creator/scripts/quick_validate.py \
  skills/codex-app-task-channel
python -m unittest discover -s tests -v
python skills/codex-app-task-channel/scripts/task_channel.py doctor
```

The unit suite exercises JSON-RPC response routing, active-turn selection,
delegation envelopes, and delivery-mode behavior without mutating App state.
The `doctor` command performs an initialized, read-only App Server handshake.

## Compatibility

This is an experimental fallback built against the current Codex App Server
v2 thread/turn protocol. Run `doctor` after Codex upgrades. If built-in task
tools work, prefer them even when this helper also passes.
