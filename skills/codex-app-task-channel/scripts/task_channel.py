#!/usr/bin/env python3
"""Fallback task creation and messaging for the Codex desktop app server."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Self

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_profiles

try:
    from websockets.legacy.client import unix_connect
except ImportError:  # pragma: no cover - exercised by the runtime dependency check
    unix_connect = None


class ChannelError(RuntimeError):
    """A task-channel operation failed without a safe automatic fallback."""


class ConnectionLost(ChannelError):
    """The app-server transport is gone or unreachable; waits reconnect, others fail."""


class WaitExpired(Exception):
    """Internal: the wait deadline passed while reconnecting."""


class TaskStateUncertain(ChannelError):
    """Read-only snapshots do not establish a safe delivery state."""


class RpcError(ChannelError):
    """A JSON-RPC request was rejected by the app server."""

    def __init__(self, method: str, error: Any) -> None:
        super().__init__(f"{method} failed: {json.dumps(error, ensure_ascii=False)}")
        self.method = method
        self.error = error


class AppServerClient:
    """Small JSON-RPC client with one reader that routes all responses."""

    def __init__(self, socket_path: Path, *, timeout: float = 15.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._lost: str | None = None
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        await self._connect()
        return self

    async def _connect(self) -> None:
        if unix_connect is None:
            raise ChannelError(
                "Python dependency 'websockets' is missing; install websockets>=14,<17"
            )
        if not self.socket_path.is_socket():
            raise ConnectionLost(f"Codex app-server socket not found: {self.socket_path}")
        self._lost = None
        try:
            self._ws = await unix_connect(
                path=str(self.socket_path),
                uri="ws://localhost/rpc",
                compression=None,
                max_size=None,
                open_timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - any connect failure is a transport failure
            raise ConnectionLost(f"cannot connect to the Codex app-server: {exc!r}") from exc
        self._reader_task = asyncio.create_task(self._reader())
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-app-task-channel",
                        "title": "Codex App Task Channel",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.notify("initialized")
        except BaseException:
            await self._close()
            raise

    async def reconnect(self) -> None:
        """Drop the dead transport and open a fresh one; notifications stay queued."""
        await self._close()
        await self._connect()

    async def _close(self) -> None:
        ws, self._ws = self._ws, None
        task, self._reader_task = self._reader_task, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._fail_pending("app-server connection closed")

    async def __aexit__(self, *_exc: object) -> None:
        await self._close()

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionLost(reason))
        self._pending.clear()

    async def _reader(self) -> None:
        reason = "app-server connection closed"
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                request_id = message.get("id")
                if request_id is not None and request_id in self._pending:
                    future = self._pending.pop(request_id)
                    if "error" in message:
                        future.set_exception(RpcError("json-rpc", message["error"]))
                    else:
                        future.set_result(message.get("result"))
                elif "method" in message:
                    await self.notifications.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail every pending RPC on transport loss
            reason = f"app-server connection failed: {exc}"
        self._lost = reason
        self._fail_pending(reason)
        # Wake any waiter sleeping on notifications so it notices the loss now.
        self.notifications.put_nowait({"method": "connection/lost"})

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if self._lost is not None:
            raise ConnectionLost(self._lost)
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            try:
                await self._ws.send(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                        separators=(",", ":"),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - a failed send is a transport failure
                raise ConnectionLost(f"app-server connection failed: {exc!r}") from exc
            try:
                return await asyncio.wait_for(future, timeout=self.timeout)
            except TimeoutError as exc:
                raise ConnectionLost(f"{method} timed out after {self.timeout}s") from exc
        except RpcError as exc:
            exc.method = method
            exc.args = (f"{method} failed: {json.dumps(exc.error, ensure_ascii=False)}",)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._ws.send(json.dumps(message, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001 - a failed send is a transport failure
            raise ConnectionLost(f"app-server connection failed: {exc!r}") from exc

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        before = await self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        page = await self.list_turns(thread_id, limit=5, items_view="notLoaded")
        after = await self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = after["thread"]
        initial = before["thread"]
        if (
            thread.get("id") != thread_id
            or initial.get("id") != thread_id
            or thread.get("cwd") != initial.get("cwd")
        ):
            raise ChannelError("task identity changed during delivery preflight")
        if thread_status(thread) != thread_status(initial):
            raise TaskStateUncertain("task state changed during delivery preflight")
        turns = page.get("data")
        if not isinstance(turns, list) or len(turns) > 5:
            raise ChannelError("invalid bounded task state page")
        for turn in turns:
            if (
                not isinstance(turn, dict)
                or not isinstance(turn.get("id"), str)
                or not turn["id"].strip()
                or turn.get("status") not in {"inProgress", "completed", "interrupted", "failed"}
            ):
                raise ChannelError("invalid task turn identity or status; no message sent")
        active = [turn for turn in turns if turn.get("status") == "inProgress"]
        status = thread_status(thread)
        if len(active) > 1:
            raise ChannelError("multiple active task turns; no message sent")
        if (status == "active") != bool(active):
            raise TaskStateUncertain("task state is uncertain; reconcile before sending")
        if status not in {"active", "idle", "notLoaded", "systemError"}:
            raise ChannelError("unsupported task state; no message sent")
        thread = dict(thread)
        thread["turns"] = turns
        return thread

    async def list_turns(
        self,
        thread_id: str,
        *,
        limit: int,
        items_view: str,
    ) -> dict[str, Any]:
        return await self.request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": limit,
                "sortDirection": "desc",
                "itemsView": items_view,
            },
        )


def default_socket_path() -> Path:
    explicit = os.environ.get("CODEX_APP_SERVER_SOCKET")
    if explicit:
        return Path(explicit).expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return (codex_home / "app-server-control" / "app-server-control.sock").resolve()


def codex_home_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def text_input(message: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": message, "textElements": []}]


def delegated_message(message: str, source_thread_id: str | None) -> str:
    if not source_thread_id:
        return message
    return (
        "<codex_delegation>\n"
        f"  <source_thread_id>{html.escape(source_thread_id)}</source_thread_id>\n"
        f"  <input>{html.escape(message)}</input>\n"
        "</codex_delegation>"
    )


def active_turn_id(thread: dict[str, Any]) -> str | None:
    for turn in reversed(thread.get("turns", [])):
        if turn.get("status") == "inProgress":
            return turn.get("id")
    return None


def thread_status(thread: dict[str, Any]) -> str:
    status = thread.get("status")
    if isinstance(status, dict):
        return str(status.get("type", "unknown"))
    return str(status)


LIST_PAGE_LIMIT = 100
LIST_MAX_PAGES = 50
LIST_SCAN_LIMIT = 1000


def epoch_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def thread_summary(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "threadId": thread.get("id"),
        "name": thread.get("name"),
        "status": thread_status(thread),
        "modelProvider": thread.get("modelProvider"),
        "cwd": thread.get("cwd"),
        "updatedAt": epoch_iso(thread.get("updatedAt")),
        "source": thread.get("source"),
    }


async def list_threads(
    client: AppServerClient,
    *,
    search: str | None = None,
    providers: list[str] | None = None,
    archived: bool = False,
    max_threads: int = 50,
) -> list[dict[str, Any]]:
    """Return distinct tasks, newest first, across all model providers by default.

    Without ``modelProviders``, App Server lists only the current default
    provider's tasks, which hides tasks that run on another provider. An empty
    list asks for every provider. Pages can repeat a task and are not ordered
    by update time, so rows are merged by id, the most recently updated row is
    kept, and the result is sorted locally after reading all pages (bounded by
    LIST_SCAN_LIMIT distinct tasks).
    """
    params: dict[str, Any] = {
        "limit": LIST_PAGE_LIMIT,
        "modelProviders": list(providers or []),
    }
    if search:
        params["searchTerm"] = search
    if archived:
        params["archived"] = True
    seen: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    for _ in range(LIST_MAX_PAGES):
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        page = await client.request("thread/list", page_params)
        for thread in page.get("data", []):
            thread_id = thread.get("id")
            if not isinstance(thread_id, str):
                continue
            previous = seen.get(thread_id)
            if previous is None or (thread.get("updatedAt") or 0) > (
                previous.get("updatedAt") or 0
            ):
                seen[thread_id] = thread
        cursor = page.get("nextCursor")
        if not cursor or len(seen) >= max(max_threads, LIST_SCAN_LIMIT):
            break
    threads = sorted(seen.values(), key=lambda item: item.get("updatedAt") or 0, reverse=True)
    return threads[:max_threads]


def ambiguous_name_error(query: str, matches: list[dict[str, Any]]) -> ChannelError:
    shown = "; ".join(
        f"{item.get('id')} {item.get('name')!r} ({item.get('modelProvider')}, {thread_status(item)})"
        for item in matches[:10]
    )
    more = f"; and {len(matches) - 10} more" if len(matches) > 10 else ""
    return ChannelError(
        f"task name {query!r} matches {len(matches)} tasks; use the full name or --thread: "
        f"{shown}{more}"
    )


async def resolve_thread_name(client: AppServerClient, name: str) -> dict[str, Any]:
    """Resolve a task name to exactly one task, or fail without side effects.

    An exact name wins; otherwise a case-insensitive substring must match one
    task. Server search narrows candidates, and a plain listing is merged in so
    that a search-index gap cannot hide the target.
    """
    query = name.strip()
    if not query:
        raise ChannelError("task name must not be empty")
    candidates = {
        item["id"]: item for item in await list_threads(client, search=query, max_threads=200)
    }
    for item in await list_threads(client, max_threads=500):
        candidates.setdefault(item["id"], item)
    named = [(item, str(item.get("name") or "").strip()) for item in candidates.values()]
    exact = [item for item, label in named if label == query]
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise ambiguous_name_error(query, exact)
    folded = query.casefold()
    partial = [item for item, label in named if folded in label.casefold()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ChannelError(f"no task name contains {query!r}; run list to see task names")
    raise ambiguous_name_error(query, partial)


def common_turn_params(args: argparse.Namespace, message: str, message_id: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": args.thread,
        "clientUserMessageId": message_id,
        "input": text_input(message),
    }
    if getattr(args, "model", None):
        params["model"] = args.model
    if getattr(args, "effort", None):
        params["effort"] = args.effort
    if getattr(args, "service_tier", None):
        params["serviceTier"] = args.service_tier
    if getattr(args, "cwd", None):
        params["cwd"] = str(Path(args.cwd).expanduser().resolve())
    return params


async def start_turn(
    client: AppServerClient,
    args: argparse.Namespace,
    message: str,
    message_id: str,
) -> dict[str, Any]:
    result = await client.request("turn/start", common_turn_params(args, message, message_id))
    return {"delivery": "start", "turnId": result["turn"]["id"]}


async def steer_turn(
    client: AppServerClient,
    args: argparse.Namespace,
    message: str,
    message_id: str,
    expected_turn_id: str,
) -> dict[str, Any]:
    if (
        getattr(args, "model", None)
        or getattr(args, "effort", None)
        or getattr(args, "service_tier", None)
    ):
        raise ChannelError(
            "turn/steer cannot change model, reasoning effort, or service tier; "
            "wait for idle and use start"
        )
    result = await client.request(
        "turn/steer",
        {
            "threadId": args.thread,
            "clientUserMessageId": message_id,
            "input": text_input(message),
            "expectedTurnId": expected_turn_id,
        },
    )
    return {"delivery": "steer", "turnId": result["turnId"]}


async def followup_turn(
    client: AppServerClient,
    args: argparse.Namespace,
    message: str,
    message_id: str,
) -> dict[str, Any]:
    thread = await client.read_thread(args.thread)
    current = active_turn_id(thread)
    if current is not None:
        try:
            return await steer_turn(client, args, message, message_id, current)
        except RpcError:
            refreshed = await client.read_thread(args.thread)
            if active_turn_id(refreshed) is not None:
                raise
            return await start_turn(client, args, message, message_id)
    try:
        return await start_turn(client, args, message, message_id)
    except RpcError:
        refreshed = await client.read_thread(args.thread)
        current = active_turn_id(refreshed)
        if current is None:
            raise
        return await steer_turn(client, args, message, message_id, current)


# Exit codes shared by `wait` and every `--wait`. 1 is any helper error and 2 a
# usage error (argparse, external wrapper); the rest report the awaited turn.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TURN_FAILED = 3
EXIT_TURN_INTERRUPTED = 4
EXIT_WAIT_TIMEOUT = 5

DEFAULT_WAIT_TIMEOUT = 3600.0
WAIT_PAGE_LIMIT = 50
WAIT_POLL_MIN = 1.0
WAIT_POLL_MAX = 15.0
RECONNECT_MIN = 2.0
RECONNECT_MAX = 30.0


def deadline_after(timeout: float | None) -> float | None:
    """Absolute loop-time deadline; a falsy timeout means wait without a limit."""
    if not timeout:
        return None
    return asyncio.get_running_loop().time() + timeout


def time_left(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - asyncio.get_running_loop().time()


async def reconnect_until(client: AppServerClient, deadline: float | None) -> bool:
    """Reconnect with backoff until it works or the deadline passes."""
    backoff = RECONNECT_MIN
    while True:
        pause = backoff
        remaining = time_left(deadline)
        if remaining is not None:
            if remaining <= 0:
                return False
            pause = min(pause, remaining)
        await asyncio.sleep(pause)
        try:
            await client.reconnect()
            return True
        except ConnectionLost:
            backoff = min(backoff * 2, RECONNECT_MAX)


async def with_reconnect(
    client: AppServerClient, deadline: float | None, operation: Callable[[], Any]
) -> Any:
    """Run a read-only operation, reconnecting after transport loss only.

    Logic errors (RpcError, other ChannelError) propagate unchanged.
    """
    while True:
        try:
            return await operation()
        except ConnectionLost:
            if not await reconnect_until(client, deadline):
                raise WaitExpired from None


async def sleep_or_notified(
    client: AppServerClient, delay: float, remaining: float | None
) -> None:
    """Sleep up to ``delay`` seconds, returning early on a state notification."""
    loop = asyncio.get_running_loop()
    end = loop.time() + (delay if remaining is None else min(delay, remaining))
    while True:
        left = end - loop.time()
        if left <= 0:
            return
        try:
            note = await asyncio.wait_for(client.notifications.get(), timeout=left)
        except TimeoutError:
            return
        method = str(note.get("method", "")) if isinstance(note, dict) else ""
        if not (method.startswith("item/") or "delta" in method.lower()):
            break
    while not client.notifications.empty():
        client.notifications.get_nowait()


async def poll_until(
    client: AppServerClient, deadline: float | None, step: Callable[[], Any]
) -> Any:
    """Call ``step`` until it returns a value that is not None; None means timed out."""
    delay = WAIT_POLL_MIN
    try:
        while True:
            value = await with_reconnect(client, deadline, step)
            if value is not None:
                return value
            remaining = time_left(deadline)
            if remaining is not None and remaining <= 0:
                return None
            await sleep_or_notified(client, delay, remaining)
            delay = min(delay * 1.5, WAIT_POLL_MAX)
    except WaitExpired:
        return None


def timeout_result(thread_id: str, turn_id: str | None) -> dict[str, Any]:
    target = f"--turn {turn_id}" if turn_id else "--next"
    return {
        "threadId": thread_id,
        "turnId": turn_id,
        "status": "inProgress" if turn_id else "notStarted",
        "timedOut": True,
        "error": None,
        "lastAgentMessage": None,
        "note": (
            "wait timed out; the other task is still running and was not affected; "
            f"continue with: wait --thread {thread_id} {target}"
        ),
    }


def terminal_result(
    thread_id: str, turn: dict[str, Any], *, status: Any = None, error: Any = None
) -> dict[str, Any]:
    messages = conversation_messages(turn)
    return {
        "threadId": thread_id,
        "turnId": turn.get("id"),
        "status": status or turn.get("status"),
        "error": error if status else turn.get("error"),
        "messages": messages,
        "lastAgentMessage": last_agent_message(messages),
    }


async def find_turn(
    client: AppServerClient, thread_id: str, turn_id: str
) -> tuple[int, dict[str, Any]] | None:
    page = await client.list_turns(thread_id, limit=WAIT_PAGE_LIMIT, items_view="notLoaded")
    for index, turn in enumerate(page.get("data", [])):
        if turn.get("id") == turn_id:
            return index, turn
    return None


async def settled_turn(
    client: AppServerClient, thread_id: str, turn_id: str
) -> dict[str, Any] | None:
    """The terminal result for the turn, or None while it is not finished."""
    found = await find_turn(client, thread_id, turn_id)
    if found is None:
        return None
    index, light = found
    status = light.get("status")
    if status in {None, "inProgress"}:
        return None
    override: dict[str, Any] = {}
    # Rollout-backed pages can briefly label an unfinished turn interrupted.
    # Confirm against the loaded Session before treating it as terminal.
    if status == "interrupted":
        try:
            thread = await client.read_thread(thread_id)
        except TaskStateUncertain:
            return None
        if active_turn_id(thread) == turn_id:
            return None
        live = next((item for item in thread.get("turns", []) if item.get("id") == turn_id), None)
        if live and live.get("status") in {"completed", "failed"}:
            override = {"status": live["status"], "error": live.get("error")}
    # Fetch full items once, for exactly the pages up to and including the turn.
    page = await client.list_turns(thread_id, limit=index + 1, items_view="full")
    turn = next((item for item in page.get("data", []) if item.get("id") == turn_id), light)
    return terminal_result(thread_id, turn, **override)


async def wait_for_terminal(
    client: AppServerClient,
    thread_id: str,
    turn_id: str,
    timeout: float | None,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Wait for a turn to finish; a timeout returns a ``timedOut`` result instead of raising.

    Polls with itemsView=notLoaded, fetches full items once after the turn
    settles, and survives disconnects by reconnecting with backoff.
    """
    if deadline is None:
        deadline = deadline_after(timeout)
    result = await poll_until(
        client, deadline, lambda: settled_turn(client, thread_id, turn_id)
    )
    return result if result is not None else timeout_result(thread_id, turn_id)


