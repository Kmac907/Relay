#!/usr/bin/env python3
"""Read-only Relay campaign status."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from run import campaign_summary, parse_bugs, parse_tasks


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Show the persisted Relay campaign summary without changing state.")
    result.add_argument("--repo", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = args.repo.resolve()
    state_path, tasks_path, bugs_path = repo / ".relay" / "state.json", repo / "tasks.md", repo / "bugs.md"
    if not state_path.is_file() or not tasks_path.is_file() or not bugs_path.is_file():
        parser().error("no Relay campaign found")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    _, tasks = parse_tasks(tasks_path.read_text(encoding="utf-8"), runtime=True)
    _, bugs = parse_bugs(bugs_path.read_text(encoding="utf-8"))
    lines, next_line = campaign_summary(state, tasks, bugs)
    print("\n".join([*lines, f"NEXT {next_line}"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
