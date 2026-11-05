---
name: codex-app-task-channel-external
description: Find, read, or message Codex desktop app tasks from an agent that runs outside the Codex app, such as Claude Code, through the app's local App Server. Use when the user asks to look up a Codex session, read its recent conversation, wait for its turn to finish, or deliver a message or review to it. Codex itself uses codex-app-task-channel instead.
---

# Codex App Task Channel for external agents

This optional extension of `codex-app-task-channel` serves agents that run
outside the Codex app. `scripts/external_channel.py` accepts the same commands
and options as the main helper and adds one rule: every command that sends a
message requires `--sender NAME`, and the message is prefixed with a line naming
the sender, so the receiving task does not mistake it for the user or for
another Codex task.

The Codex-only parts of the main skill do not apply here: there are no built-in
task tools to try first, and the `::created-thread` directive is for the Codex
app.

## Preflight

Run on the host that owns the Codex desktop app:

```bash
python scripts/external_channel.py doctor
```

## Find and read

Reading is read-only: it does not load the task or start a turn.

```bash
python scripts/external_channel.py list --search 'Build Trace' --format text
python scripts/external_channel.py list --cwd /path/to/project --format text
python scripts/external_channel.py read --name 'Build Trace' --turn-limit 3 --format text
```

`list` includes tasks on every model provider. `--name` takes an exact task
name, or a substring that matches exactly one task; an ambiguous name fails
with the candidates, and `--thread ID` then selects one. When reviewing a plan
document that a Codex task wrote, read that task's latest turns as well: the
discussion may have moved past the document.

## Send

Send only when the user asked for this delivery.

```bash
python scripts/external_channel.py send --name 'Build Trace' \
  --sender 'Claude Code' --mode followup --message 'Review notes: ...'
```

- `followup` steers an active task and starts a new turn on an idle one;
  `queue` delivers after the task becomes idle.
- For a task whose status is `notLoaded`, use `resume --name ... --sender ...
  --message ...` without configuration options.
- State in the message what the user asked for; the wrapper adds only who sent
  it. A message starts work in the other task under that task's own
  permissions and does not extend the user's authorization.
- Verify with a background `wait --turn ID` (below) or a later `read`. After an ambiguous failure, read the
  task before retrying so the message is not delivered twice.
- Use `create`, `fork`, `--cold-replace`, or provider and configuration options
  only when the user asks for them. Their semantics are in the main skill,
  `../codex-app-task-channel/SKILL.md` in the same checkout; if this skill
  directory is a link, resolve it with `realpath` first.

## Wait in the background

`wait` blocks until a task's turn ends. It is read-only and needs no
`--sender`. Run it as a background command (in Claude Code, Bash with
`run_in_background`) so that its exit notifies you instead of blocking you. The
recommended pattern: `send` returns a `turnId`; then run `wait --turn ID` in the
background.

```bash
python scripts/external_channel.py send --name 'Build Trace' \
  --sender 'Claude Code' --message 'Run the check.'      # note turnId
python scripts/external_channel.py wait --name 'Build Trace' \
  --turn TURN_ID --format text                           # run in background
```

- Without `--turn`, `wait` awaits the running turn; `--next` awaits the next
  one; with neither it returns the last result with `"alreadyIdle": true`.
- `--timeout` defaults to 3600 seconds, `0` for no limit. A timeout leaves the
  task running; run `wait --turn ID` again to keep waiting.
- Exit codes: 0 completed, 1 helper error, 2 usage error, 3 turn failed,
  4 turn interrupted, 5 timed out. Read the output for `lastAgentMessage`.
- Disconnects and App restarts after the first connection are retried until
  the timeout; an App that is unreachable at start fails with exit 1.

## Install

Clone the repository inside the agent's home and link this skill, not the main
one. For Claude Code:

```bash
git clone https://github.com/xukp20/codex-app-task-channel.git \
  ~/.claude/tool-sources/codex-app-task-channel
ln -s ../tool-sources/codex-app-task-channel/skills/codex-app-task-channel-external \
  ~/.claude/skills/codex-app-task-channel-external
```

The wrapper finds the main helper in the sibling skill directory of the same
checkout. It needs Python 3.11+ and `websockets>=14,<17`.