async def wait_for_new_turn(
    client: AppServerClient, thread_id: str, known: set[str], deadline: float | None
) -> str | None:
    async def step() -> str | None:
        page = await client.list_turns(thread_id, limit=5, items_view="notLoaded")
        fresh = [t["id"] for t in page.get("data", []) if t.get("id") not in known]
        return fresh[-1] if fresh else None

    return await poll_until(client, deadline, step)


def conversation_messages(turn: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in turn.get("items", []):
        item_type = item.get("type")
        if item_type == "userMessage":
            text = "".join(
                part.get("text", "")
                for part in item.get("content", [])
                if part.get("type") == "text"
            )
            messages.append({"role": "user", "text": text})
        elif item_type == "agentMessage":
            messages.append(
                {
                    "role": "assistant",
                    "text": item.get("text", ""),
                    "phase": item.get("phase"),
                }
            )
    return messages


def last_agent_message(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return str(message.get("text", ""))
    return None


def load_message(args: argparse.Namespace) -> str:
    if getattr(args, "message", None) is not None:
        return args.message
    if getattr(args, "message_file", None) is not None:
        return Path(args.message_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ChannelError("provide --message, --message-file, or message text on stdin")


def queue_message(args: argparse.Namespace, message: str) -> dict[str, Any]:
    command = [
        args.codex_binary,
        "queue",
        "--remote",
        f"unix://{args.socket}",
        "--thread",
        args.thread,
        "--message",
        message,
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.effort:
        command.extend(["--config", f'model_reasoning_effort="{args.effort}"'])
    if args.service_tier:
        command.extend(["--config", f'service_tier="{args.service_tier}"'])
    if args.cwd:
        command.extend(["--cd", str(Path(args.cwd).expanduser().resolve())])
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise ChannelError(
            f"codex queue failed ({result.returncode}): {result.stderr.strip()}"
        )
    return {"delivery": "queue", "stdout": result.stdout.strip()}


async def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        sample = await client.request("thread/list", {"limit": 1})
    version = await asyncio.to_thread(
        subprocess.run,
        [args.codex_binary, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "ok": True,
        "socket": str(args.socket),
        "codexVersion": version.stdout.strip() if version.returncode == 0 else None,
        "threadListReachable": isinstance(sample.get("data"), list),
    }


def thread_creation_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": args.model,
        "modelProvider": getattr(args, "provider", None),
        "serviceTier": args.service_tier,
        "cwd": str(Path(args.cwd).expanduser().resolve()),
        "ephemeral": False,
    }
    config = session_config_overrides(args)
    if config:
        params["config"] = config
    return {key: value for key, value in params.items() if value is not None}


def session_config_overrides(args: argparse.Namespace) -> dict[str, Any]:
    config: dict[str, Any] = dict(getattr(args, "session_overrides", {}))
    if getattr(args, "effort", None):
        config["model_reasoning_effort"] = args.effort
    if getattr(args, "context_window", None) is not None:
        config["model_context_window"] = args.context_window
    if getattr(args, "auto_compact_token_limit", None) is not None:
        config["model_auto_compact_token_limit"] = args.auto_compact_token_limit
    return config


def has_session_only_overrides(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "context_window", None) is not None
        or getattr(args, "auto_compact_token_limit", None) is not None
        or bool(getattr(args, "session_overrides", {}))
        or bool(getattr(args, "provider", None))
    )


def resume_params(
    args: argparse.Namespace, *, config_overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": args.thread,
        "model": args.model,
        "modelProvider": getattr(args, "provider", None),
        "serviceTier": args.service_tier,
        "cwd": str(Path(args.cwd).expanduser().resolve()) if args.cwd else None,
        "excludeTurns": True,
    }
    config = (
        session_config_overrides(args)
        if config_overrides is None
        else config_overrides
    )
    if config:
        params["config"] = config
    return {key: value for key, value in params.items() if value is not None}


def validate_resume_response(
    args: argparse.Namespace,
    result: dict[str, Any],
    *,
    expected_effort: str | None = None,
) -> None:
    expected = {
        "model": args.model,
        "modelProvider": getattr(args, "provider", None),
        "reasoningEffort": expected_effort if expected_effort is not None else args.effort,
        "serviceTier": args.service_tier,
    }
    mismatches = [
        f"{key} requested={value!r} active={result.get(key)!r}"
        for key, value in expected.items()
        if value is not None and result.get(key) != value
    ]
    if mismatches:
        raise ChannelError(
            "thread/resume did not apply the requested configuration; "
            + "; ".join(mismatches)
        )


def model_cache_entry(model: str) -> dict[str, Any] | None:
    cache_path = codex_home_path() / "models_cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return next(
        (item for item in cache.get("models", []) if item.get("slug") == model), None
    )


def choose_probe_effort(model: str, active_effort: str | None) -> str:
    model_info = model_cache_entry(model)
    if model_info is None:
        raise ChannelError(f"model metadata is unavailable for cold-replace probe: {model}")
    supported = [
        item.get("effort")
        for item in model_info.get("supported_reasoning_levels", [])
        if isinstance(item.get("effort"), str)
    ]
    probe = next((effort for effort in supported if effort != active_effort), None)
    if probe is None:
        raise ChannelError(
            f"cannot choose a distinct supported reasoning-effort probe for model {model}"
        )
    return probe


def intended_turn_effort(
    args: argparse.Namespace, baseline: dict[str, Any], target_model: str
) -> str | None:
    if args.effort is not None:
        return args.effort
    if baseline.get("model") == target_model and baseline.get("reasoningEffort") is not None:
        return baseline["reasoningEffort"]
    model_info = model_cache_entry(target_model)
    if model_info is None:
        return None
    value = model_info.get("default_reasoning_level")
    return value if isinstance(value, str) else None


def find_rollout_path(thread_id: str) -> Path | None:
    candidates = list(
        (codex_home_path() / "sessions").glob(
            f"*/*/*/rollout-*-{thread_id}.jsonl"
        )
    )
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def read_task_context_window(thread_id: str, turn_id: str) -> int | None:
    rollout_path = find_rollout_path(thread_id)
    if rollout_path is None:
        return None
    with rollout_path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - 512 * 1024))
        tail = handle.read().decode("utf-8", errors="replace")
    for line in reversed(tail.splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = item.get("payload", {})
        if (
            item.get("type") == "event_msg"
            and payload.get("type") == "task_started"
            and payload.get("turn_id") == turn_id
        ):
            value = payload.get("model_context_window")
            return value if isinstance(value, int) else None
    return None


async def observe_task_context_window(
    thread_id: str, turn_id: str, *, timeout: float = 5.0
) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        value = await asyncio.to_thread(read_task_context_window, thread_id, turn_id)
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.1)


