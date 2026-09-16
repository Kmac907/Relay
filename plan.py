#!/usr/bin/env python3
"""Read-only requirements planner for Relay."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import relay_console
import run
from repo import TARGET_AGENTS, create_exclusive

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
PLAN_SCHEMA = {"campaignValidationCommands": list, "tasks": list}


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
    result.add_argument("--task-attempts", type=positive, default=3)
    result.add_argument("--fix-loops", type=nonnegative, default=2)
    result.add_argument("--agent-timeout", type=positive, default=3600, dest="agent_timeout")
    result.add_argument("--format-retries", type=nonnegative, default=2, dest="format_retries")
    result.add_argument("--output", type=Path, help="plan path (default: <repo>/PLAN.md)")
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


def validate_plan(value: object) -> dict:
    result = validate_dict(value, PLAN_SCHEMA, "planning result")
    commands = result["campaignValidationCommands"]
    if not commands or any(not isinstance(command, str) or not command.strip() for command in commands):
        raise ValueError("planning result needs nonempty campaign validation commands")
    return {"campaignValidationCommands": commands, "tasks": validate_tasks({"tasks": result["tasks"]})}


def validate_scout(value: object, expected_scope: str) -> dict:
    try:
        result = validate_dict(value, SCOUT_SCHEMA, "scout result")
    except ValueError as error:
        raise ValueError(f"scout invalid field: {error}") from error
    if result["scope"] != expected_scope:
        raise ValueError(f"scout scope mismatch: expected {expected_scope!r}, got {result['scope']!r}")
    for key in SCOUT_SCHEMA:
        if key == "scope":
            continue
        if any(not isinstance(item, str) for item in result[key]):
            raise ValueError(f"scout invalid field: {key} must contain only strings")
    areas = {item.strip() for item in expected_scope.split(",")}
    for path in result["relevantPaths"]:
        if not valid_relative_path(path) or path.replace("\\", "/").split("/", 1)[0] not in areas:
            raise ValueError(f"scout out-of-scope path: {path!r}")
    return result


def valid_relative_path(value: str) -> bool:
    path = Path(value.replace("\\", "/"))
    return bool(value.strip()) and not path.is_absolute() and ".." not in path.parts


def render_tasks(tasks: list[dict], base_sha: str, requirements_hash: str, task_attempts: int = 3, fix_loops: int = 2, campaign_validation_commands: list[str] | None = None) -> str:
    lines = ["# Tasks", "", f"<!-- relay: planned-base={base_sha} requirements={requirements_hash} -->", ""]
    if campaign_validation_commands is not None:
        if not campaign_validation_commands or any(not isinstance(command, str) or not command.strip() for command in campaign_validation_commands):
            raise ValueError("campaign validation commands must not be empty")
        lines += ["## Campaign validation", "", *[f"- `{command}`" for command in campaign_validation_commands], ""]
    for task in tasks:
        dependencies = ", ".join(task["dependencies"]) or "none"
        lines += [
            f"## {task['id']} — {task['title']}", "",
            f"- Status: {task['status']}", f"- Priority: {task['priority']}",
            f"- Dependencies: {dependencies}", "- Allowed paths:",
            *[f"  - `{item}`" for item in task["allowedPaths"]],
            "- Acceptance criteria:", *[f"  - {item}" for item in task["acceptanceCriteria"]],
            "- Validation:", *[f"  - `{item}`" for item in task["validationCommands"]],
            f"- Attempt: 0/{task_attempts}", f"- Fix loop: 0/{fix_loops}", "- Branch: pending",
            "- Pull request: pending", "- Candidate: pending", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def json_schema(properties: dict[str, type], array_name: str | None = None) -> dict:
    def prop(kind: type) -> dict:
        return {"type": "array", "items": {"type": "string"}} if kind is list else {"type": "string"}
    item = {"type": "object", "properties": {k: prop(v) for k, v in properties.items()}, "required": list(properties), "additionalProperties": False}
    return {"type": "object", "properties": {array_name: {"type": "array", "items": item}}, "required": [array_name], "additionalProperties": False} if array_name else item


def planning_schema() -> dict:
    task = json_schema(TASK_SCHEMA)
    return {
        "type": "object",
        "properties": {
            "campaignValidationCommands": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "tasks": {"type": "array", "items": task},
        },
        "required": ["campaignValidationCommands", "tasks"],
        "additionalProperties": False,
    }


def scout_schema(scope: str) -> dict:
    schema = json_schema(SCOUT_SCHEMA)
    schema["properties"]["scope"]["enum"] = [scope]
    return schema


def command(tool: str) -> list[str]:
    parts = shlex.split(os.environ.get(f"RELAY_{tool.upper()}", tool), posix=os.name != "nt")
    if os.name == "nt" and parts:
        parts[0] = shutil.which(parts[0]) or parts[0]
    return parts


def git(repo: Path, *args: str) -> str:
    return subprocess.run(command("git") + ["-C", str(repo), *args], check=True, capture_output=True, encoding="utf-8", errors="replace", timeout=60).stdout.strip()


def progress(event: str, detail: str) -> None:
    relay_console.emit(event, detail)


def inspect_repository(repo: Path) -> tuple[str, list[str], str]:
    root = repo.resolve()
    if not root.is_dir():
        raise ValueError(f"repository does not exist: {root}")
    base = git(root, "rev-parse", "HEAD")
    files = [line for line in git(root, "ls-files").splitlines() if line]
    agents = root / "AGENTS.md"
    if os.path.lexists(agents):
        instructions = agents.read_text(encoding="utf-8") if agents.is_file() else ""
    else:
        instructions = TARGET_AGENTS
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


def create_scout_snapshot(repo: Path, files: list[str], scope: str, destination: Path) -> Path:
    """Copy only tracked files assigned to one scout into its private view."""
    areas = {item.strip() for item in scope.split(",")}
    destination.mkdir()
    root = repo.resolve()
    for relative in files:
        normalized = relative.replace("\\", "/")
        if normalized.split("/", 1)[0] not in areas:
            continue
        source = root / relative
        resolved = source.resolve()
        if root not in resolved.parents or source.is_symlink() or not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


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


def invoke_agent(repo: Path, prompt: str, schema: dict, timeout: int, budget: CallBudget | None, wait_detail: str | None = None) -> object:
    if budget is not None:
        budget.consume()
    with tempfile.TemporaryDirectory(prefix="relay-plan-") as temporary:
        root = Path(temporary)
        schema_path, result_path = root / "schema.json", root / "result.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        in_git_worktree = any((parent / ".git").exists() for parent in (repo, *repo.parents))
        invocation = command("codex") + [
            "exec", *([] if in_git_worktree else ["--skip-git-repo-check"]),
            "--ephemeral", "--sandbox", "read-only", "--cd", str(repo),
            "--output-schema", str(schema_path), "--output-last-message", str(result_path), "-",
        ]
        if wait_detail:
            relay_console.update(f"{wait_detail} | elapsed 0s / {timeout}s")
        completed = subprocess.run(invocation, input=prompt, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        if completed.returncode:
            raise RuntimeError(f"agent failed with exit code {completed.returncode}")
        if not result_path.is_file():
            raise RuntimeError("agent returned no result")
        return json.loads(result_path.read_text(encoding="utf-8"))


def invoke_validated(repo: Path, prompt: str, schema: dict, validator, timeout: int, budget: CallBudget, retries: int, identity: str = "role=agent"):
    error = None
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            call = budget.consume()
        except RuntimeError as caught:
            error = caught
            break
        detail = f"operation={identity.removeprefix('role=')} attempt={attempt}/{attempts} call={call}/{budget.limit} timeout={timeout}s"
        started = time.monotonic()
        progress("START", detail)
        try:
            result = validator(invoke_agent(repo, prompt, schema, timeout, None, detail))
            progress("DONE", f"{detail} elapsed={time.monotonic() - started:.1f}s")
            return result
        except (ValueError, json.JSONDecodeError, RuntimeError, OSError, subprocess.TimeoutExpired) as caught:
            error = caught
            reason = "invalid JSON result" if isinstance(caught, json.JSONDecodeError) else "agent timed out" if isinstance(caught, subprocess.TimeoutExpired) else str(caught).splitlines()[0]
            event = "RETRY" if attempt < attempts and budget.started < budget.limit else "FAILED"
            progress(event, f"{detail} reason={reason} elapsed={time.monotonic() - started:.1f}s")
            if event == "FAILED":
                break
    raise RuntimeError(f"agent did not return valid structured output: {error}") from error


def scout_prompt(scope: str, requirements: str, instructions: str) -> str:
    return f"""Role: Repository Scout (read-only).
