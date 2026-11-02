#!/usr/bin/env python3
"""Codex App task channel for agents that run outside the Codex app.

Accepts the same commands and options as the main helper. Every command that
sends a message also requires --sender NAME, and the message is prefixed with a
line naming that sender, so the receiving task does not mistake it for the
user or for another Codex task.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The main helper lives in the sibling skill of the same checkout. Resolve the
# real path so this works when the skill directory is reached through a link.
MAIN_SCRIPTS = Path(__file__).resolve().parents[2] / "codex-app-task-channel" / "scripts"
sys.path.insert(0, str(MAIN_SCRIPTS))
import task_channel  # noqa: E402

SENDING_COMMANDS = {"create", "fork", "send", "resume"}
SENDER_HELP = (
    "--sender NAME is required for create, fork, send, and resume; "
    "the message is prefixed with a line naming the sender."
)


def sender_header(sender: str) -> str:
    return f"[From {sender}, an agent outside the Codex app, via codex-app-task-channel.]\n\n"


def split_sender(argv: list[str]) -> tuple[str | None, list[str]]:
    sender: str | None = None
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--sender":
            if index + 1 >= len(argv):
                raise ValueError("--sender needs a value")
            sender = argv[index + 1]
            index += 2
            continue
        if item.startswith("--sender="):
            sender = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    return sender, rest


def fail(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sender, rest = split_sender(argv)
    except ValueError as exc:
        return fail(str(exc))
    command = rest[0] if rest and not rest[0].startswith("-") else None
    if "-h" in rest or "--help" in rest:
        print(SENDER_HELP)
    elif command in SENDING_COMMANDS:
        if not sender or not sender.strip():
            return fail(f"{command}: {SENDER_HELP} Example: --sender 'Claude Code'")
        if any(item == "--source-thread" or item.startswith("--source-thread=") for item in rest):
            return fail("--source-thread names a Codex source task; external senders use --sender")
    elif sender is not None:
        return fail("--sender applies only to create, fork, send, and resume")

    def adjust(args: argparse.Namespace) -> None:
        if command in SENDING_COMMANDS:
            body = task_channel.load_message(args)
            args.message = sender_header(sender.strip()) + body
            args.message_file = None

    return task_channel.main(rest, adjust=adjust)


if __name__ == "__main__":
    raise SystemExit(main())