def expected_effective_context_window(model: str | None, requested: int | None) -> int | None:
    if model is None or requested is None:
        return None
    model_info = model_cache_entry(model)
    if model_info is None:
        return None
    maximum = model_info.get("max_context_window")
    percent = model_info.get("effective_context_window_percent")
    if not isinstance(maximum, int) or not isinstance(percent, int):
        return None
    return min(requested, maximum) * percent // 100


def context_config_receipt(
    args: argparse.Namespace, observed: int | None, model: str | None
) -> dict[str, Any]:
    requested = getattr(args, "context_window", None)
    expected = expected_effective_context_window(model, requested)
    receipt = {
        "requestedModelContextWindow": getattr(args, "context_window", None),
        "requestedAutoCompactTokenLimit": getattr(
            args, "auto_compact_token_limit", None
        ),
        "expectedEffectiveModelContextWindow": expected,
        "observedEffectiveModelContextWindow": observed,
    }
    receipt["contextWindowVerified"] = (
        observed == expected if expected is not None and observed is not None else None
    )
    return receipt


async def create_or_fork(args: argparse.Namespace, *, fork: bool) -> dict[str, Any]:
    message = delegated_message(load_message(args), args.source_thread)
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        if fork:
            params = {
                "threadId": args.from_thread,
                "lastTurnId": args.last_turn,
                "beforeTurnId": None,
                "path": None,
                **thread_creation_params(args),
                "excludeTurns": False,
                "deferGoalContinuation": True,
            }
            result = await client.request("thread/fork", params)
        else:
            result = await client.request("thread/start", thread_creation_params(args))
        thread_id = result["thread"]["id"]
        binding = session_profiles.save_binding(args, thread_id)
        try:
            validate_resume_response(args, result)
        except ChannelError as exc:
            raise ChannelError(f"created thread {thread_id}, but configuration verification failed; no task sent: {exc}") from exc
        await client.request("thread/name/set", {"threadId": thread_id, "name": args.title})
        turn_args = argparse.Namespace(
            thread=thread_id,
            model=args.model,
            effort=args.effort,
            service_tier=args.service_tier,
            cwd=args.cwd,
        )
        message_id = str(uuid.uuid4())
        delivery = await start_turn(client, turn_args, message, message_id)
        observed_context_window = await observe_task_context_window(
            thread_id, delivery["turnId"]
        )
        receipt: dict[str, Any] = {
            "threadId": thread_id,
            "modelProvider": result.get("modelProvider"),
            "providerVerified": result.get("modelProvider") == getattr(args, "provider", None) if getattr(args, "provider", None) else None,
            "configBinding": binding,
            "title": args.title,
            "createdBy": "thread/fork" if fork else "thread/start",
            "sourceThreadId": args.from_thread if fork else None,
            "clientUserMessageId": message_id,
            **delivery,
            **context_config_receipt(args, observed_context_window, result.get("model")),
        }
        if args.wait:
            receipt["terminal"] = await wait_for_terminal(
                client, thread_id, delivery["turnId"], args.wait_timeout
            )
        return receipt


