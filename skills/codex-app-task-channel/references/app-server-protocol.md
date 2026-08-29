# App Server fallback protocol

Read this reference only when the built-in Codex task tools are unavailable and
you need to choose a fallback operation or diagnose a failed receipt.

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

`followup` implements the common compatibility behavior:

```text
active → steer
idle   → start
```

If state changes between read and mutation, it rereads once and tries the
opposite direct operation with the same message ID. It never silently changes
the requested delivery to queue.

## Model and reasoning

`turn/start` accepts `model` and `effort`. `thread/start` and `thread/fork`
receive the model and a `model_reasoning_effort` config override, then the first
turn repeats the explicit values.

`turn/steer` has no model or effort fields. Reject a steer request that claims
to enforce either setting. A configuration handshake for a reused task must
therefore happen as a new idle turn.

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
- App upgrade breaks `doctor`: compare current official App Server schema and
  Codex source before changing payloads.

Official reference: <https://learn.chatgpt.com/docs/app-server>
