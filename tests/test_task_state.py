import asyncio
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "codex-app-task-channel" / "scripts"))
from task_channel import AppServerClient, ChannelError, TaskStateUncertain, active_turn_id, wait_for_terminal


class FakeClient(AppServerClient):
    def __init__(self, states=("idle", "idle"), turns=None):
        self.states = iter(states)
        self.turns = [] if turns is None else turns
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            assert params == {"threadId": "synthetic", "includeTurns": False}
            return {"thread": {"id": "synthetic", "cwd": "/synthetic", "status": {"type": next(self.states)}}}
        assert method == "thread/turns/list"
        assert params["itemsView"] == "notLoaded" and params["limit"] == 5
        return {"data": self.turns, "nextCursor": None}


class TaskStateTests(unittest.TestCase):
    def test_idle_uses_bounded_metadata_without_full_history(self):
        client = FakeClient(turns=[{"id": "old", "status": "completed"}])
        thread = asyncio.run(client.read_thread("synthetic"))
        self.assertIsNone(active_turn_id(thread))
        self.assertEqual(len(client.calls), 3)
        self.assertFalse(any(method.startswith("turn/") for method, _ in client.calls))

    def test_active_retains_exact_turn_for_steer(self):
        client = FakeClient(states=("active", "active"), turns=[{"id": "current", "status": "inProgress"}])
        self.assertEqual(active_turn_id(asyncio.run(client.read_thread("synthetic"))), "current")

    def test_changed_state_blocks_delivery(self):
        with self.assertRaisesRegex(ChannelError, "changed"):
            asyncio.run(FakeClient(states=("idle", "active")).read_thread("synthetic"))

    def test_inconsistent_or_multiple_active_turns_block_delivery(self):
        cases = [
            (("active", "active"), []),
            (("idle", "idle"), [{"id": "stale", "status": "inProgress"}]),
            (("active", "active"), [{"id": "a", "status": "inProgress"}, {"id": "b", "status": "inProgress"}]),
        ]
        for states, turns in cases:
            with self.subTest(states=states, turns=turns), self.assertRaises(ChannelError):
                asyncio.run(FakeClient(states=states, turns=turns).read_thread("synthetic"))

    def test_unknown_status_blocks_delivery(self):
        with self.assertRaisesRegex(ChannelError, "unsupported"):
            asyncio.run(FakeClient(states=("unknown", "unknown")).read_thread("synthetic"))

    def test_malformed_turn_identity_blocks_delivery(self):
        for turn in [{"status": "inProgress"}, {"id": "", "status": "inProgress"}, {"id": 1, "status": "inProgress"}, {"id": "x", "status": "futureStatus"}]:
            with self.subTest(turn=turn), self.assertRaises(ChannelError):
                asyncio.run(FakeClient(states=("active", "active"), turns=[turn]).read_thread("synthetic"))

    def test_wait_keeps_polling_when_interrupted_page_is_uncertain(self):
        class WaitingClient:
            def __init__(self):
                self.calls = 0
                self.notifications = asyncio.Queue()
                self.notifications.put_nowait({"method": "synthetic-state-change"})

            async def list_turns(self, *_args, **_kwargs):
                self.calls += 1
                return {"data": [{"id": "target", "status": "interrupted" if self.calls == 1 else "completed", "items": []}]}

            async def read_thread(self, _thread):
                raise TaskStateUncertain("active metadata disagrees with interrupted page")

        async def run():
            client = WaitingClient()
            result = await wait_for_terminal(client, "synthetic", "target", 2)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(client.calls, 3)  # two light polls, one full fetch

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
