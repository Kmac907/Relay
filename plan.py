#!/usr/bin/env python3
"""Read-only requirements planner for Relay."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TASK_ID = re.compile(r"TASK-\d{4}")
SCOUT_SCHEMA = {
    "scope": str, "implemented": list, "missing": list, "conflicts": list,
    "relevantPaths": list, "validationCommands": list, "evidence": list,
}
TASK_SCHEMA = {
    "id": str, "title": str, "status": str, "priority": str,
    "dependencies": list, "allowedPaths": list, "acceptanceCriteria": list,
    "validationCommands": list,
}


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Create a bounded Relay task plan.")
    result.add_argument("--repo", required=True, type=Path)
    result.add_argument("--requirements", required=True, type=Path)
    result.add_argument("--workers", type=positive, default=3)
    result.add_argument("--agent-timeout", type=positive, default=3600, dest="agent_timeout")
    result.add_argument("--format-retries", type=positive, default=2, dest="format_retries")
    return result


def validate_dict(value: object, schema: dict[str, type], label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    missing = schema.keys() - value.keys()
    if missing:
        raise ValueError(f"{label} missing: {', '.join(sorted(missing))}")
    for key, kind in schema.items():
        if not isinstance(value[key], kind):
            raise ValueError(f"{label}.{key} must be {kind.__name__}")
    return value


def validate_tasks(value: object) -> list[dict]:
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        raise ValueError("planning result must contain a tasks list")
    tasks = [validate_dict(item, TASK_SCHEMA, "task") for item in value["tasks"]]
    ids = [task["id"] for task in tasks]
    if len(ids) != len(set(ids)) or any(not TASK_ID.fullmatch(item) for item in ids):
        raise ValueError("task IDs must be unique TASK-NNNN values")
    known = set(ids)
    for task in tasks:
        if task["status"] not in {"ready", "blocked", "satisfied"}:
            raise ValueError("invalid task status")
        if task["priority"] not in {"P0", "P1", "P2", "P3"}:
            raise ValueError("invalid task priority")
        if not all(isinstance(v, str) for key in ("dependencies", "allowedPaths", "acceptanceCriteria", "validationCommands") for v in task[key]):
            raise ValueError("task lists must contain strings")
        if set(task["dependencies"]) - known or task["id"] in task["dependencies"]:
            raise ValueError("unknown or self dependency")
    return tasks


def render_tasks(tasks: list[dict], base_sha: str, requirements_hash: str) -> str:
    lines = ["# Tasks", "", f"<!-- relay: planned-base={base_sha} requirements={requirements_hash} -->", ""]
    for task in tasks:
        dependencies = ", ".join(task["dependencies"]) or "none"
        lines += [
            f"## {task['id']} — {task['title']}", "",
            f"- Status: {task['status']}", f"- Priority: {task['priority']}",
            f"- Dependencies: {dependencies}", "- Allowed paths:",
            *[f"  - `{item}`" for item in task["allowedPaths"]],
            "- Acceptance criteria:", *[f"  - {item}" for item in task["acceptanceCriteria"]],
            "- Validation:", *[f"  - `{item}`" for item in task["validationCommands"]],
            "- Attempt: 0/3", "- Fix loop: 0/2", "- Branch: pending",
            "- Pull request: pending", "- Candidate: pending", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def json_schema(properties: dict[str, type], array_name: str | None = None) -> dict:
    def prop(kind: type) -> dict:
        return {"type": "array", "items": {"type": "string"}} if kind is list else {"type": "string"}
    item = {"type": "object", "properties": {k: prop(v) for k, v in properties.items()}, "required": list(properties), "additionalProperties": False}
    return {"type": "object", "properties": {array_name: {"type": "array", "items": item}}, "required": [array_name], "additionalProperties": False} if array_name else item


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    # The process implementation is added after the contracts are pinned.
    if not args.repo.resolve().is_dir() or not args.requirements.resolve().is_file():
        parser().error("repository and requirements file must exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
