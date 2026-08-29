# App Server fallback protocol

Read this reference only when the built-in Codex task tools are unavailable,
cannot express a requested Session setting, or you need to diagnose a failed
receipt.

## Endpoint ownership

The default endpoint is the Unix WebSocket exposed by the running desktop app:

```text
${CODEX_HOME:-$HOME/.codex}/app-server-control/app-server-control.sock
WebSocket URI: ws://localhost/rpc
```

The client sends `initialize`, declares `experimentalApi=true`, then emits the
`initialized` notification. The helper uses one background reader to route all
JSON-RPC responses and notifications. Multiple coroutines must never compete
for raw `recv()` calls; doing so can consume another request's response or lose
`turn/completed`.

## Operations

| Helper operation | App Server / CLI operation | State requirement |
| --- | --- | --- |
| `doctor` | `initialize`, `thread/list` | Read-only |
| `create` | `thread/start`, `thread/name/set`, `turn/start` | New task |
| `fork` | `thread/fork`, `thread/name/set`, `turn/start` | Source turn must be forkable |
| `resume` | `thread/read`, `thread/resume`, `turn/start` | Target must be idle or not loaded |
| `resume --cold-replace` | two `thread/resume` calls, `thread/unsubscribe`, `turn/start` | Loaded target must be idle; caller explicitly opts in |
| `read` | `thread/read(includeTurns=true)` | Read-only |
| `send --mode start` | `turn/start` | No active turn |
| `send --mode steer` | `turn/steer(expectedTurnId=...)` | Matching active turn |
| `send --mode followup` | Read, then steer or start | Race-aware |
| `send --mode queue` | `codex queue --remote unix://...` | Durable; dispatches when idle |

Every direct input uses a UUID `clientUserMessageId`. The same ID is retained
when `followup` rereads state and performs its one opposite-operation retry.

## Delivery semantics

`turn/steer` adds input to an in-progress turn and requires its exact
`expectedTurnId`. It is the only fallback mode that performs immediate same-turn
intervention.

`codex queue` stores a message for later delivery. The CLI attempts a turn when
the task is idle; while a turn is active, the message remains queued. This is
useful for durable handoffs, but it is not a replacement for steer.

`resume` is also not a queue. It requires the task to be idle or not loaded,
resolves the Session, and then starts a new turn immediately. Use it when
Session loading or configuration is material, not as a way to wait behind an
active turn.

`followup` implements the common compatibility behavior:

```text
active → steer
idle   → start
```

If state changes between read and mutation, it rereads once and tries the
opposite direct operation with the same message ID. It never silently changes
the requested delivery to queue.

## Session configuration

`turn/start` accepts `model` and `effort`. `thread/start` and `thread/fork`
receive the model and a `model_reasoning_effort` config override, then the first
turn repeats the explicit values.

The helper maps explicit options as follows:

| CLI option | App Server field |
| --- | --- |
| `--service-tier` | `serviceTier` |
| `--context-window N` | `config.model_context_window=N` |
| `--auto-compact-token-limit N` | `config.model_auto_compact_token_limit=N` |

Ordinary `resume` does not send an empty config map or try to replace a loaded
Session. An unloaded task receives the explicit configuration directly. A
loaded idle task rejoins the existing Session; model, effort, service tier, and
cwd are applied on the subsequent `turn/start` because those fields do not
require replacing the Session.

Context window and auto-compaction are Session-only settings. Applying either
to a loaded task requires explicit `--cold-replace`. The helper then performs a
two-stage canary before the real message:

1. resume without overrides to record the active configuration and subscribe
   the helper connection;
2. unsubscribe that helper connection only;
3. resume with the requested Session settings and a distinct supported
   reasoning effort;
4. require the resume response to show the canary effort;
5. restore the intended effort on the real `turn/start`.

`thread/unsubscribe` is scoped to the calling connection and cannot evict a
Desktop subscriber. If Desktop or another connection remains subscribed, App
Server preserves the loaded Session and ignores mismatching resume overrides;
the canary detects this and fails before the actual user message is sent. There
is no public force-unsubscribe or force-unload RPC.

The resume response exposes model, reasoning effort, and service tier, but not
the effective context window. After `turn/start`, the helper locates the task's
rollout and reads the matching `task_started.model_context_window`. It reports
the requested nominal window, the expected effective window after max-window
clamping and percentage adjustment from `models_cache.json`, and the observed
effective window.

These overrides configure the current Session. Codex does not persist context
window or auto-compaction settings as durable task metadata; a future cold
resume must resend them when they still matter.

`turn/steer` has no model, effort, service-tier, or session-config fields.
Reject a steer request that claims to enforce those settings. A context-window
or auto-compaction change for a reused task must therefore use idle `resume`.

## Failure handling

- Built-in mutation error: verify list/read state before fallback.
- Missing socket: confirm the desktop app is running and the correct
  `CODEX_HOME` is selected.
- JSON-RPC rejection: preserve the error body; do not downgrade to queue unless
  the caller explicitly asked for queue.
- Timeout or disconnect after mutation: use `clientUserMessageId`, thread
  readback, and the returned thread/turn identifiers before retrying.
- `steer` reports no active turn: choose `followup` or wait for idle and use
  `start`.
- loaded `resume` requests Session-only settings without `--cold-replace`:
  reject before any resume or message mutation.
- `resume --cold-replace` reports a canary mismatch: another subscriber or a
  failed shutdown prevented safe replacement; stop before sending the message.
- context verification is false after turn start: report the actual value and
  do not resend blindly.
- App upgrade breaks `doctor`: compare current official App Server schema and
  Codex source before changing payloads.

Official reference: <https://developers.openai.com/codex/app-server>
