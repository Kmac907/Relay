#!/usr/bin/env python3
"""Read-only requirements planner for Relay."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
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


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Create a bounded Relay task plan.")
    result.add_argument("--repo", required=True, type=Path)
    result.add_argument("--requirements", required=True, type=Path)
    result.add_argument("--workers", type=positive, default=3)
    result.add_argument("--agent-timeout", type=positive, default=3600, dest="agent_timeout")
    result.add_argument("--format-retries", type=nonnegative, default=2, dest="format_retries")
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
        if not task["allowedPaths"] or not task["acceptanceCriteria"] or not task["validationCommands"]:
            raise ValueError("task contracts must not be empty")
        if any(not valid_relative_path(path) for path in task["allowedPaths"]):
            raise ValueError("allowed paths must stay relative to the repository")
        if set(task["dependencies"]) - known or task["id"] in task["dependencies"]:
            raise ValueError("unknown or self dependency")
    graph, visiting, visited = {task["id"]: task["dependencies"] for task in tasks}, set(), set()
    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("cyclic task dependency")
        if task_id not in visited:
            visiting.add(task_id)
            for dependency in graph[task_id]: visit(dependency)
            visiting.remove(task_id); visited.add(task_id)
    for task_id in ids: visit(task_id)
    return tasks


def validate_scout(value: object, expected_scope: str) -> dict:
    result = validate_dict(value, SCOUT_SCHEMA, "scout result")
    if result["scope"] != expected_scope or any(not isinstance(item, str) for key in SCOUT_SCHEMA if key != "scope" for item in result[key]):
        raise ValueError("scout widened scope or returned invalid evidence")
    return result


def valid_relative_path(value: str) -> bool:
    path = Path(value.replace("\\", "/"))
    return bool(value.strip()) and not path.is_absolute() and ".." not in path.parts


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


def command(tool: str) -> list[str]:
    return shlex.split(os.environ.get(f"RELAY_{tool.upper()}", tool), posix=os.name != "nt")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(command("git") + ["-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=60).stdout.strip()


def progress(event: str, detail: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {event:<10} {detail}", file=sys.stderr, flush=True)


def inspect_repository(repo: Path) -> tuple[str, list[str], str]:
    root = repo.resolve()
    if not root.is_dir():
        raise ValueError(f"repository does not exist: {root}")
    base = git(root, "rev-parse", "HEAD")
    files = [line for line in git(root, "ls-files").splitlines() if line]
    instructions = (root / "AGENTS.md").read_text(encoding="utf-8") if (root / "AGENTS.md").is_file() else ""
    return base, files, instructions


def scout_scopes(files: list[str], workers: int) -> list[str]:
    if len(files) <= 10:
        return []
    areas = sorted({item.replace("\\", "/").split("/", 1)[0] for item in files})
    count = min(workers, len(areas))
    groups = [[] for _ in range(count)]
    for index, area in enumerate(areas):
        groups[index % count].append(area)
    return [", ".join(group) for group in groups]


class CallBudget:
    """One in-process counter; planning restarts are user actions."""
    def __init__(self, limit: int):
        self.started = 0
        self.limit = limit
        self.lock = threading.Lock()

    def consume(self) -> int:
        with self.lock:
            if self.started >= self.limit:
                raise RuntimeError("planning call limit exhausted")
            self.started += 1
            return self.started


def invoke_agent(repo: Path, prompt: str, schema: dict, timeout: int, budget: CallBudget) -> object:
    budget.consume()
    with tempfile.TemporaryDirectory(prefix="relay-plan-") as temporary:
        root = Path(temporary)
        schema_path, result_path = root / "schema.json", root / "result.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        invocation = command("codex") + [
            "exec", "--ephemeral", "--sandbox", "read-only", "--cd", str(repo),
            "--output-schema", str(schema_path), "--output-last-message", str(result_path), "-",
        ]
        completed = subprocess.run(invocation, input=prompt, capture_output=True, text=True, timeout=timeout)
        if completed.returncode or not result_path.is_file():
            raise RuntimeError(f"agent failed ({completed.returncode}): {completed.stderr[-500:]}")
        return json.loads(result_path.read_text(encoding="utf-8"))


def invoke_validated(repo: Path, prompt: str, schema: dict, validator, timeout: int, budget: CallBudget, retries: int):
    error = None
    for _ in range(retries + 1):
        try:
            return validator(invoke_agent(repo, prompt, schema, timeout, budget))
        except (ValueError, json.JSONDecodeError, RuntimeError, subprocess.TimeoutExpired) as caught:
            error = caught
            if budget.started >= budget.limit:
                break
    raise RuntimeError(f"agent did not return valid structured output: {error}") from error


def scout_prompt(scope: str, requirements: str, instructions: str) -> str:
    return f"""Role: Repository Scout (read-only).
Inspect only this fixed scope: {scope}
Do not edit, spawn agents, widen scope, or propose a task graph.
Target instructions:\n{instructions}
Requirements:\n{requirements}
Return only the required JSON evidence object."""


def planning_prompt(requirements: str, instructions: str, files: list[str], base: str, evidence: list[dict]) -> str:
    return f"""Role: Planning Project Manager (read-only).
Reconcile requirements with the existing repository. Return bounded tasks only for missing work.
Do not edit, spawn agents, request another pass, or create historical ordering dependencies.
Every task needs a unique TASK-NNNN ID, ready/blocked/satisfied status, P0-P3 priority,
genuine dependencies, allowed paths, explicit acceptance criteria, and validation commands.
Base SHA: {base}
Target instructions:\n{instructions}
Tracked tree:\n{chr(10).join(files)}
Scout evidence:\n{json.dumps(evidence)}
Requirements:\n{requirements}
Return only {{\"tasks\": [...]}} matching the supplied schema."""


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo, requirements_file = args.repo.resolve(), args.requirements.resolve()
    if not requirements_file.is_file():
        parser().error("requirements file must exist")
    requirements = requirements_file.read_text(encoding="utf-8")
    if not requirements.strip():
        parser().error("requirements file must not be empty")
    try:
        base, files, instructions = inspect_repository(repo)
        scopes = scout_scopes(files, args.workers)
        budget = CallBudget(len(scopes) + 1 + args.format_retries)
        print("Relay Planner", file=sys.stderr)
        print(f"Repository:   {repo}\nRequirements: {requirements_file}\nWorkers:      {args.workers} configured", file=sys.stderr)
        evidence = []
        if scopes:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = []
                for slot, scope in enumerate(scopes, 1):
                    progress("SCOUT", f"slot={slot} scope={scope}")
                    prompt = scout_prompt(scope, requirements, instructions)
                    futures.append(pool.submit(invoke_validated, repo, prompt, json_schema(SCOUT_SCHEMA), lambda value, expected=scope: validate_scout(value, expected), args.agent_timeout, budget, args.format_retries))
                evidence = [future.result() for future in futures]
        progress("SYNTHESIZE", "role=planning-pm")
        tasks = invoke_validated(repo, planning_prompt(requirements, instructions, files, base, evidence), json_schema(TASK_SCHEMA, "tasks"), validate_tasks, args.agent_timeout, budget, args.format_retries)
        progress("VALIDATE", f"tasks={len(tasks)}")
        output = render_tasks(tasks, base, hashlib.sha256(requirements.encode()).hexdigest()[:12])
        progress("OUTPUT", f"ready={sum(task['status'] == 'ready' for task in tasks)} blocked={sum(task['status'] == 'blocked' for task in tasks)}")
        sys.stdout.write(output)
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"plan.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