Inspect only this fixed scope: {scope}
The returned JSON scope value must equal exactly this assigned scope string: {scope}
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
Return at least one campaignValidationCommands entry: a full build, full test suite, lint, or
repository-wide analyzer that must pass on the untouched base and every accepted candidate.
Keep task validation focused on each task's new behavior. Never promote task commands merely
because they exist; if no honest baseline command exists, fail instead of inventing one.
Base SHA: {base}
Target instructions:\n{instructions}
Tracked tree:\n{chr(10).join(files)}
Scout evidence:\n{json.dumps(evidence)}
Requirements:\n{requirements}
Return only {{\"campaignValidationCommands\": [\"...\"], \"tasks\": [...]}} matching the supplied schema."""


def plan_digest(tasks: list[dict], campaign_validation_commands: list[str] | None = None) -> str:
    value: object = tasks if campaign_validation_commands is None else {"campaignValidationCommands": campaign_validation_commands, "tasks": tasks}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_review_prompt(role: str, requirements: str, instructions: str, tasks: list[dict], digest: str, campaign_validation_commands: list[str] | None = None) -> str:
    focus = (
        "Check requirement coverage, task boundaries, dependencies, allowed paths, acceptance criteria, campaign/task validation separation, and consistency."
        if role == "contract-reviewer" else
        "Audit technical feasibility against the repository, especially whether campaign commands pass on the untouched base, exact validation command syntax, target-platform behavior, and paths needed to satisfy each task."
    )
    return f"""Role: {role} (read-only plan review).
Assignment ID: PLAN
Candidate SHA: {digest}
{focus}
The draft below is the exact contract used to create both PLAN.md and tasks.md.
Return only execution-blocking, evidence-backed plan defects; do not report style preferences,
edit files, widen requirements, spawn agents, or request another review.
Target instructions:\n{instructions}
Requirements:\n{requirements}
Campaign validation commands:\n{json.dumps(campaign_validation_commands or [])}
Draft tasks:\n{json.dumps(tasks)}
Return only the supplied JSON schema."""


def plan_repair_prompt(requirements: str, instructions: str, files: list[str], base: str, tasks: list[dict], findings: list[dict], campaign_validation_commands: list[str] | None = None) -> str:
    return f"""Role: Planning Project Manager (repair, read-only).
Repair only the supplied findings and return the complete revised task graph.
Do not edit files, spawn agents, widen requirements, create another review, or omit unaffected tasks.
Every validation command must use syntax supported by the target environment and every task must allow all paths required by its acceptance criteria.
Return at least one campaign validation command that must pass on the untouched base and every candidate; keep task-specific regression commands separate.
Base SHA: {base}
Target instructions:\n{instructions}
Tracked tree:\n{chr(10).join(files)}
Requirements:\n{requirements}
Campaign validation commands:\n{json.dumps(campaign_validation_commands or [])}
Draft tasks:\n{json.dumps(tasks)}
Plan findings:\n{json.dumps(findings)}
Return only {{"campaignValidationCommands": ["..."], "tasks": [...]}} matching the supplied schema."""


def plan_verification_prompt(requirements: str, original: object, revised: object, findings: list[dict], digest: str) -> str:
    return f"""Role: verification-reviewer (read-only plan verification).
Assignment ID: PLAN
Candidate SHA: {digest}
Verify only that every supplied finding is resolved in the revised plan. Do not reopen full review,
find new issues, edit files, spawn agents, or request another pass.
Requirements:\n{requirements}
Original tasks:\n{json.dumps(original)}
Revised tasks:\n{json.dumps(revised)}
Findings:\n{json.dumps(findings)}
Return resolved, unresolved, or invalid-result using the supplied JSON schema."""


def reviewed_plan(repo: Path, requirements: str, instructions: str, files: list[str], base: str, tasks: list[dict], timeout: int, budget: CallBudget, retries: int, campaign_validation_commands: list[str] | None = None):
    digest = plan_digest(tasks, campaign_validation_commands)
    findings = []
    for role in ("contract-reviewer", "risk-reviewer"):
        operation = "plan-review" if role == "contract-reviewer" else "plan-audit"
        result = invoke_validated(
            repo, plan_review_prompt(role, requirements, instructions, tasks, digest, campaign_validation_commands), run.ROLE_JSON_SCHEMAS[role],
            lambda value, expected=role: run.validate_agent_result(expected, value, "PLAN"), timeout, budget, retries, f"role={operation}",
        )
        if result["candidateSha"] != digest:
            raise ValueError(f"{role} changed plan digest")
        findings.extend(result["findings"])
    if not findings:
        progress("DONE", "operation=plan-audit result=approved")
        return tasks if campaign_validation_commands is None else (campaign_validation_commands, tasks)
    progress("START", f"operation=plan-repair findings={len(findings)}")
    revised = invoke_validated(
        repo, plan_repair_prompt(requirements, instructions, files, base, tasks, findings, campaign_validation_commands), planning_schema() if campaign_validation_commands is not None else json_schema(TASK_SCHEMA, "tasks"),
        validate_plan if campaign_validation_commands is not None else validate_tasks, timeout, budget, retries, "role=planning-pm-repair",
    )
    revised_commands, revised_tasks = (revised["campaignValidationCommands"], revised["tasks"]) if campaign_validation_commands is not None else (None, revised)
    revised_digest = plan_digest(revised_tasks, revised_commands)
    verification = invoke_validated(
        repo, plan_verification_prompt(requirements, {"campaignValidationCommands": campaign_validation_commands, "tasks": tasks} if campaign_validation_commands is not None else tasks, revised, findings, revised_digest), run.ROLE_JSON_SCHEMAS["verification-reviewer"],
        lambda value: run.validate_agent_result("verification-reviewer", value, "PLAN"), timeout, budget, retries, "role=verification-reviewer",
    )
    if verification["candidateSha"] != revised_digest:
        raise ValueError("verification reviewer changed plan digest")
    if verification["status"] != "resolved":
        raise RuntimeError(f"plan repair verification {verification['status']}")
    progress("DONE", f"operation=plan-repair result=verified findings={len(findings)}")
    return revised_tasks if campaign_validation_commands is None else (revised_commands, revised_tasks)


def shell_join(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def emit_plan_summary(output_path: Path, repo: Path, commands: list[str], tasks: list[dict], args: argparse.Namespace) -> None:
    progress("SUMMARY", f"campaign-validation={len(commands)}")
    for task in sorted(tasks, key=lambda item: item["id"]):
        acceptance = " ".join(task["acceptanceCriteria"][0].split())
        progress("SUMMARY", f"- {task['id']} {task['status']}: {task['title']} — {acceptance}")
    counts = {status: sum(task["status"] == status for task in tasks) for status in ("ready", "blocked", "satisfied")}
    progress("SUMMARY", f"ready={counts['ready']} blocked={counts['blocked']} satisfied={counts['satisfied']} plan={output_path}")
    command = shell_join([
        sys.executable, str((Path(__file__).resolve().parent / "run.py").resolve()), "--repo", str(repo), "--plan", str(output_path),
        "--workers", str(args.workers), "--task-attempts", str(args.task_attempts), "--fix-loops", str(args.fix_loops),
    ])
    progress("NEXT", f"Review the generated plan, then execute it with: {command}")


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
        budget = CallBudget(len(scopes) + 5 + args.format_retries)
        progress("START", f"operation=plan name=Relay Planner workers={args.workers} calls={budget.limit}")
        evidence = []
        if scopes:
            with tempfile.TemporaryDirectory(prefix="relay-scouts-") as temporary, ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = []
                for slot, scope in enumerate(scopes, 1):
                    relay_console.update(f"scout {slot}/{len(scopes)} | scope={scope} | calls={budget.started}/{budget.limit}")
                    snapshot = create_scout_snapshot(repo, files, scope, Path(temporary) / f"scope-{slot}")
                    prompt = scout_prompt(scope, requirements, instructions)
                    futures.append(pool.submit(invoke_validated, snapshot, prompt, scout_schema(scope), lambda value, expected=scope: validate_scout(value, expected), args.agent_timeout, budget, args.format_retries, f"role=scout slot={slot}"))
                evidence = [future.result() for future in futures]
        relay_console.update(f"planning-pm synthesize | calls={budget.started}/{budget.limit}")
        planned = invoke_validated(repo, planning_prompt(requirements, instructions, files, base, evidence), planning_schema(), validate_plan, args.agent_timeout, budget, args.format_retries, "role=planning-pm")
        commands, tasks = reviewed_plan(repo, requirements, instructions, files, base, planned["tasks"], args.agent_timeout, budget, args.format_retries, planned["campaignValidationCommands"])
        progress("DONE", f"operation=validate-plan tasks={len(tasks)}")
        output = render_tasks(tasks, base, hashlib.sha256(requirements.encode()).hexdigest()[:12], args.task_attempts, args.fix_loops, commands)
        output_path = (args.output or repo / "PLAN.md").resolve()
        if not create_exclusive(output_path, output):
            raise ValueError(f"refusing existing plan: {output_path}")
        create_exclusive(repo / "AGENTS.md", TARGET_AGENTS)
        progress("COMPLETE", f"operation=plan tasks={len(tasks)} ready={sum(task['status'] == 'ready' for task in tasks)} blocked={sum(task['status'] == 'blocked' for task in tasks)}")
        print(output_path)
        try:
            emit_plan_summary(output_path, repo, commands, tasks, args)
        except Exception as error:
            try:
                progress("SUMMARY", f"unavailable reason={str(error).splitlines()[0]}")
            except Exception:
                print(f"SUMMARY unavailable: {str(error).splitlines()[0]}", file=sys.stderr)
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        progress("FAILED", f"operation=plan reason={str(error).splitlines()[0]}")
        return 1
    finally:
        relay_console.close()


if __name__ == "__main__":
    raise SystemExit(main())
