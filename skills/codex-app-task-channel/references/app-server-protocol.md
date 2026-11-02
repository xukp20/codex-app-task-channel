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
| `read` | `thread/read`, `thread/turns/list(itemsView=full)` | Read-only |
| `send --mode start` | `turn/start` | No active turn |
| `send --mode steer` | `turn/steer(expectedTurnId=...)` | Matching active turn |
| `send --mode followup` | Read, then steer or start | Race-aware |
| `send --mode queue` | `codex queue --remote unix://...` | Durable; dispatches when idle |

`doctor` establishes initialization and listing only; it does not prove delivery
or full-history compatibility. Delivery preflight reads the exact task metadata
with `includeTurns=false`, a bounded five-turn page with `itemsView=notLoaded`,
then metadata again. Changed identity/state or inconsistent active-turn evidence
blocks sending. This avoids the legacy full-history route that some endpoints
reject with `list_turns is not supported yet`.

Every direct input uses a UUID `clientUserMessageId`. The same ID is retained
when `followup` rereads state and performs its one opposite-operation retry.

## Content readback

State inspection uses metadata and bounded turn pages without history items.
Full-history hydration is unnecessary and may be unsupported. For explicitly
requested user-visible content, the helper calls
`thread/turns/list` newest-first with `itemsView: "full"`, then emits compact
user/agent messages. `read --include-items` preserves the complete raw items
when command or tool evidence is required.

After `send`, `create`, `fork`, or `resume` with `--wait`, the helper reads the
completed target turn through the same native endpoint and returns
`messages` plus `lastAgentMessage`. Treat the accepted `turn/start` or
`turn/steer` response as a dispatch receipt; use this completed readback as
delivery evidence.

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

These overrides configure the current Session. Current source can restore model,
provider ID and reasoning effort from thread metadata when no model-family
override is supplied. This does not persist arbitrary provider definitions,
credential overrides, context or auto-compaction settings. Helper bindings
explicitly reload those references on resume; direct Desktop resume bypasses this.

## Explicit session config inputs and bindings

`--provider` maps to `modelProvider` on thread/start, thread/fork and thread/resume.
`--agent-config` reads a registered role's file; `--config-file` reads a dedicated
TOML file. The imported subset is model, model_provider, model_reasoning_effort,
service_tier, model_context_window, model_auto_compact_token_limit,
developer_instructions and model_providers. Other role settings are not copied;
unknown keys in an explicit config file are rejected. Permission and tool settings
are intentionally outside this interface. Explicit CLI settings win over the
config file, which wins over imported role values. Provider definitions merge
global and local values, retaining only the selected provider.

The helper sends these through the Session config map, verifies returned model,
provider, effort and service tier before turn/start, and verifies context through
the matching task_started event afterwards. Actual HTTP authentication is not
exposed by the response; no claim of credential-use verification should be based
only on READY. Provider mismatch returns the created thread ID and sends no task.

Private `task-channel/threads/ID.json` bindings store references and non-secret CLI
overrides, never credential/config snapshots. Sources are read again at resume.
Bindings are socket-scoped, atomically written with 0600 permissions, and survive
process exit. They do not intercept Desktop or ordinary send calls. Loaded bound
resumes require cold replacement and retain the native subscriber/canary limits.

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