async def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    message = delegated_message(load_message(args), args.source_thread)
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        thread = await client.read_thread(args.thread)
        if active_turn_id(thread) is not None:
            raise ChannelError("resume requires an idle task; the target has an active turn")
        status = thread_status(thread)
        if status not in {"idle", "notLoaded", "systemError"}:
            raise ChannelError(
                f"resume requires an idle or not-loaded task; current status is {status}"
            )

        if status != "notLoaded" and has_session_only_overrides(args) and not args.cold_replace:
            raise ChannelError(
                "the task is already loaded; context-window and auto-compaction overrides "
                "require --cold-replace, or retry after the task becomes notLoaded"
            )

        cold_replace_used = False
        probe_effort: str | None = None
        baseline_effort: str | None = None
        turn_effort = args.effort
        if status != "notLoaded" and args.cold_replace:
            baseline = await client.request(
                "thread/resume",
                {"threadId": args.thread, "excludeTurns": True},
            )
            baseline_effort = baseline.get("reasoningEffort")
            unsubscribe = await client.request(
                "thread/unsubscribe", {"threadId": args.thread}
            )
            if unsubscribe.get("status") != "unsubscribed":
                raise ChannelError(
                    "cold-replace preflight could not remove the helper's own subscription; "
                    f"status={unsubscribe.get('status')!r}"
                )

            target_model = args.model or baseline.get("model")
            if not isinstance(target_model, str):
                raise ChannelError("cold-replace preflight could not resolve the target model")
            probe_effort = choose_probe_effort(target_model, baseline_effort)
            probe_config = session_config_overrides(args)
            probe_config["model_reasoning_effort"] = probe_effort
            resumed = await client.request(
                "thread/resume",
                resume_params(args, config_overrides=probe_config),
            )
            try:
                validate_resume_response(
                    args, resumed, expected_effort=probe_effort
                )
            except ChannelError:
                await client.request("thread/unsubscribe", {"threadId": args.thread})
                raise
            turn_effort = intended_turn_effort(args, baseline, target_model)
            cold_replace_used = True
        elif status == "notLoaded":
            resumed = await client.request("thread/resume", resume_params(args))
            validate_resume_response(args, resumed)
        else:
            # A normal loaded-task resume intentionally avoids Session config
            # overrides. Model, effort, service tier, and cwd are applied by
            # the real turn/start below without replacing the Session.
            resumed = await client.request(
                "thread/resume",
                {"threadId": args.thread, "excludeTurns": True},
            )

        turn_args = argparse.Namespace(
            thread=args.thread,
            model=args.model,
            effort=turn_effort,
            service_tier=args.service_tier,
            cwd=args.cwd,
        )
        message_id = str(uuid.uuid4())
        binding = session_profiles.save_binding(args, args.thread)
        delivery = await start_turn(client, turn_args, message, message_id)
        observed_context_window = await observe_task_context_window(
            args.thread, delivery["turnId"]
        )
        receipt: dict[str, Any] = {
            "threadId": args.thread,
            "modelProvider": resumed.get("modelProvider"),
            "providerVerified": resumed.get("modelProvider") == getattr(args, "provider", None) if getattr(args, "provider", None) else None,
            "configBinding": binding,
            "preResumeStatus": status,
            "coldReplaceRequested": args.cold_replace,
            "coldReplaceUsed": cold_replace_used,
            "coldReplaceProbeEffort": probe_effort,
            "preReplaceReasoningEffort": baseline_effort,
            "resumedModel": resumed.get("model"),
            "resumedReasoningEffort": resumed.get("reasoningEffort"),
            "resumedServiceTier": resumed.get("serviceTier"),
            "turnReasoningEffort": turn_effort,
            "clientUserMessageId": message_id,
            **delivery,
            **context_config_receipt(
                args, observed_context_window, resumed.get("model")
            ),
        }
        if args.wait:
            receipt["terminal"] = await wait_for_terminal(
                client, args.thread, delivery["turnId"], args.wait_timeout
            )
        return receipt


