#!/usr/bin/env python
"""Claude Code PreToolUse hook: run the unit tests before any `git commit`.

Registered in .claude/settings.json for the Bash tool. Claude Code sends
the pending tool call as JSON on stdin; if its command is a git commit we
run `python -m pytest tests -q` first. Exit code 2 blocks the tool call and
hands stderr back to the model, so a failing suite stops the commit and
the failure output becomes the next thing Claude (or a subagent) reads.
Any other command passes straight through.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

COMMIT_RE = re.compile(r"(^|[;&|]\s*)git\s+(?:-\S+\s+)*commit\b")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not COMMIT_RE.search(command):
        return 0

    root = Path(payload.get("cwd") or Path(__file__).resolve().parents[2])
    if not (root / "tests").is_dir():
        root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:warnings"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-40:])
        print("pre-commit hook: unit tests FAILED - commit blocked. Fix the tests, then commit again.\n" + tail,
              file=sys.stderr)
        return 2
    summary = (result.stdout.strip().splitlines() or ["tests passed"])[-1]
    print(f"pre-commit hook: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
