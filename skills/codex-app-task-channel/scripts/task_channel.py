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
import uuid
from pathlib import Path
from typing import Any, Self

try:
    from websockets.legacy.client import unix_connect
except ImportError:  # pragma: no cover - exercised by the runtime dependency check
    unix_connect = None


class ChannelError(RuntimeError):
    """A task-channel operation failed without a safe automatic fallback."""


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
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        if unix_connect is None:
            raise ChannelError(
                "Python dependency 'websockets' is missing; install websockets>=14,<17"
            )
        if not self.socket_path.is_socket():
            raise ChannelError(f"Codex app-server socket not found: {self.socket_path}")
        self._ws = await unix_connect(
            path=str(self.socket_path),
            uri="ws://localhost/rpc",
            compression=None,
            open_timeout=self.timeout,
        )
        self._reader_task = asyncio.create_task(self._reader())
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
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

    async def _reader(self) -> None:
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
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ChannelError(f"app-server connection failed: {exc}"))
            self._pending.clear()

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            )
        )
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
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
        await self._ws.send(json.dumps(message, separators=(",", ":")))

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        result = await self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        return result["thread"]


def default_socket_path() -> Path:
    explicit = os.environ.get("CODEX_APP_SERVER_SOCKET")
    if explicit:
        return Path(explicit).expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return (codex_home / "app-server-control" / "app-server-control.sock").resolve()


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
    if getattr(args, "model", None) or getattr(args, "effort", None):
        raise ChannelError(
            "turn/steer cannot change model or reasoning effort; wait for idle and use start"
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


async def wait_for_terminal(
    client: AppServerClient,
    thread_id: str,
    turn_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        thread = await client.read_thread(thread_id)
        for turn in thread.get("turns", []):
            if turn.get("id") == turn_id and turn.get("status") != "inProgress":
                return {"status": turn.get("status"), "error": turn.get("error")}
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise ChannelError(f"timed out waiting for turn {turn_id}")
        try:
            await asyncio.wait_for(client.notifications.get(), timeout=min(1.0, remaining))
        except TimeoutError:
            pass


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
        "cwd": str(Path(args.cwd).expanduser().resolve()),
        "ephemeral": False,
    }
    if args.effort:
        params["config"] = {"model_reasoning_effort": args.effort}
    return {key: value for key, value in params.items() if value is not None}


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
        await client.request("thread/name/set", {"threadId": thread_id, "name": args.title})
        turn_args = argparse.Namespace(
            thread=thread_id, model=args.model, effort=args.effort, cwd=args.cwd
        )
        message_id = str(uuid.uuid4())
        delivery = await start_turn(client, turn_args, message, message_id)
        receipt: dict[str, Any] = {
            "threadId": thread_id,
            "title": args.title,
            "createdBy": "thread/fork" if fork else "thread/start",
            "sourceThreadId": args.from_thread if fork else None,
            "clientUserMessageId": message_id,
            **delivery,
        }
        if args.wait:
            receipt["terminal"] = await wait_for_terminal(
                client, thread_id, delivery["turnId"], args.wait_timeout
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
    turns = thread.get("turns", [])
    return {
        "threadId": thread.get("id"),
        "name": thread.get("name"),
        "status": thread_status(thread),
        "activeTurnId": active_turn_id(thread),
        "lastTurnId": turns[-1].get("id") if turns else None,
        "lastTurnStatus": turns[-1].get("status") if turns else None,
        "cwd": thread.get("cwd"),
        "source": thread.get("source"),
    }


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
    parser.add_argument("--cwd")


def add_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=300.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check the app-owned endpoint without mutation")
    add_connection_args(doctor)

    create = subparsers.add_parser("create", help="create, name, and start an App-visible task")
    add_connection_args(create)
    add_message_args(create)
    add_turn_config_args(create)
    add_wait_args(create)
    create.add_argument("--title", required=True)
    create.set_defaults(cwd=os.getcwd())

    fork = subparsers.add_parser("fork", help="fork, name, and start an App-visible task")
    add_connection_args(fork)
    add_message_args(fork)
    add_turn_config_args(fork)
    add_wait_args(fork)
    fork.add_argument("--from-thread", required=True)
    fork.add_argument("--last-turn")
    fork.add_argument("--title", required=True)
    fork.set_defaults(cwd=os.getcwd())

    send = subparsers.add_parser("send", help="steer, start, follow up, or queue a message")
    add_connection_args(send)
    add_message_args(send)
    add_turn_config_args(send)
    add_wait_args(send)
    send.add_argument("--thread", required=True)
    send.add_argument(
        "--mode", choices=["followup", "steer", "start", "queue"], default="followup"
    )

    read = subparsers.add_parser("read", help="read compact task state")
    add_connection_args(read)
    read.add_argument("--thread", required=True)
    return parser


async def run(args: argparse.Namespace) -> dict[str, Any]:
    args.socket = args.socket.expanduser().resolve()
    if args.command == "doctor":
        return await command_doctor(args)
    if args.command == "create":
        return await create_or_fork(args, fork=False)
    if args.command == "fork":
        return await create_or_fork(args, fork=True)
    if args.command == "send":
        return await command_send(args)
    if args.command == "read":
        return await command_read(args)
    raise AssertionError(args.command)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except (ChannelError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