async def command_send(args: argparse.Namespace) -> dict[str, Any]:
    message = delegated_message(load_message(args), args.source_thread)
    if args.mode == "queue":
        return queue_message(args, message)
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        message_id = str(uuid.uuid4())
        if args.mode == "steer":
            thread = await client.read_thread(args.thread)
            current = active_turn_id(thread)
            if current is None:
                raise ChannelError("steer requires an active turn")
            receipt = await steer_turn(client, args, message, message_id, current)
        elif args.mode == "start":
            thread = await client.read_thread(args.thread)
            if active_turn_id(thread) is not None:
                raise ChannelError("start requires an idle thread; use steer or followup")
            receipt = await start_turn(client, args, message, message_id)
        else:
            receipt = await followup_turn(client, args, message, message_id)
        receipt.update({"threadId": args.thread, "clientUserMessageId": message_id})
        if args.wait:
            receipt["terminal"] = await wait_for_terminal(
                client, args.thread, receipt["turnId"], args.wait_timeout
            )
        return receipt


async def command_read(args: argparse.Namespace) -> dict[str, Any]:
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        thread = await client.read_thread(args.thread)
        page = await client.list_turns(
            args.thread,
            limit=args.turn_limit,
            items_view="full",
        )
    turns = []
    for turn in page.get("data", []):
        entry = {
            "id": turn.get("id"),
            "status": turn.get("status"),
            "error": turn.get("error"),
            "messages": conversation_messages(turn),
        }
        if args.include_items:
            entry["items"] = turn.get("items", [])
        turns.append(entry)
    return {
        "threadId": thread.get("id"),
        "name": thread.get("name"),
        "status": thread_status(thread),
        "activeTurnId": active_turn_id(thread),
        "turns": turns,
        "nextCursor": page.get("nextCursor"),
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
    }


