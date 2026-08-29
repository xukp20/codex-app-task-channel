from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "codex-app-task-channel"
    / "scripts"
    / "task_channel.py"
)
SPEC = importlib.util.spec_from_file_location("task_channel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
task_channel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(task_channel)


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        message = task_channel.json.loads(raw)
        if "id" in message:
            await self.incoming.put(
                task_channel.json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"method": message["method"]},
                    }
                )
            )

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value


class RpcRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_reader_routes_concurrent_responses_and_notifications(self) -> None:
        client = task_channel.AppServerClient(Path("/unused"))
        websocket = FakeWebSocket()
        client._ws = websocket
        client._reader_task = asyncio.create_task(client._reader())
        await websocket.incoming.put(
            task_channel.json.dumps(
                {"jsonrpc": "2.0", "method": "thread/started", "params": {"id": "t"}}
            )
        )

        first, second = await asyncio.gather(
            client.request("thread/list", {}), client.request("thread/read", {})
        )
        notification = await asyncio.wait_for(client.notifications.get(), timeout=1)

        self.assertEqual(first, {"method": "thread/list"})
        self.assertEqual(second, {"method": "thread/read"})
        self.assertEqual(notification["method"], "thread/started")
        client._reader_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await client._reader_task


class MessageContractTests(unittest.TestCase):
    def test_delegation_envelope_escapes_payload(self) -> None:
        wrapped = task_channel.delegated_message("a < b & c", "thread<&")
        self.assertIn("thread&lt;&amp;", wrapped)
        self.assertIn("a &lt; b &amp; c", wrapped)

    def test_active_turn_chooses_latest_in_progress_turn(self) -> None:
        thread = {
            "turns": [
                {"id": "old", "status": "completed"},
                {"id": "current", "status": "inProgress"},
            ]
        }
        self.assertEqual(task_channel.active_turn_id(thread), "current")

    def test_steer_rejects_model_change(self) -> None:
        args = task_channel.argparse.Namespace(
            thread="thread", model="gpt-5.6-sol", effort=None, service_tier=None
        )

        async def exercise() -> None:
            with self.assertRaisesRegex(task_channel.ChannelError, "cannot change model"):
                await task_channel.steer_turn(object(), args, "message", "id", "turn")

        asyncio.run(exercise())

    def test_default_socket_respects_codex_home(self) -> None:
        with mock.patch.dict(
            task_channel.os.environ,
            {"CODEX_HOME": "/tmp/codex-home"},
            clear=True,
        ):
            self.assertEqual(
                task_channel.default_socket_path(),
                Path("/tmp/codex-home/app-server-control/app-server-control.sock"),
            )

    def test_resume_omits_empty_config(self) -> None:
        args = task_channel.argparse.Namespace(
            thread="thread",
            model=None,
            effort=None,
            service_tier=None,
            cwd=None,
            context_window=None,
            auto_compact_token_limit=None,
        )
        self.assertEqual(
            task_channel.resume_params(args),
            {"threadId": "thread", "excludeTurns": True},
        )

    def test_resume_maps_explicit_session_config(self) -> None:
        args = task_channel.argparse.Namespace(
            thread="thread",
            model="gpt-5.6-luna",
            effort="high",
            service_tier="default",
            cwd=None,
            context_window=320000,
            auto_compact_token_limit=270000,
        )
        params = task_channel.resume_params(args)
        self.assertEqual(params["model"], "gpt-5.6-luna")
        self.assertEqual(params["serviceTier"], "default")
        self.assertEqual(
            params["config"],
            {
                "model_reasoning_effort": "high",
                "model_context_window": 320000,
                "model_auto_compact_token_limit": 270000,
            },
        )

    def test_expected_effective_context_window_uses_model_limits(self) -> None:
        with mock.patch.object(
            task_channel,
            "model_cache_entry",
            return_value={
                "max_context_window": 872000,
                "effective_context_window_percent": 95,
            },
        ):
            self.assertEqual(
                task_channel.expected_effective_context_window(
                    "gpt-5.6-luna", 320000
                ),
                304000,
            )

    def test_probe_effort_must_differ_from_active_effort(self) -> None:
        with mock.patch.object(
            task_channel,
            "model_cache_entry",
            return_value={
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "medium"},
                    {"effort": "high"},
                ]
            },
        ):
            self.assertEqual(
                task_channel.choose_probe_effort("gpt-5.6-luna", "low"),
                "medium",
            )


class FakeThreadClient:
    def __init__(self, thread: dict[str, object]) -> None:
        self.thread = thread
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def read_thread(self, _thread_id: str) -> dict[str, object]:
        return self.thread

    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.requests.append((method, params))
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/start":
            return {"turn": {"id": "new-turn"}}
        raise AssertionError(method)


class DeliveryModeTests(unittest.IsolatedAsyncioTestCase):
    def args(self) -> object:
        return task_channel.argparse.Namespace(
            thread="thread", model=None, effort=None, service_tier=None, cwd=None
        )

    async def test_followup_steers_active_turn(self) -> None:
        client = FakeThreadClient(
            {"turns": [{"id": "active", "status": "inProgress"}]}
        )
        receipt = await task_channel.followup_turn(
            client, self.args(), "message", "message-id"
        )
        self.assertEqual(receipt, {"delivery": "steer", "turnId": "active"})
        self.assertEqual(client.requests[0][0], "turn/steer")

    async def test_followup_starts_idle_turn(self) -> None:
        client = FakeThreadClient(
            {"turns": [{"id": "done", "status": "completed"}]}
        )
        receipt = await task_channel.followup_turn(
            client, self.args(), "message", "message-id"
        )
        self.assertEqual(receipt, {"delivery": "start", "turnId": "new-turn"})
        self.assertEqual(client.requests[0][0], "turn/start")


if __name__ == "__main__":
    unittest.main()
