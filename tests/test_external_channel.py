from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "codex-app-task-channel-external"
    / "scripts"
    / "external_channel.py"
)
SPEC = importlib.util.spec_from_file_location("external_channel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
external_channel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(external_channel)


class ExternalChannelTests(unittest.TestCase):
    def run_main(self, argv):
        captured = {}

        async def fake_run(args):
            captured["args"] = args
            return {"threadId": getattr(args, "thread", None)}

        with (
            mock.patch.object(external_channel.task_channel, "run", side_effect=fake_run),
            mock.patch("sys.stdout"),
            mock.patch("sys.stderr"),
        ):
            code = external_channel.main(argv)
        return code, captured.get("args")

    def test_sending_requires_sender_and_sends_nothing_without_it(self) -> None:
        code, args = self.run_main(["send", "--thread", "t", "--message", "hi"])
        self.assertEqual(code, 2)
        self.assertIsNone(args)

    def test_sender_line_prefixes_the_message(self) -> None:
        code, args = self.run_main(
            ["send", "--thread", "t", "--sender", "Claude Code", "--message", "hi"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(args.message.startswith("[From Claude Code, an agent outside the Codex app"))
        self.assertTrue(args.message.endswith("\n\nhi"))

    def test_read_passes_through_and_rejects_sender(self) -> None:
        code, args = self.run_main(["read", "--thread", "t"])
        self.assertEqual(code, 0)
        self.assertEqual(args.thread, "t")
        code, args = self.run_main(["read", "--thread", "t", "--sender", "Claude Code"])
        self.assertEqual(code, 2)
        self.assertIsNone(args)

    def test_external_sender_cannot_claim_a_codex_source_task(self) -> None:
        code, args = self.run_main(
            ["send", "--thread", "t", "--sender=Claude Code", "--source-thread", "x", "--message", "hi"]
        )
        self.assertEqual(code, 2)
        self.assertIsNone(args)


if __name__ == "__main__":
    unittest.main()