async def command_wait(args: argparse.Namespace) -> dict[str, Any]:
    """Read-only: never loads, resumes, or subscribes to the task, and starts no turn."""
    deadline = deadline_after(args.timeout)
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        try:
            return await wait_on_thread(client, args, deadline)
        except WaitExpired:
            return timeout_result(args.thread, args.turn)


async def wait_on_thread(
    client: AppServerClient, args: argparse.Namespace, deadline: float | None
) -> dict[str, Any]:
    thread_id = args.thread

    async def snapshot() -> tuple[str, list[dict[str, Any]], str | None]:
        meta = (
            await client.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        )["thread"]
        page = await client.list_turns(thread_id, limit=5, items_view="notLoaded")
        turns = page.get("data", [])
        status = thread_status(meta)
        # A task that is not loaded cannot be running a turn, whatever a stale rollout says.
        active = None if status == "notLoaded" else active_turn_id({"turns": turns})
        return status, turns, active

    turn_id: str | None = args.turn
    already_idle = False
    if turn_id is None:
        _status, turns, active = await with_reconnect(client, deadline, snapshot)
        if active is not None:
            turn_id = active
        elif args.next:
            known = {t.get("id") for t in turns}
            turn_id = await wait_for_new_turn(client, thread_id, known, deadline)
            if turn_id is None:
                return timeout_result(thread_id, None)
        else:
            already_idle = True
            page = await with_reconnect(
                client,
                deadline,
                lambda: client.list_turns(thread_id, limit=1, items_view="full"),
            )
            data = page.get("data", [])
            if not data:
                return {
                    "threadId": thread_id, "turnId": None, "status": "noTurns",
                    "error": None, "lastAgentMessage": None, "alreadyIdle": True,
                }
            result = terminal_result(thread_id, data[0])
    else:
        found = await with_reconnect(
            client,
            deadline,
            lambda: client.list_turns(thread_id, limit=WAIT_PAGE_LIMIT, items_view="notLoaded"),
        )
        if not any(t.get("id") == turn_id for t in found.get("data", [])):
            scope = f"the latest {WAIT_PAGE_LIMIT} turns of " if found.get("nextCursor") else ""
            raise ChannelError(f"turn {turn_id} not found in {scope}task {thread_id}")
    if not already_idle:
        result = await wait_for_terminal(client, thread_id, turn_id, None, deadline=deadline)
    summary = {
        key: result.get(key)
        for key in ("threadId", "turnId", "status", "error", "lastAgentMessage")
    }
    summary["alreadyIdle"] = already_idle
    if result.get("timedOut"):
        summary.update(timedOut=True, note=result["note"])
    return summary


async def command_list(args: argparse.Namespace) -> dict[str, Any]:
    cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else None
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        threads = await list_threads(
            client,
            search=args.search,
            providers=args.provider,
            archived=args.archived,
            max_threads=LIST_SCAN_LIMIT if cwd else args.limit,
        )
    if cwd:
        threads = [item for item in threads if item.get("cwd") == cwd]
    return {"threads": [thread_summary(item) for item in threads[: args.limit]]}


async def resolve_targets(args: argparse.Namespace) -> None:
    """Replace --name/--from-name with the matching thread id before any mutation."""
    name = getattr(args, "name", None)
    from_name = getattr(args, "from_name", None)
    if name is None and from_name is None:
        return
    async with AppServerClient(args.socket, timeout=args.rpc_timeout) as client:
        if name is not None:
            target = await resolve_thread_name(client, name)
            args.thread = target["id"]
            args.resolved_by_name = {"query": name, **thread_summary(target)}
        if from_name is not None:
            source = await resolve_thread_name(client, from_name)
            args.from_thread = source["id"]
            args.resolved_source_by_name = {"query": from_name, **thread_summary(source)}


def render_wait_text(args: argparse.Namespace, result: dict[str, Any]) -> str:
    state = "already idle" if result.get("alreadyIdle") else "finished"
    if result.get("timedOut"):
        state = "timed out"
    lines = [
        f"thread {result.get('threadId')} | turn {result.get('turnId')} | "
        f"status {result.get('status')} ({state})"
    ]
    if result.get("note"):
        lines.append(result["note"])
    if result.get("error"):
        lines.append(f"error: {json.dumps(result['error'], ensure_ascii=False)}")
    message = result.get("lastAgentMessage")
    if message:
        limit = args.max_chars
        shown = message if len(message) <= limit else message[:limit] + f"... [truncated, {len(message)} chars]"
        lines += ["", shown.rstrip()]
    return "\n".join(lines)


def render_text(args: argparse.Namespace, result: dict[str, Any]) -> str:
    if args.command == "list":
        rows = result["threads"]
        if not rows:
            return "No matching tasks."
        lines = []
        for row in rows:
            lines.append(
                f"{row.get('updatedAt') or '-'}  {row['status']:<9}  {row.get('modelProvider') or '-':<12}  "
                f"{row['threadId']}  {row.get('name') or '(unnamed)'}  [{row.get('cwd')}]"
            )
        return "\n".join(lines)
    if args.command == "wait":
        return render_wait_text(args, result)
    lines = [
        f"# {result.get('name') or '(unnamed task)'}",
        f"thread {result.get('threadId')} | status {result.get('status')} | cwd {result.get('cwd')}",
    ]
    turns = list(reversed(result.get("turns", [])))
    if result.get("nextCursor"):
        lines.append(f"(showing the latest {len(turns)} turns; raise --turn-limit for older ones)")
    for index, turn in enumerate(turns, 1):
        lines += ["", f"## Turn {index}/{len(turns)} | {turn.get('status')} | {turn.get('id')}"]
        if turn.get("error"):
            lines.append(f"error: {json.dumps(turn['error'], ensure_ascii=False)}")
        for message in turn.get("messages", []):
            label = "User" if message.get("role") == "user" else "Assistant"
            if message.get("phase"):
                label += f" ({message['phase']})"
            lines += ["", f"### {label}", "", str(message.get("text", "")).rstrip()]
    return "\n".join(lines)


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--rpc-timeout", type=float, default=15.0)
    parser.add_argument("--codex-binary", default="codex")


def add_message_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--message")
    group.add_argument("--message-file")
    parser.add_argument("--source-thread")


def add_turn_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model")
    parser.add_argument(
        "--effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    )
    parser.add_argument("--service-tier")
    parser.add_argument("--cwd")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def add_session_config_args(parser: argparse.ArgumentParser) -> None:
    add_turn_config_args(parser)
    parser.add_argument("--provider", help="explicit session provider id")
    parser.add_argument("--agent-config", help="registered role id imported as a new session config")
    parser.add_argument("--config-file", help="TOML session settings; kept as a persistent local reference")
    parser.add_argument("--context-window", type=positive_int)
    parser.add_argument("--auto-compact-token-limit", type=positive_int)


def timeout_seconds(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("timeout must be >= 0 (0 means no limit)")
    return parsed


def add_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true")
    parser.add_argument(
        "--wait-timeout",
        type=timeout_seconds,
        default=DEFAULT_WAIT_TIMEOUT,
        help="seconds to wait for the turn; 0 waits without a limit (default 3600)",
    )


def add_target_args(parser: argparse.ArgumentParser) -> None:
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--thread", help="task id")
    target.add_argument(
        "--name",
        help="task name; an exact name wins, otherwise the substring must match one task",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check the app-owned endpoint without mutation")
    add_connection_args(doctor)

    create = subparsers.add_parser("create", help="create, name, and start an App-visible task")
    add_connection_args(create)
    add_message_args(create)
    add_session_config_args(create)
    add_wait_args(create)
    create.add_argument("--title", required=True)
    create.set_defaults(cwd=os.getcwd())

    fork = subparsers.add_parser("fork", help="fork, name, and start an App-visible task")
    add_connection_args(fork)
    add_message_args(fork)
    add_session_config_args(fork)
    add_wait_args(fork)
    source = fork.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-thread", help="source task id")
    source.add_argument("--from-name", help="source task name, resolved like --name")
    fork.add_argument("--last-turn")
    fork.add_argument("--title", required=True)
    fork.set_defaults(cwd=os.getcwd())

    send = subparsers.add_parser("send", help="steer, start, follow up, or queue a message")
    add_connection_args(send)
    add_message_args(send)
    add_turn_config_args(send)
    add_wait_args(send)
    add_target_args(send)
    send.add_argument(
        "--mode", choices=["followup", "steer", "start", "queue"], default="followup"
    )

    resume = subparsers.add_parser(
        "resume", help="resume an idle task, optionally replacing its loaded Session"
    )
    add_connection_args(resume)
    add_message_args(resume)
    add_session_config_args(resume)
    add_wait_args(resume)
    add_target_args(resume)
    resume.add_argument(
        "--cold-replace",
        action="store_true",
        help=(
            "opt in to replacing a loaded idle Session; required when applying "
            "context-window or auto-compaction overrides to a loaded task"
        ),
    )

    read = subparsers.add_parser("read", help="read task state and recent messages")
    add_connection_args(read)
    add_target_args(read)
    read.add_argument("--turn-limit", type=positive_int, default=1)
    read.add_argument(
        "--include-items",
        action="store_true",
        help="include complete raw turn items instead of only conversation messages",
    )
    read.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="text prints the turns oldest first as a readable transcript",
    )

    wait = subparsers.add_parser(
        "wait", help="read-only: wait for a running turn to finish and report its result"
    )
    add_connection_args(wait)
    add_target_args(wait)
    wait.add_argument("--turn", help="wait for this turn id")
    wait.add_argument(
        "--next",
        action="store_true",
        help="when no turn is running, wait for the next turn to start and finish",
    )
    wait.add_argument(
        "--timeout",
        type=timeout_seconds,
        default=DEFAULT_WAIT_TIMEOUT,
        help="seconds to wait; 0 waits without a limit (default 3600)",
    )
    wait.add_argument("--format", choices=["json", "text"], default="json")
    wait.add_argument(
        "--max-chars",
        type=positive_int,
        default=2000,
        help="text format: truncate the last agent message to this many characters",
    )

    listing = subparsers.add_parser(
        "list", help="list tasks across all model providers, newest first"
    )
    add_connection_args(listing)
    listing.add_argument("--search", help="App Server search term, such as part of a task name")
    listing.add_argument("--cwd", help="keep only tasks whose working directory is exactly this path")
    listing.add_argument(
        "--provider",
        action="append",
        help="keep only this model provider id; repeat for several (default: all providers)",
    )
    listing.add_argument("--archived", action="store_true", help="list archived tasks instead")
    listing.add_argument("--limit", type=positive_int, default=30)
    listing.add_argument("--format", choices=["json", "text"], default="json")
    return parser


async def run(args: argparse.Namespace) -> dict[str, Any]:
    args.socket = args.socket.expanduser().resolve()
    # Names resolve first: resume bindings are keyed by the resolved thread id.
    await resolve_targets(args)
    if args.command in {"create", "fork", "resume"}:
        session_profiles.prepare(args)
    if args.command == "doctor":
        return await command_doctor(args)
    if args.command == "list":
        return await command_list(args)
    if args.command == "create":
        result = await create_or_fork(args, fork=False)
    elif args.command == "fork":
        result = await create_or_fork(args, fork=True)
    elif args.command == "resume":
        result = await command_resume(args)
    elif args.command == "send":
        result = await command_send(args)
    elif args.command == "read":
        result = await command_read(args)
    elif args.command == "wait":
        result = await command_wait(args)
    else:
        raise AssertionError(args.command)
    if getattr(args, "resolved_by_name", None):
        result["resolvedByName"] = args.resolved_by_name
    if getattr(args, "resolved_source_by_name", None):
        result["resolvedSourceByName"] = args.resolved_source_by_name
    return result


def outcome_exit_code(command: str, result: dict[str, Any]) -> int:
    """Map the awaited turn's outcome (`wait` result or `--wait` terminal) to an exit code."""
    outcome = result if command == "wait" else result.get("terminal")
    if not isinstance(outcome, dict):
        return EXIT_OK
    if outcome.get("timedOut"):
        return EXIT_WAIT_TIMEOUT
    status = outcome.get("status")
    if status == "failed":
        return EXIT_TURN_FAILED
    if status == "interrupted":
        return EXIT_TURN_INTERRUPTED
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    adjust: Callable[[argparse.Namespace], None] | None = None,
) -> int:
    """Run the CLI; ``adjust`` lets a wrapper amend parsed arguments before any RPC."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if adjust is not None:
            adjust(args)
        result = asyncio.run(run(args))
    except (ChannelError, OSError, ValueError) as exc:
        # Provider RPC errors may echo configuration; never emit secret values.
        error = str(exc)
        def redact(value):
            nonlocal error
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str) and any(word in key.lower() for word in ('token', 'key', 'authorization', 'secret')):
                        error = error.replace(item, '[REDACTED]') if item else error
                    else:
                        redact(item)
        redact(getattr(args, 'session_overrides', {}))
        if isinstance(exc, ValueError):
            error = 'Invalid session configuration (check TOML and supported fields)' if 'TOML' in type(exc).__name__ else error
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    code = outcome_exit_code(args.command, result)
    if getattr(args, "format", "json") == "text":
        if getattr(args, "include_items", False):
            print("note: --include-items is omitted from text output; use --format json", file=sys.stderr)
        print(render_text(args, result))
        return code
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
