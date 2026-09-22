#!/usr/bin/env python3
"""Deterministic Relay coordinator."""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import relay_console
from repo import GENERATED_AGENTS_MARKER, TARGET_AGENTS, TARGET_AGENTS_SHA256, create_exclusive

MARKER = re.compile(r"<!-- relay: planned-base=([0-9a-f]{7,64}) requirements=([0-9a-f]{6,64}) -->")
TASK_HEADING = re.compile(r"^## (TASK-\d{4}) — (.+)$")
BUG_HEADING = re.compile(r"^## (BUG-\d{4}) — (.+)$")
BUG_MARKER = re.compile(r"<!-- relay: campaign=([A-Za-z0-9._-]+) repository=([0-9a-f]{12}) -->")
BACKLOG_MARKER = re.compile(r"<!-- relay: backlog campaign=([A-Za-z0-9._-]+) repository=([0-9a-f]{12}) -->")
TASK_FIELDS = ("Status", "Priority", "Dependencies", "Allowed paths", "Acceptance criteria", "Validation", "Attempt", "Fix loop", "Branch", "Pull request", "Candidate")
WORKER_MODES = {"task", "bug", "repair"}
TERMINAL_REVIEW_PHASES = {"approved", "needs-user", "blocked"}
CHILD_LOCK = threading.Lock()
ACTIVE_CHILDREN: set[subprocess.Popen] = set()
STATE_SCHEMA_VERSION = 3
ORIGINAL_PYTHON_USER_SITE = site.getusersitepackages()

AGENT_SCHEMAS = {
    "worker": {"mode": str, "assignmentId": str, "status": str, "candidateSha": str, "validation": list, "summary": str},
    "plan-reviewer": {"assignmentId": str, "candidateSha": str, "findings": list},
    "slice-reviewer": {"assignmentId": str, "candidateSha": str, "findings": list},
    "verification-reviewer": {"assignmentId": str, "candidateSha": str, "status": str},
    "audit-planner": {"scopes": list},
    "audit-worker": {"scopeId": str, "findings": list},
}

FINDING_FIELDS = {"id": str, "severity": str, "location": str, "failure": str, "reproduction": str, "requirement": str, "evidence": str, "candidateIntroduced": bool}
ROLE_PROMPTS = {
    "plan-reviewer": "Review the complete plan once. Trace every acceptance criterion through real production entrypoints and collaborators; reject layer-only decomposition, missing production or integration paths, internal-component fakes, and validation that cannot prove the composed slice works.",
    "slice-reviewer": "Review the complete validated slice once against its acceptance criteria and ownership graph. Disposition every finding as repair, backlog, discard, or needs-user, with a reason and exact repairPaths. Do not inspect unrelated code or defer work that belongs to this slice.",
    "verification-reviewer": "Verify only the accepted blocker and exact repair delta. Return resolved, unresolved, or invalid-result; do not reopen full review.",
    "audit-planner": "Define one finite list of explicit audit scopes with nonempty executable validation commands. Every scopeId must be AUDIT-NNNN, starting at AUDIT-0001. Never request an unrestricted search or another audit.",
    "audit-worker": "Inspect only the assigned finite scope, read-only, and disposition every evidence-backed finding as repair, backlog, discard, or needs-user with exact repairPaths. Reproduction is human-readable evidence; Relay uses the audit scope commands for execution. Do not create more work.",
}


def _json_object(properties: dict[str, object]) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _string_array() -> dict:
    return {"type": "array", "items": {"type": "string"}}


FINDING_JSON = _json_object({
    "id": {"type": "string"}, "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]}, "location": {"type": "string"},
    "failure": {"type": "string"}, "reproduction": {"type": "string"}, "requirement": {"type": "string"},
    "evidence": {"type": "string"}, "candidateIntroduced": {"type": "boolean"},
})
DISPOSITION_FINDING_JSON = _json_object(FINDING_JSON["properties"] | {
    "action": {"type": "string", "enum": ["repair", "backlog", "discard", "needs-user"]},
    "reason": {"type": "string"}, "repairPaths": _string_array(),
})
ROLE_JSON_SCHEMAS = {
    "worker": _json_object({
        "mode": {"type": "string", "enum": ["task", "bug", "repair"]}, "assignmentId": {"type": "string"}, "status": {"type": "string", "enum": ["candidate"]},
        "candidateSha": {"type": "string"},
        "validation": {"type": "array", "items": _json_object({"command": {"type": "string"}, "exitCode": {"type": "integer"}})},
        "summary": {"type": "string"},
    }),
    "plan-reviewer": _json_object({"assignmentId": {"type": "string"}, "candidateSha": {"type": "string"}, "findings": {"type": "array", "items": FINDING_JSON}}),
    "slice-reviewer": _json_object({"assignmentId": {"type": "string"}, "candidateSha": {"type": "string"}, "findings": {"type": "array", "items": DISPOSITION_FINDING_JSON}}),
    "verification-reviewer": _json_object({"assignmentId": {"type": "string"}, "candidateSha": {"type": "string"}, "status": {"type": "string"}}),
    "audit-planner": _json_object({"scopes": {"type": "array", "items": _json_object({"scopeId": {"type": "string", "pattern": "^AUDIT-\\d{4}$"}, "scope": {"type": "string"}, "requirements": _string_array(), "paths": _string_array(), "commands": _string_array(), "completionCondition": {"type": "string"}})}}),
    "audit-worker": _json_object({"scopeId": {"type": "string"}, "findings": {"type": "array", "items": DISPOSITION_FINDING_JSON}}),
}


def bounded_run(command, *, timeout: int, cwd: Path | None = None, check: bool = True, input: str | None = None, shell: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, cwd=cwd, env=env, shell=shell, stdin=subprocess.PIPE if input is not None else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", text=True)
    with CHILD_LOCK:
        ACTIVE_CHILDREN.add(process)
    try:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            error.stdout, error.stderr = process.communicate()
            raise
    finally:
        with CHILD_LOCK:
            ACTIVE_CHILDREN.discard(process)
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    return result


def terminate_children() -> None:
    with CHILD_LOCK:
        children = list(ACTIVE_CHILDREN)
    for process in children:
        with contextlib.suppress(OSError):
            process.kill()


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
    result = argparse.ArgumentParser(
        description="Run or resume a bounded Relay campaign.",
        epilog="needs-user requires a human decision; waiting-provider requires external completion; blocked records an unsafe or repeated operational failure.",
    )
    result.add_argument("--repo", required=True, type=Path)
    result.add_argument("--workers", type=positive, default=3)
    result.add_argument("--fix-loops", type=nonnegative, default=2)
    result.add_argument("--task-attempts", type=positive, default=3)
    result.add_argument("--format-retries", type=nonnegative, default=2)
    result.add_argument("--agent-timeout", type=positive, default=3600)
    result.add_argument("--validation-timeout", type=positive, default=1800)
    result.add_argument("--provider-timeout", type=positive, default=300)
    result.add_argument("--provider-check-timeout", type=positive, default=3600)
    result.add_argument("--provider-attempts", type=positive, default=3)
    result.add_argument("--merge-method", choices=("squash", "merge", "rebase"), default="squash")
    result.add_argument("--plan", type=Path, help="plan path (default for new campaigns: <repo>/PLAN.md)")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--cleanup", action="store_true")
    result.add_argument("--recover", action="store_true", help="preview or confirm resume, grant, and defer recovery")
    result.add_argument("--defer-blocker", action="append", default=[], metavar="BUG-NNNN")
    result.add_argument("--grant-attempt", action="append", default=[], metavar="TASK-NNNN")
    result.add_argument("--confirm", action="store_true")
    return result


def _field(block: list[str], name: str) -> str:
    prefix = f"- {name}:"
    matches = [line[len(prefix):].strip() for line in block if line.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"expected one {name} field")
    return matches[0]


def _sublist(block: list[str], name: str) -> list[str]:
    start = block.index(f"- {name}:") + 1
    values = []
    for line in block[start:]:
        if line.startswith("- "):
            break
        if line.startswith("  - "):
            value = line[4:].strip()
            values.append(value[1:-1] if len(value) > 1 and value[0] == value[-1] == "`" else value)
    return values


def valid_relative_path(value: str) -> bool:
    path = Path(value.replace("\\", "/"))
    return bool(value.strip()) and not path.is_absolute() and ".." not in path.parts


def parse_tasks(text: str, runtime: bool = False) -> tuple[dict, list[dict]]:
    if not text.startswith("# Tasks\n"):
        raise ValueError("plan must start with # Tasks")
    marker = MARKER.search(text)
    if not marker:
        raise ValueError("missing Relay ownership marker")
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if TASK_HEADING.fullmatch(line)]
    campaign_heading = [i for i, line in enumerate(lines) if line == "## Campaign validation"]
    if len(campaign_heading) > 1:
        raise ValueError("expected one Campaign validation section")
    campaign_commands = None
    if campaign_heading:
        start = campaign_heading[0]
        if starts and start > starts[0]:
            raise ValueError("Campaign validation must precede tasks")
        end = starts[0] if starts else len(lines)
        campaign_commands = []
        for line in lines[start + 1:end]:
            if line.startswith("- "):
                command = line[2:].strip()
                campaign_commands.append(command[1:-1] if len(command) > 1 and command[0] == command[-1] == "`" else command)
        if not campaign_commands or any(not command.strip() for command in campaign_commands):
            raise ValueError("Campaign validation must contain nonempty commands")
    tasks = []
    for index, start in enumerate(starts):
        match = TASK_HEADING.fullmatch(lines[start])
        block = lines[start + 1: starts[index + 1] if index + 1 < len(starts) else len(lines)]
        for field in TASK_FIELDS:
            if field in {"Allowed paths", "Acceptance criteria", "Validation"}:
                if f"- {field}:" not in block:
                    raise ValueError(f"missing {field}")
            else:
                _field(block, field)
        dependencies = [] if _field(block, "Dependencies") == "none" else [item.strip() for item in _field(block, "Dependencies").split(",")]
        attempt = re.fullmatch(r"(\d+)/(\d+)", _field(block, "Attempt"))
        fix_loop = re.fullmatch(r"(\d+)/(\d+)", _field(block, "Fix loop"))
        if not attempt or not fix_loop:
            raise ValueError(f"invalid counters for {match.group(1)}")
        tasks.append({
            "id": match.group(1), "title": match.group(2), "status": _field(block, "Status"),
            "priority": _field(block, "Priority"), "dependencies": dependencies,
            "allowedPaths": _sublist(block, "Allowed paths"),
            "acceptanceCriteria": _sublist(block, "Acceptance criteria"),
            "validationCommands": _sublist(block, "Validation"),
            "attempt": int(attempt.group(1)), "attemptLimit": int(attempt.group(2)),
            "fixLoop": int(fix_loop.group(1)), "fixLoopLimit": int(fix_loop.group(2)),
        })
    ids = [task["id"] for task in tasks]
    if not tasks or len(ids) != len(set(ids)):
        raise ValueError("plan needs unique tasks")
    known = set(ids)
    for task in tasks:
        statuses = {"ready", "blocked", "satisfied", "integrated", "needs-user", "waiting-provider"} if runtime else {"ready", "blocked", "satisfied"}
        if task["status"] not in statuses or task["priority"] not in {"P0", "P1", "P2", "P3"}:
            raise ValueError(f"invalid task metadata for {task['id']}")
        if set(task["dependencies"]) - known or task["id"] in task["dependencies"]:
            raise ValueError(f"invalid dependency for {task['id']}")
        if not task["allowedPaths"] or not task["acceptanceCriteria"] or not task["validationCommands"]:
            raise ValueError(f"empty contract for {task['id']}")
        if any(not valid_relative_path(path) for path in task["allowedPaths"]):
            raise ValueError(f"unsafe allowed path for {task['id']}")
        if task["attemptLimit"] <= 0 or task["fixLoopLimit"] < 0 or task["attempt"] > task["attemptLimit"] or task["fixLoop"] > task["fixLoopLimit"]:
            raise ValueError(f"invalid task counters for {task['id']}")
        if not runtime and (task["attempt"] or task["fixLoop"]):
            raise ValueError(f"new plan counters must start at zero for {task['id']}")
    visiting, visited = set(), set()
    graph = {task["id"]: task["dependencies"] for task in tasks}
    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("cyclic task dependency")
        if task_id not in visited:
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)
    for task_id in ids:
        visit(task_id)
    attempt_limits = {task["attemptLimit"] for task in tasks}
    fix_loop_limits = {task["fixLoopLimit"] for task in tasks}
    if len(attempt_limits) != 1 or len(fix_loop_limits) != 1:
        raise ValueError("task limits must be consistent")
    if campaign_commands is None:
        raise ValueError("plan requires a Campaign validation section")
    return {
        "baseSha": marker.group(1), "requirementsHash": marker.group(2),
        "taskAttemptLimit": attempt_limits.pop(), "fixLoopLimit": fix_loop_limits.pop(),
        "campaignValidationCommands": campaign_commands,
    }, tasks


def validate_agent_result(role: str, value: object, assignment_id: str | None = None, mode: str | None = None) -> dict:
    schema = AGENT_SCHEMAS[role]
    if not isinstance(value, dict):
        raise ValueError("agent result must be an object")
    for key, kind in schema.items():
        if key not in value or not isinstance(value[key], kind):
            raise ValueError(f"invalid {role} result field: {key}")
    identity_key = "scopeId" if role == "audit-worker" else "assignmentId"
    if assignment_id is not None and value.get(identity_key) != assignment_id:
        raise ValueError("agent changed assignment ID")
    if role == "worker" and (mode not in WORKER_MODES or value["mode"] != mode or value["status"] != "candidate"):
        raise ValueError("agent changed Worker mode or candidate status")
    if role in {"plan-reviewer", "slice-reviewer", "contract-reviewer", "risk-reviewer", "audit-worker"}:
        ids = []
        for finding in value["findings"]:
            if not isinstance(finding, dict) or any(not isinstance(finding.get(key), kind) for key, kind in FINDING_FIELDS.items()):
                raise ValueError("invalid evidence-backed finding")
            if finding["severity"] not in {"P0", "P1", "P2", "P3"} or any(not finding[key].strip() for key in ("id", "location", "failure", "reproduction", "requirement", "evidence")):
                raise ValueError("invalid finding severity")
            ids.append(finding["id"])
            if role in {"slice-reviewer", "audit-worker"}:
                if not isinstance(finding.get("action"), str) or not isinstance(finding.get("reason"), str) or not finding["reason"].strip() or not isinstance(finding.get("repairPaths"), list) or any(not isinstance(path, str) or not valid_relative_path(path) for path in finding["repairPaths"]):
                    raise ValueError("invalid finding disposition")
                action = finding["action"]
                if action == "repair" and (finding["severity"] not in {"P0", "P1"} or not finding["candidateIntroduced"] or not finding["repairPaths"]):
                    raise ValueError("repair requires a candidate-introduced P0/P1 finding and repair paths")
                if action != "repair" and finding["repairPaths"]:
                    raise ValueError("only repair findings may contain repair paths")
                if action == "backlog" and finding["severity"] != "P2" and finding["candidateIntroduced"]:
                    raise ValueError("backlog requires P2 or pre-existing evidence")
                if action == "discard" and finding["severity"] != "P3" and "unsupported" not in finding["reason"].lower():
                    raise ValueError("discard requires P3 or unsupported evidence")
                if action == "needs-user" and not finding["reason"].strip():
                    raise ValueError("needs-user requires a concrete reason")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate finding ID")
    if role == "verification-reviewer" and value["status"] not in {"resolved", "unresolved", "invalid-result"}:
        raise ValueError("invalid verification status")
    return value


def review_call_limit(fix_loops: int, format_retries: int) -> int:
    return 1 + 2 * fix_loops + format_retries


def legal_review_targets(phase: str, fix_loop_limit: int) -> set[str]:
    if phase == "slice-review":
        return {"approved", "scope-resolution", "needs-user"} | ({"repair-1"} if fix_loop_limit else set())
    if phase == "scope-resolution":
        return {"needs-user"} | ({f"repair-{number}" for number in range(1, fix_loop_limit + 1)})
    match = re.fullmatch(r"repair-(\d+)", phase)
    if match:
        number = int(match.group(1))
        result = {f"verify-{number}", "scope-resolution", "needs-user"} if number <= fix_loop_limit else set()
        if number < fix_loop_limit:
            result.add(f"repair-{number + 1}")
        return result
    match = re.fullmatch(r"verify-(\d+)", phase)
    if match:
        number = int(match.group(1))
        result = {"approved", "needs-user"}
        if number < fix_loop_limit:
            result.add(f"repair-{number + 1}")
        return result
    return set()


def transition_review(session: dict, target: str, fix_loop_limit: int) -> None:
    phase = session["phase"]
    if phase == "slice-review" and target.startswith("repair-"):
        match = re.fullmatch(r"repair-(\d+)", target)
        allowed = bool(match and 1 <= int(match.group(1)) <= fix_loop_limit)
    else:
        allowed = target in legal_review_targets(phase, fix_loop_limit)
    if not allowed:
        raise ValueError(f"illegal review transition: {phase} -> {target}")
    session["phase"] = target


def ready_tasks(tasks: list[dict], integrated: set[str], active_paths: set[str] | None = None) -> list[dict]:
    active_paths = active_paths or set()
    return sorted(
        (task for task in tasks if task["status"] in {"ready", "blocked"} and set(task["dependencies"]) <= integrated and not paths_conflict(task["allowedPaths"], active_paths)),
        key=lambda item: (int(item["priority"][1]), item["id"]),
    )


def paths_conflict(paths: list[str], active: set[str]) -> bool:
    return any(scopes_may_overlap(path, other) for path in paths for other in active)


def normalized_path(value: str) -> str:
    return os.path.normcase(value.strip("/\\")).replace("\\", "/")


def path_has_magic(value: str) -> bool:
    return any(character in value for character in "*?[")


def glob_matches(path: str, pattern: str) -> bool:
    """Match Git-style path segments; ** alone crosses segment boundaries."""
    path_parts, pattern_parts = normalized_path(path).split("/"), normalized_path(pattern).split("/")
    memo: dict[tuple[int, int], bool] = {}
    def match(path_index: int, pattern_index: int) -> bool:
        key = path_index, pattern_index
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(path_index, pattern_index + 1) or (path_index < len(path_parts) and match(path_index + 1, pattern_index))
        else:
            result = path_index < len(path_parts) and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index]) and match(path_index + 1, pattern_index + 1)
        memo[key] = result
        return result
    return match(0, 0)


def allowed_change(path: str, allowed: list[str], directories: set[str] | None = None) -> bool:
    normalized = normalized_path(path)
    known_directories = (
        {normalized_path(item) for item in directories}
        if directories is not None
        else {normalized_path(item) for item in allowed if not path_has_magic(item) and (item.endswith(("/", "\\")) or not Path(item).suffix)}
    )
    for item in allowed:
        scope = normalized_path(item)
        if path_has_magic(scope):
            if glob_matches(normalized, scope):
                return True
        elif normalized == scope or (scope in known_directories and normalized.startswith(scope + "/")):
            return True
    return False


def non_wildcard_prefix(scope: str) -> tuple[str, ...]:
    parts = normalized_path(scope).split("/")
    return tuple(part for part in parts[:next((index for index, part in enumerate(parts) if path_has_magic(part)), len(parts))] if part)


def scopes_may_overlap(left: str, right: str) -> bool:
    left_prefix, right_prefix = non_wildcard_prefix(left), non_wildcard_prefix(right)
    for left_part, right_part in zip(left_prefix, right_prefix):
        if left_part != right_part:
            return False
    if not path_has_magic(left) and not path_has_magic(right):
        left_value, right_value = normalized_path(left), normalized_path(right)
        if left_value == right_value:
            return True
        # A suffix-free literal is conservatively treated as a directory until repository metadata proves otherwise.
        left_dir = left.endswith(("/", "\\")) or not Path(left_value).suffix
        right_dir = right.endswith(("/", "\\")) or not Path(right_value).suffix
        return (left_dir and right_value.startswith(left_value + "/")) or (right_dir and left_value.startswith(right_value + "/"))
    return True


def initial_state(repo: Path, metadata: dict, args: argparse.Namespace) -> dict:
    campaign_commands = list(metadata.get("campaignValidationCommands", []))
    return {
        "schemaVersion": STATE_SCHEMA_VERSION, "campaignId": "", "repository": str(repo.resolve()), "phase": "build",
        "baseSha": metadata["baseSha"], "requirementsHash": metadata.get("requirementsHash", "000000"), "workerLimit": args.workers, "taskAttemptLimit": args.task_attempts,
        "fixLoopLimit": args.fix_loops, "formatRetryAllowance": args.format_retries,
        "agentTimeoutSeconds": args.agent_timeout, "validationTimeoutSeconds": args.validation_timeout,
        "providerTimeoutSeconds": args.provider_timeout, "providerCheckTimeoutSeconds": args.provider_check_timeout,
        "providerAttemptLimit": args.provider_attempts, "mergeMethod": args.merge_method,
        "createdAt": datetime.now(timezone.utc).isoformat(), "heartbeat": datetime.now(timezone.utc).isoformat(),
        "activeProcesses": {}, "worktrees": {}, "candidateShas": {}, "attemptCounters": {}, "validationCommandsStarted": {}, "taskStates": {},
        "auditBugValidationCommands": {},
        "reviewSessions": {}, "pullRequests": {}, "providerAttemptCounters": {}, "providerOperationsStarted": 0, "providerDeadlines": {},
        "auditPlanStarted": False, "auditPlanCompleted": False, "auditDispositionsCompleted": False, "auditCallsStarted": 0,
        "auditCallLimit": 0, "auditScopes": {}, "pendingLedgerOperation": None,
        "targetInstructions": "", "agentsBootstrap": None, "pathDirectories": [],
        "campaignValidationCommands": campaign_commands,
        "baselineValidation": {
            "baseSha": metadata["baseSha"], "commandsHash": commands_hash(campaign_commands),
            "phase": "pending",
            "commandsStarted": 0, "currentCommand": None, "startedAt": None, "deadline": None,
            "completedAt": None, "error": None, "log": None,
        },
    }


def commands_hash(commands: list[str]) -> str:
    return hashlib.sha256(json.dumps(commands, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.02)


class StateStore:
    def __init__(self, path: Path, state: dict):
        self.path, self.state, self.lock = path, state, threading.RLock()

    def save(self) -> None:
        with self.lock:
            self.state["heartbeat"] = datetime.now(timezone.utc).isoformat()
            atomic_write(self.path, json.dumps(self.state, indent=2, sort_keys=True) + "\n")

    def update(self, change) -> None:
        with self.lock:
            change(self.state)
            self.save()


OPERATION_FIELDS = ("operation", "operationStartedAt", "operationDeadline", "validationCommand", "validationPosition", "validationTotal", "validationCategory")
AZURE_REVIEW_POLICY_IDS = {"fa4e907d-c16b-4a4c-9dfa-4906e5d171dd", "fd2167ab-b0be-447a-8ec8-39368250530e"}


def _operation_target(state: dict, assignment_id: str) -> dict | None:
    if assignment_id == "BASELINE":
        return state.get("baselineValidation")
    if assignment_id == "AGENTS":
        return state.get("agentsBootstrap")
    return state.get("taskStates", {}).get(assignment_id)


def _progress_role(operation: str, mode: str | None = None) -> str:
    if mode == "repair":
        return "repair"
    return {
        "worker": "implement",
        "plan-reviewer": "review",
        "slice-reviewer": "review",
        "verification-reviewer": "review",
        "audit-planner": "audit",
        "provider-approve": "publish",
        "provider-checks": "publish",
        "merge-bypass": "merge",
    }.get(operation, operation)


def runtime_progress(state: dict, bugs: list[dict] | None = None) -> str:
    task_states = state.get("taskStates", {})
    total = state.get("taskTotal", len([key for key in task_states if key.startswith("TASK-")]))
    complete = sum(value.get("phase") == "integrated" for key, value in task_states.items() if key.startswith("TASK-"))
    running = [process for process in state.get("activeProcesses", {}).values() if process.get("status") == "running"]
    queued = sum(process.get("status") == "queued" for process in state.get("activeProcesses", {}).values())
    operations: dict[str, set[str]] = {}
    active_assignments = set()
    for process in state.get("activeProcesses", {}).values():
        if process.get("status") not in {"running", "queued"}:
            continue
        assignment_id = process.get("assignmentId", "unknown")
        operations.setdefault(_progress_role(process.get("role", "worker"), process.get("mode")), set()).add(display_assignment(state, assignment_id))
        active_assignments.add(assignment_id)
    bootstrap = state.get("agentsBootstrap")
    if bootstrap and bootstrap.get("operation") and "AGENTS" not in active_assignments:
        operations.setdefault(_progress_role(bootstrap["operation"]), set()).add("AGENTS")
    baseline = state.get("baselineValidation")
    if baseline and baseline.get("operation"):
        operations.setdefault(_progress_role(baseline["operation"]), set()).add("BASELINE")
    for assignment_id, task in task_states.items():
        if task.get("operation") and assignment_id not in active_assignments:
            operations.setdefault(_progress_role(task["operation"]), set()).add(display_assignment(state, assignment_id))
    bug_counts = Counter(bug["status"] for bug in (bugs or []))
    bug_summary = " ".join(f"{status}={bug_counts[status]}" for status in ("active", "needs-user", "waiting-provider", "resolved", "backlog") if bug_counts[status])
    parts = [f"tasks {complete}/{total} integrated", f"bugs {bug_summary or 0}", f"agents {len(running)}/{state.get('workerLimit', 0)}" + (f" (+{queued} queued)" if queued else "")]
    if operations:
        role_order = {role: index for index, role in enumerate(("implement", "review", "audit", "validate", "repair", "publish", "merge"))}
        parts.extend(f"{role} {','.join(sorted(assignment_ids))}" for role, assignment_ids in sorted(operations.items(), key=lambda item: (role_order.get(item[0], len(role_order)), item[0])))
    else:
        parts.append("idle")
    return " | ".join(parts)


def start_operation(store: StateStore, assignment_id: str, operation: str, timeout: float, **fields: object) -> None:
    began = datetime.now(timezone.utc)
    started = began.isoformat()
    deadline = began.timestamp() + timeout
    def change(state: dict) -> None:
        target = _operation_target(state, assignment_id)
        if target is not None:
            target.update(operation=operation, operationStartedAt=started, operationDeadline=deadline, **fields)
            if operation == "provider-checks" and assignment_id != "AGENTS":
                target["phase"] = "provider-checks"
    store.update(change)
    relay_console.update(runtime_progress(store.state, load_bugs(store)))


def clear_operation(store: StateStore, assignment_id: str, operation: str | None = None) -> None:
    def change(state: dict) -> None:
        target = _operation_target(state, assignment_id)
        if target is not None and (operation is None or target.get("operation") == operation):
            for field in OPERATION_FIELDS:
                target.pop(field, None)
    store.update(change)
    relay_console.update(runtime_progress(store.state, load_bugs(store)))


@contextlib.contextmanager
def coordinator_lock(relay: Path):
    lock = relay / "coordinator.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"another Relay coordinator holds {lock}") from error
        locked = True
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        yield lock
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def tool_command(tool: str) -> list[str]:
    parts = shlex.split(os.environ.get(f"RELAY_{tool.upper()}", tool), posix=os.name != "nt")
    if os.name == "nt" and parts:
        parts[0] = shutil.which(parts[0]) or parts[0]
    return parts


def validation_command(command: str) -> list[str]:
    if os.name == "nt":
        return tool_command("pwsh") + ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-c", command]


def run_validation_command(command: list[str], *, timeout: int, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix="relay-validation-") as temporary:
        environment = (os.environ if env is None else env).copy()
        environment.update(TEMP=temporary, TMP=temporary, TMPDIR=temporary)
        return bounded_run(command, cwd=cwd, env=environment, check=False, timeout=timeout)


def require_validation_shell(timeout: int = 30) -> None:
    executable = validation_command("")[0]
    if not os.path.isfile(executable) and not shutil.which(executable):
        raise RuntimeError(f"validation shell is unavailable: {executable}")
    try:
        completed = run_validation_command(validation_command("$null" if os.name == "nt" else ":"), timeout=min(timeout, 30))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"validation shell is unavailable: {executable}") from error
    if completed.returncode:
        raise RuntimeError(f"validation shell is unavailable: {executable}")


def run_tool(tool: str, *args: str, timeout: int, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return bounded_run(tool_command(tool) + list(args), cwd=cwd, check=check, timeout=timeout)


def git(repo: Path, *args: str, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    return run_tool("git", "-C", str(repo), *args, timeout=timeout, check=check)


def safe_within(path: Path, root: Path) -> Path:
    resolved, resolved_root = path.resolve(), root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"path escapes safe root: {resolved}")
    return resolved


def campaign_temp_root(state: dict) -> Path:
    root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees"
    return safe_within(root / state["campaignId"], root)


def assignment_environment(store: StateStore, assignment_id: str, worktree: Path) -> dict[str, str]:
    campaign_root = campaign_temp_root(store.state)
    user_base = safe_within(campaign_root / ".python-user-bases" / assignment_id, campaign_root)
    user_base.mkdir(parents=True, exist_ok=True)
    worktree_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees"
    existing = os.environ.get("PYTHONPATH")
    entries = [str(worktree.resolve() / "src"), ORIGINAL_PYTHON_USER_SITE, *(existing.split(os.pathsep) if existing else [])]
    python_path = []
    seen = set()
    for index, entry in enumerate(entries):
        if not entry:
            continue
        resolved = Path(entry).resolve()
        if index > 1 and (resolved == worktree_root or worktree_root in resolved.parents):
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            python_path.append(entry)
            seen.add(key)
    environment = os.environ.copy()
    environment.update(PYTHONUSERBASE=str(user_base), PYTHONPATH=os.pathsep.join(python_path))
    return environment


def cleanup_assignment_environment(store: StateStore, assignment_id: str) -> None:
    campaign_root = campaign_temp_root(store.state)
    user_base = safe_within(campaign_root / ".python-user-bases" / assignment_id, campaign_root)
    if user_base.exists():
        shutil.rmtree(user_base)


def cleanup_campaign_environment(state: dict) -> None:
    campaign_root = campaign_temp_root(state)
    environments = safe_within(campaign_root / ".python-user-bases", campaign_root)
    if environments.exists():
        shutil.rmtree(environments)


def repository_layout(repo: Path, timeout: int = 300) -> tuple[Path, str]:
    target = repo.resolve()
    root = Path(git(target, "rev-parse", "--show-toplevel", timeout=timeout).stdout.strip()).resolve()
    if target != root and root not in target.parents:
        raise ValueError("repository path is outside its Git worktree")
    relative = target.relative_to(root)
    return root, "" if relative == Path(".") else relative.as_posix()


def repository_path(state: dict, name: str) -> str:
    prefix = state.get("repositoryPrefix", "")
    return f"{prefix}/{name}" if prefix else name


def exclude_relay_files(repo: Path) -> None:
    _, prefix = repository_layout(repo)
    value = Path(git(repo, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    exclude = value if value.is_absolute() else repo.resolve() / value
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    entries = [f"{prefix}/{name}" if prefix else name for name in ("tasks.md", "bugs.md", ".relay/")]
    missing = [entry for entry in entries if entry not in existing.splitlines()]
    if missing:
        atomic_write(exclude, existing + ("" if not existing or existing.endswith("\n") else "\n") + "\n".join(missing) + "\n")


def finding_path(location: str) -> str:
    return re.sub(r":\d.*$", "", location.replace("\\", "/"))


def render_bugs(campaign: str, repo: Path, bugs: list[dict] | None = None) -> str:
    lines = ["# Bugs", "", f"<!-- relay: campaign={campaign} repository={hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]} -->", ""]
    for bug in bugs or []:
        lines += [
            f"## {bug['id']} — {bug['title']}", "", f"- Severity: {bug['severity']}", f"- Status: {bug['status']}",
            f"- Source: {bug['source']}", f"- Source finding: {bug.get('sourceFindingId', bug['id'])}",
            f"- Location: {bug['location']}", f"- Observable failure: {bug['failure']}",
            f"- Reproduction: `{bug['reproduction']}`", f"- Requirement: {bug['requirement']}", f"- Evidence: {bug['evidence']}",
            *([f"- Deferral reason: {bug['deferralReason']}"] if bug.get("deferralReason") else []),
            *([f"- Decision reason: {bug['decisionReason']}"] if bug.get("decisionReason") else []),
            "- Allowed paths:", *[f"  - `{item}`" for item in bug.get("allowedPaths", [finding_path(bug["location"])])],
            f"- Branch: {bug.get('branch', 'pending')}", f"- Pull request: {bug.get('pullRequest', 'pending')}", f"- Candidate: {bug.get('candidate', 'pending')}", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def parse_bugs(text: str) -> tuple[dict, list[dict]]:
    if not text.startswith("# Bugs\n"):
        raise ValueError("bug ledger must start with # Bugs")
    marker = BUG_MARKER.search(text)
    if not marker:
        raise ValueError("missing bug ledger ownership marker")
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if BUG_HEADING.fullmatch(line)]
    bugs = []
    fields = ("Severity", "Status", "Source", "Location", "Observable failure", "Reproduction", "Requirement", "Evidence", "Branch", "Pull request", "Candidate")
    for index, start in enumerate(starts):
        heading = BUG_HEADING.fullmatch(lines[start])
        block = lines[start + 1: starts[index + 1] if index + 1 < len(starts) else len(lines)]
        values = {field: _field(block, field) for field in fields}
        reproduction = values["Reproduction"]
        bugs.append({
            "id": heading.group(1), "title": heading.group(2), "severity": values["Severity"], "status": values["Status"],
            "source": values["Source"], "location": values["Location"], "failure": values["Observable failure"],
            "sourceFindingId": _field(block, "Source finding") if any(line.startswith("- Source finding:") for line in block) else heading.group(1),
            "allowedPaths": [finding_path(item) for item in _sublist(block, "Allowed paths")] if "- Allowed paths:" in block else [finding_path(values["Location"])],
            "reproduction": reproduction[1:-1] if reproduction.startswith("`") and reproduction.endswith("`") else reproduction,
            "requirement": values["Requirement"], "evidence": values["Evidence"], "branch": values["Branch"],
            "deferralReason": _field(block, "Deferral reason") if any(line.startswith("- Deferral reason:") for line in block) else None,
            "decisionReason": _field(block, "Decision reason") if any(line.startswith("- Decision reason:") for line in block) else None,
            "pullRequest": values["Pull request"], "candidate": values["Candidate"],
        })
    ids = [bug["id"] for bug in bugs]
    if len(ids) != len(set(ids)) or any(bug["severity"] not in {"P0", "P1", "P2", "P3"} or bug["status"] not in {"active", "backlog", "resolved", "needs-user", "waiting-provider"} for bug in bugs):
        raise ValueError("invalid bug ledger")
    return {"campaignId": marker.group(1), "repositoryHash": marker.group(2)}, bugs


def _validate_ledger(store: StateStore, name: str, text: str) -> None:
    repo = Path(store.state["repository"])
    if name == "tasks.md":
        metadata, tasks = parse_tasks(text, runtime=True)
        for key in ("baseSha", "requirementsHash", "taskAttemptLimit", "fixLoopLimit"):
            if metadata[key] != store.state[key]:
                raise RuntimeError(f"tasks.md {key} does not match campaign state")
        if "campaignValidationCommands" in store.state and metadata["campaignValidationCommands"] != store.state["campaignValidationCommands"]:
            raise RuntimeError("tasks.md campaign validation commands do not match campaign state")
    elif name == "bugs.md":
        metadata, _ = parse_bugs(text)
        expected = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]
        if metadata != {"campaignId": store.state["campaignId"], "repositoryHash": expected}:
            raise RuntimeError("bugs.md ownership does not match campaign state")
    else:
        raise ValueError(f"unsupported ledger: {name}")


def write_ledger(store: StateStore, name: str, text: str) -> None:
    """Persist a replayable intent before atomically replacing a ledger."""
    with store.lock:
        _validate_ledger(store, name, text)
        operation = {"ledger": name, "content": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
        store.state["pendingLedgerOperation"] = operation
        store.save()
        atomic_write(Path(store.state["repository"]) / name, text)
        store.state["pendingLedgerOperation"] = None
        store.save()


def recover_pending_ledger(store: StateStore) -> None:
    operation = store.state.get("pendingLedgerOperation")
    if operation is None:
        return
    if not isinstance(operation, dict) or set(operation) != {"ledger", "content", "sha256"}:
        raise RuntimeError("invalid pending ledger operation")
    name, text = operation["ledger"], operation["content"]
    if not isinstance(name, str) or not isinstance(text, str) or operation["sha256"] != hashlib.sha256(text.encode()).hexdigest():
        raise RuntimeError("corrupt pending ledger operation")
    _validate_ledger(store, name, text)
    atomic_write(Path(store.state["repository"]) / name, text)
    store.state["pendingLedgerOperation"] = None
    store.save()


def load_tasks(store: StateStore) -> list[dict]:
    path = Path(store.state["repository"]) / "tasks.md"
    text = path.read_text(encoding="utf-8")
    _validate_ledger(store, "tasks.md", text)
    return parse_tasks(text, runtime=True)[1]


def load_bugs(store: StateStore) -> list[dict]:
    path = Path(store.state["repository"]) / "bugs.md"
    text = path.read_text(encoding="utf-8")
    _validate_ledger(store, "bugs.md", text)
    return parse_bugs(text)[1]


def write_bugs(store: StateStore, bugs: list[dict]) -> None:
    write_ledger(store, "bugs.md", render_bugs(store.state["campaignId"], Path(store.state["repository"]), bugs))


def render_backlog(campaign: str, repo: Path, bugs: list[dict]) -> str:
    repository_hash = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]
    lines = [
        "# Relay Backlog", "",
        f"<!-- relay: backlog campaign={campaign} repository={repository_hash} -->", "",
        "Resolve the following verified backlog defects. Plan only work still missing from the repository.", "",
    ]
    for bug in sorted((item for item in bugs if item["status"] == "backlog"), key=lambda item: item["id"]):
        lines += [
            f"## {bug['id']} — {bug['title']}", "",
            f"- Severity: {bug['severity']}", f"- Requirement: {bug['requirement']}",
            f"- Failure: {bug['failure']}", f"- Evidence: {bug['evidence']}", "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def publish_backlog(store: StateStore, bugs: list[dict]) -> Path | None:
    repo = Path(store.state["repository"])
    path = repo / "BACKLOG.md"
    repository_hash = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]
    if os.path.lexists(path):
        if not path.is_file():
            raise RuntimeError(f"refusing user-owned backlog: {path}")
        marker = BACKLOG_MARKER.search(path.read_text(encoding="utf-8"))
        if not marker or marker.group(2) != repository_hash:
            raise RuntimeError(f"refusing user-owned or malformed backlog: {path}")
    backlog = [bug for bug in bugs if bug["status"] == "backlog"]
    if backlog:
        atomic_write(path, render_backlog(store.state["campaignId"], repo, backlog))
        return path
    return None


def update_task_ledger(store: StateStore, assignment_id: str, **values: str) -> None:
    with store.lock:
        path = Path(store.state["repository"]) / "tasks.md"
        text = path.read_text(encoding="utf-8")
        start = text.index(f"## {assignment_id} ")
        next_start = text.find("\n## ", start + 1)
        end = len(text) if next_start < 0 else next_start
        block = text[start:end]
        names = {"status": "Status", "attempt": "Attempt", "fixLoop": "Fix loop", "branch": "Branch", "pullRequest": "Pull request", "candidate": "Candidate"}
        for key, value in values.items():
            label = names[key]
            block, count = re.subn(rf"^- {re.escape(label)}:.*$", f"- {label}: {value}", block, count=1, flags=re.MULTILINE)
            if count != 1:
                raise ValueError(f"ledger field missing: {label}")
        write_ledger(store, "tasks.md", text[:start] + block + text[end:])


def worker_prompt(mode: str, assignment: dict, candidate_sha: str = "", blockers: list[dict] | None = None, previous_failure: str = "") -> str:
    return f"""Role: Worker
Mode: {mode}
Assignment ID: {assignment['id']}
Worker is the only write-capable role. Work only in this assigned worktree.
Do not edit tasks.md, bugs.md, or .relay; do not push, create/merge PRs, change the contract, or spawn agents.
Allowed paths: {json.dumps(assignment['allowedPaths'])}
Acceptance criteria: {json.dumps(assignment['acceptanceCriteria'])}
Validation commands: {json.dumps(assignment['validationCommands'])}
Current candidate: {candidate_sha or 'none'}
Repair blockers: {json.dumps(blockers or [])}
Previous validation failure: {previous_failure or 'none'}
Trace the real production entrypoint and its direct callers and callees. Build the complete assigned
slice, exercise the production composition, and run focused validation before committing one clean
candidate. External processes, networks, clocks, and providers may be faked in tests; internal
production components being integrated may not be replaced with fakes.
Implement only this assignment, run validation, commit the candidate locally, and return the required JSON.
The result status must be the literal string \"candidate\", never \"completed\". The result mode must exactly match {mode}."""


def role_prompt(role: str, assignment: dict, candidate_sha: str, context: object) -> str:
    return f"""Role: {role}
Assignment ID: {assignment['id']}
You are read-only. Do not edit, spawn agents, write ledgers, push, merge, or request a new review session.
{ROLE_PROMPTS[role]}
Candidate SHA: {candidate_sha}
Contract: {json.dumps(assignment)}
Bounded context: {json.dumps(context)}
Return only the required JSON."""


def task_ownership(store: StateStore) -> list[dict]:
    return [
        {key: task[key] for key in ("id", "title", "dependencies", "allowedPaths")}
        for task in load_tasks(store)
    ]


def _consume_agent_call(store: StateStore, assignment_id: str, role: str, mode: str | None, review: bool, audit: bool) -> tuple[int | str, str]:
    result = {}
    def change(state: dict) -> None:
        if review:
            session = state["reviewSessions"][assignment_id]
            if session["reviewCallsStarted"] >= session["reviewCallLimit"]:
                raise RuntimeError("review call limit exhausted")
            session["reviewCallsStarted"] += 1
            number = session["reviewCallsStarted"]
        elif audit:
            if state["auditCallsStarted"] >= state["auditCallLimit"]:
                raise RuntimeError("audit call limit exhausted")
            state["auditCallsStarted"] += 1
            number = state["auditCallsStarted"]
        else:
            count = state["attemptCounters"].get(assignment_id, 0)
            if count < state["taskAttemptLimit"]:
                state["attemptCounters"][assignment_id] = count + 1
                number = state["attemptCounters"][assignment_id]
            else:
                granted = state.get("recoveryAttemptGrants", {}).get(assignment_id, 0)
                started = state.setdefault("recoveryAttemptsStarted", {}).get(assignment_id, 0)
                if started >= granted:
                    raise RuntimeError("implementation attempt limit exhausted")
                state["recoveryAttemptsStarted"][assignment_id] = started + 1
                number = f"recovery-{started + 1}"
        process_id = f"{assignment_id}:{role}:{number}"
        state["activeProcesses"][process_id] = {"assignmentId": assignment_id, "role": role, "mode": mode, "status": "queued", "reservedAt": datetime.now(timezone.utc).isoformat(), "deadlineSeconds": state["agentTimeoutSeconds"]}
        result.update(number=number, process_id=process_id)
    store.update(change)
    if assignment_id.startswith("TASK-") and role == "worker":
        if mode == "repair":
            count = fix_attempts_started(store.state, assignment_id)
            update_task_ledger(store, assignment_id, fixLoop=f"{count}/{store.state['fixLoopLimit']}")
        else:
            count = store.state["attemptCounters"][assignment_id]
            update_task_ledger(store, assignment_id, attempt=f"{count}/{store.state['taskAttemptLimit']}")
    return result["number"], result["process_id"]


def worker_attempt_available(state: dict, assignment_id: str) -> bool:
    return (
        state["attemptCounters"].get(assignment_id, 0) < state["taskAttemptLimit"]
        or state.get("recoveryAttemptsStarted", {}).get(assignment_id, 0) < state.get("recoveryAttemptGrants", {}).get(assignment_id, 0)
    )


def stop_phase(error: object) -> str:
    text = str(error).lower()
    human = (
        "credential", "authentication", "authorization", "not authorized", "permission denied",
        "conflicting requirement", "destructive ambiguity", "outside assignment scope",
        "requires paths outside", "attempt limit exhausted", "fix loop", "budget exhausted", "validation command ",
    )
    return "needs-user" if any(marker in text for marker in human) else "blocked"


def coordinator_failure_identity(error: BaseException, candidate: str) -> str:
    value = f"{type(error).__name__}:{' '.join(str(error).split())}:{candidate}"
    return hashlib.sha256(value.encode()).hexdigest()


def fix_attempts_started(state: dict, assignment_id: str) -> int:
    return state.get("taskStates", {}).get(assignment_id, {}).get("fixAttemptsStarted", 0)


def reserve_fix(store: StateStore, assignment_id: str) -> int:
    reserved = {}
    def consume(state: dict) -> None:
        task_state = state["taskStates"][assignment_id]
        count = task_state.get("fixAttemptsStarted", 0)
        if count >= state["fixLoopLimit"]:
            raise RuntimeError("fix loop budget exhausted")
        task_state["fixAttemptsStarted"] = count + 1
        reserved["number"] = count + 1
    store.update(consume)
    if assignment_id.startswith("TASK-"):
        update_task_ledger(store, assignment_id, fixLoop=f"{reserved['number']}/{store.state['fixLoopLimit']}")
    return reserved["number"]


def invoke_agent(store: StateStore, semaphore: threading.Semaphore, repo: Path, assignment_id: str, role: str, prompt: str, *, mode: str | None = None, review: bool = False, audit: bool = False) -> dict:
    number, process_id = _consume_agent_call(store, assignment_id, role, mode, review, audit)
    log = store.path.parent / "logs" / f"{assignment_id}-{role}-{number}.log"
    schema = store.path.parent / f".{assignment_id}-{role}-{number}.schema.json"
    output = store.path.parent / f".{assignment_id}-{role}-{number}.result.json"
    atomic_write(schema, json.dumps(ROLE_JSON_SCHEMAS[role]))
    command = tool_command("codex") + [
        "exec", "--ephemeral", "--sandbox", "workspace-write" if role == "worker" else "read-only",
        "--cd", str(repo), "--output-schema", str(schema), "--output-last-message", str(output), "-",
    ]
    prompt = f"Target repository instructions:\n{store.state.get('targetInstructions', '')}\n\n{prompt}"
    operation = "worker" if role == "worker" else role
    try:
        with semaphore:
            store.update(lambda state: state["activeProcesses"][process_id].update(status="running", startedAt=datetime.now(timezone.utc).isoformat()))
            start_operation(store, assignment_id, operation, store.state["agentTimeoutSeconds"])
            relay_console.emit("START", f"operation={operation}" + (f" mode={mode}" if mode else "") + f" assignment={assignment_id} call={number} deadline={store.state['agentTimeoutSeconds']}s")
            completed = bounded_run(command, input=prompt, timeout=store.state["agentTimeoutSeconds"], env=assignment_environment(store, assignment_id, repo))
        atomic_write(log, completed.stdout + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else ""))
        if completed.returncode or not output.is_file():
            raise RuntimeError(f"{role} failed with exit code {completed.returncode}; log: {log}")
        result = json.loads(output.read_text(encoding="utf-8"))
        try:
            validated = validate_agent_result(role, result, assignment_id if role != "audit-planner" else None, mode)
        except ValueError as error:
            raise ValueError(f"{error}; log: {log}") from error
        relay_console.emit("DONE", f"operation={operation} assignment={assignment_id} call={number} log={log}")
        return validated
    except subprocess.TimeoutExpired as error:
        atomic_write(log, f"timed out after {store.state['agentTimeoutSeconds']} seconds\n")
        raise RuntimeError(f"{role} timed out; log: {log}") from error
    finally:
        schema.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        store.update(lambda state: state["activeProcesses"].pop(process_id, None))
        clear_operation(store, assignment_id, operation)


def invoke_with_replacements(store: StateStore, semaphore: threading.Semaphore, repo: Path, assignment_id: str, role: str, prompt: str, *, mode: str | None = None, review: bool = False, audit: bool = False, validator=None) -> dict:
    error = None
    attempts = store.state["formatRetryAllowance"] + 1
    for attempt in range(1, attempts + 1):
        try:
            result = invoke_agent(store, semaphore, repo, assignment_id, role, prompt, mode=mode, review=review, audit=audit)
            return validator(result) if validator else result
        except (ValueError, json.JSONDecodeError, RuntimeError) as caught:
            error = caught
            if review:
                session = store.state["reviewSessions"][assignment_id]
                if session["reviewCallsStarted"] >= session["reviewCallLimit"]:
                    break
            elif audit and store.state["auditCallsStarted"] >= store.state["auditCallLimit"]:
                break
            relay_console.emit("RETRY", f"operation={role} assignment={assignment_id} attempt={attempt}/{attempts} reason={str(caught).splitlines()[0]}")
    relay_console.emit("FAILED", f"operation={role} assignment={assignment_id} reason={str(error).splitlines()[0] if error else 'budget exhausted'}")
    raise RuntimeError(f"{role} exhausted structured-output budget: {error}") from error


def create_worktree(store: StateStore, assignment: dict) -> tuple[Path, str]:
    assignment_id = assignment["id"]
    with store.lock:
        existing = store.state["worktrees"].get(assignment_id)
        if existing:
            return Path(existing["path"]), existing["branch"]
        campaign_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / store.state["campaignId"]
        root = safe_within(campaign_root / assignment_id, campaign_root)
        path = safe_within(root / Path(store.state.get("repositoryPrefix", "")), root)
        root.parent.mkdir(parents=True, exist_ok=True)
        branch = f"relay/{store.state['campaignId']}/{assignment_id}"
        repository = Path(store.state["repository"])
        git_provider_with_retries(store, f"{assignment_id}:fetch", repository, "fetch", "origin", "main")
        remote_base = git(repository, "rev-parse", "origin/main", timeout=store.state["providerTimeoutSeconds"], check=False)
        assignment_base = remote_base.stdout.strip() if remote_base.returncode == 0 else store.state["baseSha"]
        created = git(repository, "worktree", "add", "-b", branch, str(root), assignment_base, timeout=store.state["providerTimeoutSeconds"], check=False)
        if created.returncode:
            detail = " ".join((created.stderr or created.stdout).split()) or f"git exited {created.returncode}"
            raise RuntimeError(f"worktree setup failed: {detail}")
        path.mkdir(parents=True, exist_ok=True)
        store.state["worktrees"][assignment_id] = {"path": str(path), "root": str(root), "branch": branch, "baseSha": assignment_base}
        store.save()
        return path, branch


def scope_directories(store: StateStore, scopes: list[str]) -> set[str]:
    known = {normalized_path(item) for item in store.state.get("pathDirectories", [])}
    has_metadata = "pathDirectories" in store.state
    repository = Path(store.state["repository"])
    for scope in scopes:
        if not path_has_magic(scope) and (scope.endswith(("/", "\\")) or (repository / scope).is_dir() or (not has_metadata and not Path(scope).suffix)):
            known.add(normalized_path(scope))
    return known


def completed_dependency_paths(store: StateStore, assignment: dict) -> list[str]:
    if not assignment.get("dependencies"):
        return []
    tasks = {task["id"]: task for task in load_tasks(store)}
    complete = {
        task_id for task_id, task in tasks.items()
        if task["status"] == "satisfied" or store.state.get("taskStates", {}).get(task_id, {}).get("phase") == "integrated"
    }
    result, seen = [], set()
    def visit(task_id: str) -> None:
        if task_id in seen:
            return
        seen.add(task_id)
        task = tasks.get(task_id)
        if not task or task_id not in complete:
            return
        result.extend(task["allowedPaths"])
        for dependency in task["dependencies"]:
            visit(dependency)
    for dependency in assignment.get("dependencies", []):
        visit(dependency)
    return list(dict.fromkeys(result))


def maximum_repair_paths(store: StateStore, assignment: dict) -> list[str]:
    return list(dict.fromkeys([*assignment["allowedPaths"], *completed_dependency_paths(store, assignment)]))


def approved_repair_paths(store: StateStore, assignment: dict, blockers: list[dict]) -> tuple[list[str], list[str]]:
    maximum = maximum_repair_paths(store, assignment)
    required = list(dict.fromkeys(path for bug in blockers for path in bug.get("allowedPaths", [])))
    if not required:
        return maximum, []
    directories = scope_directories(store, maximum)
    outside = [path for path in required if not allowed_change(path, maximum, directories)]
    return required, outside


def with_repair_paths(assignment: dict, paths: list[str]) -> dict:
    return assignment | {"allowedPaths": list(paths)}


def target_git_paths(store: StateStore, worktree: Path, *args: str) -> list[str]:
    changed = [item for item in git(worktree, *args, timeout=store.state["validationTimeoutSeconds"]).stdout.splitlines() if item]
    prefix = store.state.get("repositoryPrefix", "")
    if not prefix:
        return changed
    marker = prefix + "/"
    if any(not item.startswith(marker) for item in changed):
        raise ValueError("candidate changed paths outside target directory")
    return [item[len(marker):] for item in changed]


def target_changes(store: StateStore, worktree: Path, base: str, sha: str) -> list[str]:
    return target_git_paths(store, worktree, "diff", "--name-only", f"{base}..{sha}")


def _output(value: str | bytes | None) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""


def run_validations(store: StateStore, assignment: dict, worktree: Path, category: str = "task", commands: list[str] | None = None, record: dict | None = None) -> None:
    assignment_id = assignment["id"]
    commands = assignment["validationCommands"] if commands is None else commands
    expected_record = store.state["baselineValidation"] if assignment_id == "BASELINE" else store.state["taskStates"][assignment_id]
    record = expected_record if record is None else record
    if record is not expected_record:
        raise ValueError("validation state record mismatch")
    for command_number, command in enumerate(commands, 1):
        started = {}
        def consume(state: dict) -> None:
            key = assignment_id if category == "task" else f"{assignment_id}:{category}"
            count = state["validationCommandsStarted"].get(key, 0) + 1
            state["validationCommandsStarted"][key] = count
            started["number"] = count
            target = state["baselineValidation"] if assignment_id == "BASELINE" else state["taskStates"][assignment_id]
            target.update(
                operation="validate", operationStartedAt=datetime.now(timezone.utc).isoformat(),
                operationDeadline=time.time() + state["validationTimeoutSeconds"], validationCommand=command,
                validationPosition=command_number, validationTotal=len(commands), validationCategory=category,
            )
            if assignment_id == "BASELINE":
                target.update(phase="running", commandsStarted=count, currentCommand=command, startedAt=target.get("startedAt") or datetime.now(timezone.utc).isoformat(), deadline=target["operationDeadline"])
        store.update(consume)
        relay_console.update(runtime_progress(store.state, load_bugs(store)))
        relay_console.emit("START", f"operation=validate category={category} assignment={assignment_id} command={command_number}/{len(commands)} attempt={started['number']} deadline={store.state['validationTimeoutSeconds']}s")
        shell = validation_command(command)
        middle = "" if category == "task" else f"-{category}"
        log = store.path.parent / "logs" / f"{assignment_id}{middle}-validation-{started['number']}.log"
        try:
            completed = run_validation_command(shell, cwd=worktree, timeout=store.state["validationTimeoutSeconds"], env=assignment_environment(store, assignment_id, worktree))
            exit_code, stdout, stderr = str(completed.returncode), completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as error:
            exit_code, stdout, stderr = "timeout", _output(error.stdout), _output(error.stderr)
            atomic_write(log, f"command: {command}\nshell: {json.dumps(shell)}\nexitCode: {exit_code}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
            prefix = "" if category == "task" else f"{category} "
            message = f"{prefix}validation command {command_number} timed out after {store.state['validationTimeoutSeconds']}s; log: {log}"
            def timed_out(state: dict) -> None:
                target = state["baselineValidation"] if assignment_id == "BASELINE" else state["taskStates"][assignment_id]
                target.update(error=message, validationFailure={"category": category, "command": command, "commandHash": hashlib.sha256(command.encode()).hexdigest(), "outcome": "timeout", "requiredExternalChange": "make this command pass without changing the preserved candidate"}, validationLog=str(log))
            store.update(timed_out)
            relay_console.emit("FAILED", f"operation=validate category={category} assignment={assignment_id} command={command_number}/{len(commands)} log={log}")
            clear_operation(store, assignment_id, "validate")
            raise RuntimeError(message) from error
        atomic_write(log, f"command: {command}\nshell: {json.dumps(shell)}\nexitCode: {exit_code}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
        if completed.returncode:
            prefix = "" if category == "task" else f"{category} "
            message = f"{prefix}validation command {command_number} exited with code {completed.returncode}; log: {log}"
            def failed(state: dict) -> None:
                target = state["baselineValidation"] if assignment_id == "BASELINE" else state["taskStates"][assignment_id]
                target.update(error=message, validationFailure={"category": category, "command": command, "commandHash": hashlib.sha256(command.encode()).hexdigest(), "outcome": f"exit:{completed.returncode}", "requiredExternalChange": "make this command pass without changing the preserved candidate"}, validationLog=str(log))
            store.update(failed)
            relay_console.emit("FAILED", f"operation=validate category={category} assignment={assignment_id} command={command_number}/{len(commands)} exit={completed.returncode} log={log}")
            clear_operation(store, assignment_id, "validate")
            raise RuntimeError(message)
        relay_console.emit("DONE", f"operation=validate category={category} assignment={assignment_id} command={command_number}/{len(commands)} log={log}")
        clear_operation(store, assignment_id, "validate")


def candidate_integrity(store: StateStore, assignment: dict, worktree: Path, result: dict, parent_sha: str | None = None) -> str:
    sha = git(worktree, "rev-parse", "HEAD", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()
    if result["candidateSha"] != sha:
        raise ValueError("reported candidate does not equal worktree HEAD")
    dirty = git(worktree, "status", "--porcelain=v1", "--untracked-files=all", timeout=store.state["validationTimeoutSeconds"]).stdout.splitlines()
    if dirty:
        raise ValueError("candidate worktree has uncommitted changes")
    assignment_base = store.state["worktrees"][assignment["id"]]["baseSha"]
    ancestry = git(worktree, "merge-base", "--is-ancestor", assignment_base, sha, timeout=store.state["validationTimeoutSeconds"], check=False)
    if ancestry.returncode:
        raise ValueError("candidate does not descend from expected base")
    if parent_sha:
        ancestry = git(worktree, "merge-base", "--is-ancestor", parent_sha, sha, timeout=store.state["validationTimeoutSeconds"], check=False)
        if sha == parent_sha or ancestry.returncode:
            raise ValueError("validation repair must commit a descendant candidate")
    changed = target_changes(store, worktree, assignment_base, sha)
    allowed = assignment["allowedPaths"]
    outside = sorted(item for item in changed if not allowed_change(item, allowed, scope_directories(store, allowed)))
    if outside:
        raise ValueError(f"candidate changed paths outside assignment scope: {', '.join(outside)}")
    return sha


def validate_candidate(store: StateStore, assignment: dict, worktree: Path, result: dict) -> str:
    sha = candidate_integrity(store, assignment, worktree, result)
    store.update(lambda state: state["taskStates"][assignment["id"]].update(validationShellVersion=3, validationCandidateSha=sha))
    run_validations(store, assignment, worktree, "task")
    campaign_commands = store.state.get("campaignValidationCommands", [])
    if campaign_commands:
        run_validations(store, assignment, worktree, "campaign", campaign_commands)
    def accepted(state: dict) -> None:
        state["candidateShas"][assignment["id"]] = sha
        state["taskStates"][assignment["id"]].update(candidateSha=sha, phase="slice-review")
        state["taskStates"][assignment["id"]].pop("error", None)
        state["taskStates"][assignment["id"]].pop("pendingWorkerSha", None)
        state["taskStates"][assignment["id"]].pop("validationFailure", None)
        state["taskStates"][assignment["id"]].pop("coordinatorFailure", None)
        state["taskStates"][assignment["id"]].pop("coordinatorFailureBlocked", None)
    store.update(accepted)
    relay_console.emit("DONE", f"operation=candidate assignment={assignment['id']} sha={sha[:12]} validation=passed")
    return sha


def clean_validation_candidate(store: StateStore, assignment_id: str, worktree: Path) -> str | None:
    task_state = store.state["taskStates"][assignment_id]
    candidate = task_state.get("validationCandidateSha") or task_state.get("pendingWorkerSha") or store.state.get("reviewSessions", {}).get(assignment_id, {}).get("pendingWorkerSha")
    if not candidate:
        return None
    head = git(worktree, "rev-parse", "HEAD", timeout=store.state["validationTimeoutSeconds"], check=False).stdout.strip()
    dirty = git(worktree, "status", "--porcelain=v1", "--untracked-files=all", timeout=store.state["validationTimeoutSeconds"], check=False).stdout
    return candidate if head == candidate and not dirty else None


def provider_call(store: StateStore, key: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = {}
    def consume(state: dict) -> None:
        count = state["providerAttemptCounters"].get(key, 0)
        if count >= state["providerAttemptLimit"]:
            raise RuntimeError(f"provider attempt limit exhausted: {key}")
        state["providerAttemptCounters"][key] = count + 1
        result["count"] = count + 1
    store.update(consume)
    actual = list(args)
    tool = "az" if store.state.get("provider") == "azure-devops" else "gh"
    try:
        completed = run_tool(tool, *actual, timeout=store.state["providerTimeoutSeconds"], check=False)
    except subprocess.TimeoutExpired:
        log_provider(store, f"{key} timeout={store.state['providerTimeoutSeconds']}s args={actual}")
        raise
    log_provider(store, f"{key} exit={completed.returncode} args={actual}\n{completed.stdout}{completed.stderr}")
    if check and completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, actual, completed.stdout, completed.stderr)
    return completed


def log_provider(store: StateStore, message: str) -> None:
    with store.lock:
        path = store.path.parent / "logs" / "provider.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now(timezone.utc).isoformat()} {message.rstrip()}\n")


def provider_with_retries(store: StateStore, key: str, *args: str) -> subprocess.CompletedProcess:
    error = None
    while store.state["providerAttemptCounters"].get(key, 0) < store.state["providerAttemptLimit"]:
        try:
            return provider_call(store, key, *args)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as caught:
            error = caught
    raise RuntimeError(f"{provider_name(store.state)} operation exhausted attempts: {key}; log: {store.path.parent / 'logs' / 'provider.log'}") from error


def detect_provider(remote: str) -> dict[str, str]:
    """Return normalized provider identity from a supported origin URL."""
    value = remote.strip()
    scp = re.fullmatch(r"(?:[^@\s]+@)?([^:/\s]+):(.+)", value)
    if scp and "://" not in value:
        host, path = scp.group(1).lower(), scp.group(2)
    else:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https", "ssh"}:
            raise ValueError("origin uses an unsupported URL scheme")
        host, path = (parsed.hostname or "").lower(), parsed.path.lstrip("/")
    parts = [unquote(part) for part in path.rstrip("/").split("/") if part]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    if host == "github.com" and len(parts) == 2 and all(parts):
        return {"provider": "github", "githubRepository": "/".join(parts)}
    if host in {"ssh.dev.azure.com", "vs-ssh.visualstudio.com"}:
        if parts[:1] == ["v3"]:
            parts = parts[1:]
        if len(parts) == 3 and all(parts):
            return {"provider": "azure-devops", "azureOrganization": parts[0], "azureProject": parts[1], "azureRepository": parts[2]}
    if host == "dev.azure.com" and len(parts) == 4 and parts[2].lower() == "_git" and all(parts):
        return {"provider": "azure-devops", "azureOrganization": parts[0], "azureProject": parts[1], "azureRepository": parts[3]}
    if host.endswith(".visualstudio.com") and host != "visualstudio.com" and "_git" in [part.lower() for part in parts]:
        marker = [part.lower() for part in parts].index("_git")
        if marker >= 1 and marker + 1 == len(parts) - 1:
            return {"provider": "azure-devops", "azureOrganization": unquote(host[:-len(".visualstudio.com")]), "azureProject": parts[marker - 1], "azureRepository": parts[marker + 1]}
    raise ValueError("origin is not a supported GitHub or Azure DevOps Services repository")


def provider_name(state: dict) -> str:
    return "Azure DevOps" if state.get("provider") == "azure-devops" else "GitHub"


def _azure_organization(state: dict) -> str:
    return f"https://dev.azure.com/{quote(state['azureOrganization'], safe='')}"


def _azure_context(state: dict, *, repository: bool = True, project: bool = True) -> list[str]:
    result = ["--organization", _azure_organization(state)]
    if project:
        result += ["--project", state["azureProject"]]
    if repository:
        result += ["--repository", state["azureRepository"]]
    return result


def normalize_pr(state: dict, data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("provider pull request output must be an object")
    if state.get("provider") == "azure-devops":
        number = data.get("pullRequestId")
        commit = data.get("lastMergeSourceCommit")
        head = commit.get("commitId") if isinstance(commit, dict) else None
        raw_state = str(data.get("status", "")).lower()
        normalized_state = {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}.get(raw_state)
        links = data.get("_links")
        web = links.get("web") if isinstance(links, dict) else None
        url = web.get("href") if isinstance(web, dict) else None
        if not url and isinstance(number, int):
            url = f"{_azure_organization(state)}/{quote(state['azureProject'], safe='')}/_git/{quote(state['azureRepository'], safe='')}/pullrequest/{number}"
    else:
        number, head, normalized_state, url = data.get("number"), data.get("headRefOid"), str(data.get("state", "")).upper(), data.get("url")
    if isinstance(number, bool) or not isinstance(number, int) or not isinstance(url, str) or not url or not isinstance(head, str) or not head or normalized_state not in {"OPEN", "CLOSED", "MERGED"}:
        raise ValueError("invalid provider pull request output")
    return {"number": number, "url": url, "headRefOid": head, "state": normalized_state}


def _provider_json(completed: subprocess.CompletedProcess) -> object:
    value = json.loads(completed.stdout)
    if not isinstance(value, (dict, list)):
        raise ValueError("provider output must be a JSON object or array")
    return value


def pr_discover(store: StateStore, key: str, branch: str) -> list[dict]:
    if store.state.get("provider") == "azure-devops":
        args = ["repos", "pr", "list", "--source-branch", branch, "--status", "all", *_azure_context(store.state), "--output", "json"]
    else:
        args = ["pr", "list", "--head", branch, "--state", "all", "--json", "number,url,headRefOid,state", "--repo", store.state.get("githubRepository", "fake/relay")]
    data = _provider_json(provider_with_retries(store, key, *args))
    if not isinstance(data, list):
        raise ValueError("provider pull request list must be an array")
    return [normalize_pr(store.state, item) for item in data]


def pr_inspect(store: StateStore, key: str, identifier: str | int) -> dict:
    if store.state.get("provider") == "azure-devops":
        args = ["repos", "pr", "show", "--id", str(identifier), *_azure_context(store.state, repository=False, project=False), "--output", "json"]
    else:
        args = ["pr", "view", str(identifier), "--json", "number,url,headRefOid,state", "--repo", store.state.get("githubRepository", "fake/relay")]
    return normalize_pr(store.state, _provider_json(provider_with_retries(store, key, *args)))


def pr_create(store: StateStore, key: str, branch: str, title: str, body: Path) -> dict:
    if store.state.get("provider") == "azure-devops":
        args = ["repos", "pr", "create", "--source-branch", branch, "--target-branch", "main", "--title", title, "--description", body.read_text(encoding="utf-8"), *_azure_context(store.state), "--output", "json"]
        return normalize_pr(store.state, _provider_json(provider_with_retries(store, key, *args)))
    provider_with_retries(store, key, "pr", "create", "--base", "main", "--head", branch, "--title", title, "--body-file", str(body), "--repo", store.state.get("githubRepository", "fake/relay"))
    return pr_inspect(store, key.replace("pr-create", "pr-view"), branch)


def pr_edit(store: StateStore, key: str, number: int, title: str, body: Path) -> None:
    if store.state.get("provider") == "azure-devops":
        provider_with_retries(store, key, "repos", "pr", "update", "--id", str(number), "--title", title, "--description", body.read_text(encoding="utf-8"), *_azure_context(store.state, repository=False, project=False), "--output", "json")
    else:
        provider_with_retries(store, key, "pr", "edit", str(number), "--title", title, "--body-file", str(body), "--repo", store.state.get("githubRepository", "fake/relay"))


def pr_merge(store: StateStore, key: str, number: int, bypass_sha: str | None = None, subject: str | None = None, body: str | None = None) -> None:
    if store.state.get("provider") == "azure-devops":
        args = ["repos", "pr", "update", "--id", str(number), "--status", "completed", "--squash", "true" if store.state["mergeMethod"] == "squash" else "false", "--delete-source-branch", "true"]
        if subject is not None:
            args += ["--merge-commit-message", subject + (f"\n\n{body}" if body else "")]
        provider_with_retries(store, key, *args, *_azure_context(store.state, repository=False, project=False), "--output", "json")
    else:
        args = ["pr", "merge", str(number), f"--{store.state['mergeMethod']}", "--delete-branch", "--repo", store.state.get("githubRepository", "fake/relay")]
        if bypass_sha:
            args += ["--admin", "--match-head-commit", bypass_sha]
        if subject is not None and store.state["mergeMethod"] != "rebase":
            args += ["--subject", subject, "--body", body or ""]
        provider_with_retries(store, key, *args)


def provider_approve(store: StateStore, assignment_id: str, pr: dict, reviewed_sha: str) -> bool:
    if store.state.get("provider") != "azure-devops":
        return True
    session = store.state["reviewSessions"][assignment_id]
    if session.get("providerApprovalSha") == reviewed_sha:
        return True
    log = store.path.parent / "logs" / "provider.log"
    start_operation(store, assignment_id, "provider-approve", store.state["providerTimeoutSeconds"])
    relay_console.emit("START", f"operation=provider-approve assignment={assignment_id} pr={pr['number']} deadline={store.state['providerTimeoutSeconds']}s")
    try:
        completed = provider_with_retries(
            store, f"{assignment_id}:provider-approve:{reviewed_sha}", "repos", "pr", "set-vote",
            "--id", str(pr["number"]), "--vote", "approve", "--organization", _azure_organization(store.state), "--output", "json",
        )
        if not isinstance(_provider_json(completed), dict):
            raise ValueError("Azure DevOps approval output must be an object")
    except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        relay_console.emit("FAILED", f"operation=provider-approve assignment={assignment_id} pr={pr['number']} next=provider-checks log={log}")
        clear_operation(store, assignment_id, "provider-approve")
        return False
    store.update(lambda state: state["reviewSessions"][assignment_id].__setitem__("providerApprovalSha", reviewed_sha))
    relay_console.emit("DONE", f"operation=provider-approve assignment={assignment_id} pr={pr['number']} sha={reviewed_sha[:12]}")
    clear_operation(store, assignment_id, "provider-approve")
    return True


def git_provider_with_retries(store: StateStore, key: str, repo: Path, *args: str) -> subprocess.CompletedProcess:
    last = None
    while store.state["providerAttemptCounters"].get(key, 0) < store.state["providerAttemptLimit"]:
        store.update(lambda state: state["providerAttemptCounters"].__setitem__(key, state["providerAttemptCounters"].get(key, 0) + 1))
        try:
            last = git(repo, *args, timeout=store.state["providerTimeoutSeconds"], check=False)
        except subprocess.TimeoutExpired:
            log_provider(store, f"{key} timeout={store.state['providerTimeoutSeconds']}s git={args}")
            continue
        log_provider(store, f"{key} exit={last.returncode} git={args}\n{last.stdout}{last.stderr}")
        if last.returncode == 0:
            return last
    raise RuntimeError(f"Git provider operation exhausted attempts: {key}; log: {store.path.parent / 'logs' / 'provider.log'}")


def qualified_assignment(state: dict, assignment_id: str) -> str:
    return f"{state['campaignId']}/{assignment_id}"


def display_assignment(state: dict, assignment_id: str) -> str:
    return qualified_assignment(state, assignment_id) if state.get("campaignId") and re.fullmatch(r"(?:TASK|BUG)-\d{4}", assignment_id) else assignment_id


def canonical_pr_metadata(state: dict, assignment: dict, sha: str) -> tuple[str, str, str]:
    qualified = qualified_assignment(state, assignment["id"])
    title = f"{qualified}: {assignment['title']}"
    sections = (
        ("Acceptance criteria", assignment["acceptanceCriteria"]),
        ("Allowed paths", assignment["allowedPaths"]),
        ("Focused validation", assignment["validationCommands"]),
        ("Campaign validation", state.get("campaignValidationCommands", [])),
    )
    lines = [
        f"Campaign: `{state['campaignId']}`", f"Qualified assignment: `{qualified}`",
        f"Current candidate SHA: `{sha}`", "",
    ]
    for heading, values in sections:
        lines += [f"## {heading}", "", *([f"- `{value}`" for value in values] or ["- none"]), ""]
    body = "\n".join(lines).rstrip() + "\n"
    digest = hashlib.sha256((title + "\n" + body).encode()).hexdigest()
    return title, body, digest


def canonical_merge_metadata(state: dict, assignment: dict, sha: str) -> tuple[str, str, str]:
    subject, body = assignment["title"], ""
    return subject, body, hashlib.sha256((subject + "\n" + body).encode()).hexdigest()


def update_pr_metadata(store: StateStore, assignment: dict, pr: dict, sha: str) -> dict:
    assignment_id = assignment["id"]
    if pr["headRefOid"] != sha:
        raise RuntimeError("provider pull request source commit does not match candidate")
    title, content, digest = canonical_pr_metadata(store.state, assignment, sha)
    proof = store.state["taskStates"][assignment_id].get("prMetadata")
    if proof == {"hash": digest, "candidateSha": sha, "title": title}:
        return pr
    body = store.path.parent / f".{assignment_id}-pr.md"
    atomic_write(body, content)
    try:
        pr_edit(store, f"{assignment_id}:pr-edit:{sha}", pr["number"], title, body)
    finally:
        body.unlink(missing_ok=True)
    store.update(lambda state: state["taskStates"][assignment_id].__setitem__("prMetadata", {"hash": digest, "candidateSha": sha, "title": title}))
    return pr


def persist_pr_inspection(store: StateStore, assignment_id: str, pr: dict) -> None:
    def persist(state: dict) -> None:
        task_state = state["taskStates"][assignment_id]
        task_state["pr"] = pr
        state["pullRequests"][assignment_id] = pr
        if pr["headRefOid"] == task_state.get("pushedSha"):
            task_state["publicationProof"] = {"candidateSha": pr["headRefOid"], "providerRecord": pr}
        else:
            task_state.pop("publicationProof", None)
    store.update(persist)


def inspect_pr_head(store: StateStore, assignment_id: str, identifier: str | int, sha: str, key: str) -> dict:
    target = _operation_target(store.state, assignment_id) or {}
    deadline = target.get("operationDeadline", time.time() + store.state["providerTimeoutSeconds"])
    while True:
        pr = pr_inspect(store, key, identifier)
        persist_pr_inspection(store, assignment_id, pr)
        if pr["headRefOid"] == sha:
            return pr
        remaining = deadline - time.time()
        if remaining <= 0:
            raise RuntimeError("provider pull request source commit does not match candidate")
        time.sleep(min(1, remaining))


def inspect_merged_pr(store: StateStore, assignment_id: str, identifier: str | int, sha: str) -> dict:
    target = _operation_target(store.state, assignment_id) or {}
    deadline = target.get("operationDeadline", time.time() + store.state["providerTimeoutSeconds"])
    key = f"{assignment_id}:merge-proof:{sha}"
    while True:
        pr = pr_inspect(store, key, identifier)
        persist_pr_inspection(store, assignment_id, pr)
        if pr["headRefOid"] != sha:
            raise RuntimeError("provider pull request source commit does not match candidate")
        if pr["state"] == "MERGED":
            return pr
        remaining = deadline - time.time()
        if remaining <= 0:
            raise RuntimeError("provider merge proof deadline expired")
        time.sleep(min(1, remaining))


def validate_publication_proof(store: StateStore, assignment: dict, sha: str, *, merged: bool = False) -> dict:
    assignment_id = assignment["id"]
    task_state = store.state["taskStates"][assignment_id]
    title, _body, metadata_hash = canonical_pr_metadata(store.state, assignment, sha)
    metadata = task_state.get("prMetadata")
    proof = task_state.get("publicationProof")
    record = proof.get("providerRecord") if isinstance(proof, dict) else None
    expected = {"hash": metadata_hash, "candidateSha": sha, "title": title}
    if not isinstance(proof, dict) or task_state.get("pushedSha") != sha or metadata != expected or proof.get("candidateSha") != sha:
        raise RuntimeError("provider publication proof does not match reviewed candidate")
    if not isinstance(record, dict) or record.get("headRefOid") != sha or (merged and record.get("state") != "MERGED"):
        raise RuntimeError("provider publication proof does not match reviewed candidate")
    return record


def mark_integrated(store: StateStore, assignment: dict, pr: dict, reviewed_sha: str, provider_status: str) -> None:
    assignment_id = assignment["id"]
    _title, _body, metadata_hash = canonical_pr_metadata(store.state, assignment, reviewed_sha)
    _subject, _merge_body, merge_hash = canonical_merge_metadata(store.state, assignment, reviewed_sha)
    record = validate_publication_proof(store, assignment, reviewed_sha, merged=True)
    if pr.get("number") != record.get("number"):
        raise RuntimeError("provider publication proof does not match reviewed candidate")
    proof = {"finalCandidate": reviewed_sha, "prMetadataHash": metadata_hash, "mergeMetadataHash": merge_hash, "mergedProviderRecord": record}
    store.update(lambda state: (state["taskStates"][assignment_id].update(phase="integrated", merged=True, providerStatus=provider_status, providerProof=proof), state["pullRequests"].__setitem__(assignment_id, record)))


def publish_candidate(store: StateStore, assignment: dict, worktree: Path, branch: str, sha: str) -> dict:
    assignment_id, task_state = assignment["id"], store.state["taskStates"][assignment["id"]]
    start_operation(store, assignment_id, "publish", store.state["providerTimeoutSeconds"])
    pushed = task_state.get("pushedSha") != sha
    if pushed:
        remote = git_provider_with_retries(store, f"{assignment_id}:ls-remote", worktree, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        if not remote.stdout.startswith(sha):
            remote_sha = remote.stdout.split()[0] if remote.stdout.split() else ""
            lease = (f"--force-with-lease=refs/heads/{branch}:{remote_sha}",) if remote_sha else ()
            git_provider_with_retries(store, f"{assignment_id}:push:{sha}", worktree, "push", *lease, "--set-upstream", "origin", branch)
        store.update(lambda state: state["taskStates"][assignment_id].update(pushed=True, pushedSha=sha))
        relay_console.emit("DONE", f"operation=publish assignment={assignment_id} branch={branch}")
    if task_state.get("pr"):
        pr = inspect_pr_head(store, assignment_id, task_state["pr"]["number"], sha, f"{assignment_id}:pr-refresh:{sha}")
        update_pr_metadata(store, assignment, pr, sha)
        validate_publication_proof(store, assignment, sha)
        clear_operation(store, assignment_id, "publish")
        return pr
    body = store.path.parent / f".{assignment_id}-pr.md"
    title, content, _digest = canonical_pr_metadata(store.state, assignment, sha)
    atomic_write(body, content)
    try:
        matches = pr_discover(store, f"{assignment_id}:pr-list", branch)
        matching = [pr for pr in matches if pr["headRefOid"] == sha]
        pr = next((pr for pr in matching if pr["state"] == "OPEN"), matching[0] if matching else None)
        if pr is None and any(pr["state"] == "OPEN" for pr in matches):
            raise RuntimeError("provider pull request source commit does not match candidate")
        if pr is None:
            pr = pr_create(store, f"{assignment_id}:pr-create", branch, title, body)
        pr = inspect_pr_head(store, assignment_id, pr["number"], sha, f"{assignment_id}:pr-refresh:{sha}")
        update_pr_metadata(store, assignment, pr, sha)
        validate_publication_proof(store, assignment, sha)
        relay_console.emit("DONE", f"operation=pull-request assignment={assignment_id} pr={pr.get('number')}")
        clear_operation(store, assignment_id, "publish")
        return pr
    finally:
        body.unlink(missing_ok=True)


def record_findings(store: StateStore, assignment_id: str, findings: list[dict], decisions: list[dict] | None = None) -> list[dict]:
    actions = {item["findingId"]: item["action"] for item in decisions or []}
    reasons = {item["findingId"]: item["reason"] for item in decisions or []}
    accepted = []
    bugs = load_bugs(store)
    known = {(bug["source"], bug["sourceFindingId"]) for bug in bugs}
    for finding in findings:
        action = finding.get("action", actions.get(finding["id"], "discard"))
        action = "accept-blocker" if action == "repair" else action
        reason = finding.get("reason") or reasons.get(finding["id"])
        if action == "accept-blocker" and (finding["severity"] not in {"P0", "P1"} or not finding["candidateIntroduced"] or not all(finding[key] for key in ("location", "failure", "reproduction", "requirement", "evidence"))):
            action = "discard"
        if finding["severity"] == "P2" and action == "accept-blocker":
            action = "backlog"
        if finding["severity"] == "P3":
            action = "discard"
        if action in {"accept-blocker", "backlog", "needs-user"}:
            finding_key = (assignment_id, finding["id"])
            if finding_key not in known:
                bug = {
                    "id": f"BUG-{len(bugs) + 1:04d}", "title": finding["failure"][:80], "severity": finding["severity"],
                    "status": "active" if action == "accept-blocker" else action, "source": assignment_id,
                    "sourceFindingId": finding["id"], "location": finding["location"], "failure": finding["failure"],
                    "reproduction": finding["reproduction"], "requirement": finding["requirement"], "evidence": finding["evidence"],
                    "allowedPaths": list(finding.get("repairPaths") or [finding_path(finding["location"])]),
                }
                if action == "backlog":
                    bug["deferralReason"] = reason or "Reason not recorded by the originating campaign"
                if action == "needs-user":
                    bug["decisionReason"] = reason
                bugs.append(bug)
                known.add(finding_key)
            else:
                bug = next(item for item in bugs if (item["source"], item["sourceFindingId"]) == finding_key)
                bug.update(
                    title=finding["failure"][:80], severity=finding["severity"], status="active" if action == "accept-blocker" else action,
                    location=finding["location"], failure=finding["failure"], reproduction=finding["reproduction"],
                    requirement=finding["requirement"], evidence=finding["evidence"],
                    allowedPaths=list(finding.get("repairPaths") or bug.get("allowedPaths") or [finding_path(finding["location"])]),
                )
                if action == "backlog" and not bug.get("deferralReason"):
                    bug["deferralReason"] = reason or "Reason not recorded by the originating campaign"
                if action == "needs-user" and not bug.get("decisionReason"):
                    bug["decisionReason"] = reason
            if action == "accept-blocker":
                accepted.append(bug)
    write_bugs(store, bugs)
    return accepted


def ensure_review_session(store: StateStore, assignment_id: str, sha: str, assignment: dict | None = None) -> dict:
    if assignment_id not in store.state["reviewSessions"]:
        def create(state: dict) -> None:
            task_state = state["taskStates"][assignment_id]
            audit = (assignment or {}).get("auditFinding")
            base = state["worktrees"].get(assignment_id, {}).get("baseSha", state["baseSha"])
            state["reviewSessions"][assignment_id] = {
                "reviewSessionId": f"{assignment_id}-REVIEW-1", "initialCandidateSha": sha, "reviewedSha": "",
                "phase": "verify-1" if audit else "slice-review", "reviewResult": None,
                "acceptedBlockerIds": [audit["id"]] if audit else [],
                "reviewCallsStarted": 0,
                "reviewCallLimit": review_call_limit(state["fixLoopLimit"], state["formatRetryAllowance"]),
            }
            if audit:
                state["reviewSessions"][assignment_id].update(previousCandidateSha=base, currentCandidateSha=sha, pendingRepairSha=sha, pendingRepairNumber=1, approvedRepairPaths=list(assignment["allowedPaths"]))
        store.update(create)
    return store.state["reviewSessions"][assignment_id]


def run_review(store: StateStore, semaphore: threading.Semaphore, assignment: dict, worktree: Path, sha: str) -> bool:
    assignment_id = assignment["id"]
    session = ensure_review_session(store, assignment_id, sha, assignment)
    try:
        if session["phase"] == "scope-resolution":
            transition_review(session, "needs-user", store.state["fixLoopLimit"])
            store.state["taskStates"][assignment_id]["error"] = "repair paths require explicit scope authorization or replanning"
            store.save()
            return False
        if session["phase"] == "slice-review":
            result = session.get("reviewResult")
            if result is None:
                result = invoke_with_replacements(
                    store, semaphore, worktree, assignment_id, "slice-reviewer",
                    role_prompt("slice-reviewer", assignment, sha, {"taskOwnership": task_ownership(store), "validatedCandidate": sha}),
                    review=True,
                )
                if result["candidateSha"] != sha:
                    raise ValueError("slice reviewer changed candidate SHA")
                store.update(lambda state: state["reviewSessions"][assignment_id].__setitem__("reviewResult", result))
            findings = result["findings"]
            maximum = maximum_repair_paths(store, assignment)
            directories = scope_directories(store, maximum)
            requested = sorted({path for finding in findings if finding["action"] == "repair" for path in finding["repairPaths"]})
            outside = [path for path in requested if not allowed_change(path, maximum, directories)]
            with store.lock:
                accepted = record_findings(store, assignment_id, findings)
            def reviewed(state: dict) -> None:
                current = state["reviewSessions"][assignment_id]
                current["acceptedBlockerIds"] = [item["id"] for item in accepted]
                current["approvedRepairPaths"] = requested
                current["currentCandidateSha"] = sha
                if any(finding["action"] == "needs-user" for finding in findings):
                    target = "needs-user"
                elif outside:
                    target = "scope-resolution"
                elif accepted and fix_attempts_started(state, assignment_id) < state["fixLoopLimit"]:
                    target = f"repair-{fix_attempts_started(state, assignment_id) + 1}"
                elif accepted:
                    target = "needs-user"
                else:
                    target = "approved"
                    current["reviewedSha"] = sha
                    current["finalReviewedSha"] = sha
                transition_review(current, target, state["fixLoopLimit"])
                if target in {"needs-user", "scope-resolution"}:
                    state["taskStates"][assignment_id]["error"] = "review requires a human decision" if target == "needs-user" else f"repair paths outside bounded scope: {', '.join(outside)}"
            store.update(reviewed)
            relay_console.emit("DONE", f"operation=slice-review assignment={assignment_id} repairs={len(accepted)}")
            if session["phase"] == "scope-resolution":
                transition_review(session, "needs-user", store.state["fixLoopLimit"])
                store.save()
                return False
        while session["phase"] not in TERMINAL_REVIEW_PHASES:
            number = int(session["phase"].split("-")[1])
            blockers = [bug for bug in load_bugs(store) if bug["id"] in session["acceptedBlockerIds"]]
            if session["phase"].startswith("repair-"):
                repair_paths = session.get("approvedRepairPaths")
                if repair_paths is None:
                    repair_paths, outside = approved_repair_paths(store, assignment, blockers)
                else:
                    maximum = maximum_repair_paths(store, assignment)
                    directories = scope_directories(store, maximum)
                    outside = [path for path in repair_paths if not allowed_change(path, maximum, directories)]
                if outside:
                    store.state["taskStates"][assignment_id]["error"] = f"accepted blocker requires paths outside assignment scope: {', '.join(outside)}"
                    transition_review(session, "scope-resolution", store.state["fixLoopLimit"])
                    transition_review(session, "needs-user", store.state["fixLoopLimit"])
                    store.save()
                    return False
                if session.get("approvedRepairPaths") != repair_paths:
                    session["approvedRepairPaths"] = repair_paths
                    store.save()
                repair_assignment = with_repair_paths(assignment, repair_paths)
                current_sha = session.get("currentCandidateSha", sha)
                try:
                    if session.get("pendingWorkerSha"):
                        repair = {"candidateSha": session["pendingWorkerSha"]}
                    else:
                        if fix_attempts_started(store.state, assignment_id) >= store.state["fixLoopLimit"]:
                            transition_review(session, "needs-user", store.state["fixLoopLimit"])
                            if not store.state["taskStates"][assignment_id].get("error"):
                                store.state["taskStates"][assignment_id]["error"] = "review requires user"
                            store.save()
                            return False
                        if reserve_fix(store, assignment_id) != number:
                            raise RuntimeError("review fix sequence drifted")
                        repair = invoke_with_replacements(store, semaphore, worktree, assignment_id, "worker", worker_prompt("repair", repair_assignment, current_sha, blockers, store.state["taskStates"][assignment_id].get("error", "")), mode="repair", review=True)
                        if repair.get("summary") is not None:
                            store.state["taskStates"][assignment_id]["workerSummary"] = repair["summary"]
                        session.update(previousCandidateSha=current_sha, pendingWorkerSha=repair["candidateSha"])
                        store.save()
                    candidate_integrity(store, repair_assignment, worktree, repair, current_sha)
                    repaired_sha = validate_candidate(store, repair_assignment, worktree, repair)
                except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
                    task_state = store.state["taskStates"][assignment_id]
                    task_state["error"] = str(error)
                    candidate = clean_validation_candidate(store, assignment_id, worktree)
                    if task_state.get("validationFailure") and candidate:
                        session["currentCandidateSha"] = candidate
                        session.pop("pendingWorkerSha", None)
                    elif candidate:
                        identity = coordinator_failure_identity(error, candidate)
                        previous = task_state.get("coordinatorFailure", {})
                        count = previous.get("count", 0) + 1 if previous.get("identity") == identity else 1
                        task_state["coordinatorFailure"] = {"identity": identity, "count": count, "candidateSha": candidate, "evidence": str(error)}
                        session["pendingWorkerSha"] = candidate
                        if count < 2:
                            store.save()
                            continue
                        task_state["phase"] = session["phase"] = "blocked"
                        store.save()
                        return False
                    else:
                        session.pop("pendingWorkerSha", None)
                    fixes = fix_attempts_started(store.state, assignment_id)
                    target = f"repair-{fixes + 1}" if fixes < store.state["fixLoopLimit"] and session["reviewCallsStarted"] < session["reviewCallLimit"] else "needs-user"
                    transition_review(session, target, store.state["fixLoopLimit"])
                    store.save()
                    continue
                session.update(currentCandidateSha=repaired_sha, pendingRepairSha=repaired_sha, pendingRepairNumber=number)
                session.pop("pendingWorkerSha", None)
                transition_review(session, f"verify-{number}", store.state["fixLoopLimit"])
                store.save()
                continue
            repaired_sha = session["pendingRepairSha"]
            previous_sha = session["previousCandidateSha"]
            verification = invoke_with_replacements(store, semaphore, worktree, assignment_id, "verification-reviewer", role_prompt("verification-reviewer", assignment, repaired_sha, {"blockers": blockers, "previousCandidate": previous_sha, "repairDiff": f"{previous_sha}..{repaired_sha}"}), review=True)
            relay_console.emit("DONE", f"operation=repair assignment={assignment_id} fix={number}/{store.state['fixLoopLimit']} result={verification['status']}")
            if verification["candidateSha"] != repaired_sha:
                raise ValueError("verification reviewer changed candidate SHA")
            if verification["status"] == "resolved":
                transition_review(session, "approved", store.state["fixLoopLimit"])
                session["reviewedSha"] = repaired_sha
                session["finalReviewedSha"] = repaired_sha
                session.pop("pendingRepairSha", None)
                session.pop("pendingRepairNumber", None)
                with store.lock:
                    bugs = load_bugs(store)
                    for bug in blockers:
                        next(item for item in bugs if item["id"] == bug["id"])["status"] = "resolved"
                    write_bugs(store, bugs)
                break
            store.state["taskStates"][assignment_id]["error"] = f"verification {verification['status']}"
            target = f"repair-{number + 1}" if number < store.state["fixLoopLimit"] else "needs-user"
            transition_review(session, target, store.state["fixLoopLimit"])
            session.pop("pendingRepairSha", None)
            session.pop("pendingRepairNumber", None)
            store.save()
        approved = session["phase"] == "approved"
        if not approved and not store.state["taskStates"][assignment_id].get("error"):
            store.state["taskStates"][assignment_id]["error"] = "review requires user"
            store.save()
        return approved
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
        store.state["taskStates"][assignment_id]["error"] = str(error)
        if session["phase"] not in TERMINAL_REVIEW_PHASES:
            session["phase"] = stop_phase(error)
            store.save()
        return False


def wait_for_checks(store: StateStore, assignment_id: str, pr: dict, reviewed_sha: str) -> str:
    if assignment_id not in store.state["providerDeadlines"]:
        store.update(lambda state: state["providerDeadlines"].__setitem__(assignment_id, time.time() + state["providerCheckTimeoutSeconds"]))
    deadline = store.state["providerDeadlines"][assignment_id]
    start_operation(store, assignment_id, "provider-checks", max(0, deadline - datetime.now(timezone.utc).timestamp()), nextAction="poll")

    def progress(status: str, counts: dict[str, int] | None = None, next_action: str = "poll") -> None:
        def change(state: dict) -> None:
            target = _operation_target(state, assignment_id)
            if target is not None:
                target.update(providerStatus=status, providerPolicyCounts=counts or {}, nextAction=next_action)
        store.update(change)
        relay_console.update(runtime_progress(store.state, load_bugs(store)))

    first = True
    while first or time.time() < deadline:
        first = False
        try:
            store.update(lambda state: state.__setitem__("providerOperationsStarted", state.get("providerOperationsStarted", 0) + 1))
            if store.state.get("provider") == "azure-devops":
                arguments = ["repos", "pr", "show", "--id", str(pr["number"]), *_azure_context(store.state, repository=False, project=False), "--output", "json"]
                tool = "az"
            else:
                arguments = ["pr", "view", str(pr["number"]), "--json", "number,url,headRefOid,mergeStateStatus,statusCheckRollup,state", "--repo", store.state.get("githubRepository", "fake/relay")]
                tool = "gh"
            view = run_tool(tool, *arguments, timeout=store.state["providerTimeoutSeconds"], check=False)
            log_provider(store, f"{assignment_id}:check exit={view.returncode} args={arguments}\n{view.stdout}{view.stderr}")
        except (RuntimeError, subprocess.TimeoutExpired):
            key = f"{assignment_id}:check-errors"
            store.update(lambda state: state["providerAttemptCounters"].__setitem__(key, state["providerAttemptCounters"].get(key, 0) + 1))
            progress(f"provider-error {store.state['providerAttemptCounters'][key]}/{store.state['providerAttemptLimit']}")
            if store.state["providerAttemptCounters"][key] >= store.state["providerAttemptLimit"]:
                return "waiting-provider"
            continue
        if view.returncode:
            key = f"{assignment_id}:check-errors"
            store.update(lambda state: state["providerAttemptCounters"].__setitem__(key, state["providerAttemptCounters"].get(key, 0) + 1))
            progress(f"provider-error {store.state['providerAttemptCounters'][key]}/{store.state['providerAttemptLimit']}")
            if store.state["providerAttemptCounters"][key] >= store.state["providerAttemptLimit"]:
                return "waiting-provider"
            continue
        data = _provider_json(view)
        current = normalize_pr(store.state, data) if store.state.get("provider") == "azure-devops" else data
        if assignment_id in store.state.get("taskStates", {}):
            persist_pr_inspection(store, assignment_id, current)
        if current.get("headRefOid") != reviewed_sha:
            progress("sha-drift", next_action="repair")
            return "sha-drift"
        if str(current.get("state", "")).upper() == "MERGED":
            progress("merged", next_action="reconcile")
            return "merged"
        if str(current.get("state", "")).upper() == "CLOSED":
            progress("closed", next_action="user")
            return "failed"
        if store.state.get("provider") == "azure-devops":
            merge_status = str(data.get("mergeStatus", "")).lower()
            if merge_status == "conflicts":
                return "repair-required"
            try:
                store.update(lambda state: state.__setitem__("providerOperationsStarted", state.get("providerOperationsStarted", 0) + 1))
                arguments = ["repos", "pr", "policy", "list", "--id", str(pr["number"]), *_azure_context(store.state, repository=False, project=False), "--output", "json"]
                policies = run_tool("az", *arguments, timeout=store.state["providerTimeoutSeconds"], check=False)
                log_provider(store, f"{assignment_id}:policy exit={policies.returncode} args={arguments}\n{policies.stdout}{policies.stderr}")
                if policies.returncode:
                    raise RuntimeError("Azure DevOps policy inspection failed")
                policy_data = _provider_json(policies)
                if not isinstance(policy_data, list):
                    raise ValueError("Azure DevOps policy output must be an array")
                blocking = []
                reviewer_waiting = []
                for policy in policy_data:
                    if not isinstance(policy, dict):
                        raise ValueError("invalid Azure DevOps policy output")
                    configuration = policy.get("configuration")
                    is_blocking = configuration.get("isBlocking") if isinstance(configuration, dict) else policy.get("isBlocking")
                    if is_blocking is True:
                        status = str(policy.get("status", "")).lower()
                        blocking.append(status)
                        policy_type = configuration.get("type") if isinstance(configuration, dict) else None
                        policy_id = str(policy_type.get("id", "")).lower() if isinstance(policy_type, dict) else ""
                        display_name = str(policy_type.get("displayName", "")).lower() if isinstance(policy_type, dict) else ""
                        if status not in {"approved", "notapplicable"} and (policy_id in AZURE_REVIEW_POLICY_IDS or "reviewer" in display_name):
                            reviewer_waiting.append(status)
            except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
                key = f"{assignment_id}:check-errors"
                store.update(lambda state: state["providerAttemptCounters"].__setitem__(key, state["providerAttemptCounters"].get(key, 0) + 1))
                if store.state["providerAttemptCounters"][key] >= store.state["providerAttemptLimit"]:
                    return "waiting-provider"
                continue
            non_reviewer = list(blocking)
            for status in reviewer_waiting:
                non_reviewer.remove(status)
            if set(non_reviewer) & {"rejected", "broken"} or merge_status == "failure":
                progress("failed", dict(Counter(blocking)), "repair")
                return "failed"
            if reviewer_waiting:
                progress("reviewer-policy-waiting", dict(Counter(blocking)), "external-approval")
                time.sleep(min(10, max(0, deadline - time.time())))
                continue
            if set(blocking) & {"queued", "running"} or merge_status == "queued":
                progress("pending", dict(Counter(blocking)), "poll")
                time.sleep(min(10, max(0, deadline - time.time())))
                continue
            if any(status not in {"approved", "notapplicable"} for status in blocking):
                progress("failed", dict(Counter(blocking)), "user")
                return "failed"
            if merge_status == "rejectedbypolicy":
                progress("rejected-by-policy", dict(Counter(blocking)), "user")
                return "waiting-provider"
            progress("passed", dict(Counter(blocking)), "merge")
            return "passed"
        checks = data.get("statusCheckRollup") or []
        states = {str(item.get("conclusion") or item.get("state") or item.get("status", "")).upper() for item in checks}
        if states & {"FAILURE", "FAILED", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
            progress("failed", dict(Counter(state.lower() for state in states)), "repair")
            return "failed"
        if states & {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED"}:
            progress("pending", dict(Counter(state.lower() for state in states)), "poll")
            time.sleep(min(10, max(0, deadline - time.time())))
            continue
        if data.get("mergeStateStatus") in {"BEHIND", "DIRTY"}:
            progress(str(data.get("mergeStateStatus")).lower(), next_action="repair")
            return "repair-required"
        if data.get("mergeStateStatus") == "UNKNOWN":
            progress("mergeability-unknown", next_action="poll")
            time.sleep(min(10, max(0, deadline - time.time())))
            continue
        if data.get("mergeStateStatus") == "BLOCKED":
            progress("bypassable" if assignment_id != "AGENTS" else "blocked", next_action="merge-bypass" if assignment_id != "AGENTS" else "user")
            return "bypassable" if assignment_id != "AGENTS" else "waiting-provider"
        progress("passed", next_action="merge")
        return "passed"
    target = _operation_target(store.state, assignment_id) or {}
    if target.get("providerStatus") == "reviewer-policy-waiting":
        progress("policy-waiting", next_action="external-approval")
        return "policy-waiting"
    progress("deadline-expired", next_action="resume")
    return "waiting-provider"


def merge_assignment(store: StateStore, semaphore: threading.Semaphore, assignment: dict, worktree: Path, branch: str, pr: dict, reviewed_sha: str) -> bool:
    assignment_id = assignment["id"]
    session = store.state["reviewSessions"][assignment_id]
    merge_subject, merge_body, _merge_hash = canonical_merge_metadata(store.state, assignment, reviewed_sha)
    provider_approve(store, assignment_id, pr, reviewed_sha)
    status = wait_for_checks(store, assignment_id, pr, reviewed_sha)
    while status in {"failed", "repair-required"}:
        if fix_attempts_started(store.state, assignment_id) >= store.state["fixLoopLimit"] or session["reviewCallsStarted"] + 2 > session["reviewCallLimit"]:
            store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user", providerStatus=status))
            relay_console.emit("BLOCKED", f"operation=provider-checks assignment={assignment_id} reason={status} log={store.path.parent / 'logs' / 'provider.log'}")
            clear_operation(store, assignment_id)
            return False
        fix_number = reserve_fix(store, assignment_id)
        blocker = [{"id": f"PROVIDER-{fix_number}", "failure": status, "evidence": f"{provider_name(store.state)} checks or merge readiness failed"}]
        try:
            git_provider_with_retries(store, f"{assignment_id}:repair-fetch:{fix_number}", worktree, "fetch", "origin", "main")
            current_base = git(worktree, "rev-parse", "origin/main", timeout=store.state["providerTimeoutSeconds"], check=False)
            if current_base.returncode == 0:
                store.state["worktrees"][assignment_id]["baseSha"] = current_base.stdout.strip()
                store.save()
            result = invoke_with_replacements(store, semaphore, worktree, assignment_id, "worker", worker_prompt("repair", assignment, reviewed_sha, blocker, store.state["taskStates"][assignment_id].get("error", "")), mode="repair", review=True)
            store.update(lambda state: state["taskStates"][assignment_id].update(**({"workerSummary": result["summary"]} if result.get("summary") is not None else {}), pendingWorkerSha=result["candidateSha"]))
            replacement = validate_candidate(store, assignment, worktree, result)
            pr = publish_candidate(store, assignment, worktree, branch, replacement)
            verification = invoke_with_replacements(store, semaphore, worktree, assignment_id, "verification-reviewer", role_prompt("verification-reviewer", assignment, replacement, {"previousCandidate": reviewed_sha, "repairDiff": f"{reviewed_sha}..{replacement}", "providerFailure": status}), review=True)
            if verification["candidateSha"] != replacement:
                raise ValueError("verification reviewer changed candidate SHA")
        except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            store.update(lambda state: state["taskStates"][assignment_id].update(error=str(error)))
            if fix_attempts_started(store.state, assignment_id) >= store.state["fixLoopLimit"] or session["reviewCallsStarted"] >= session["reviewCallLimit"]:
                store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user", providerStatus=status))
                clear_operation(store, assignment_id)
                return False
            status = "failed"
            continue
        if verification["status"] != "resolved":
            if fix_attempts_started(store.state, assignment_id) >= store.state["fixLoopLimit"]:
                store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user", providerStatus=status))
                clear_operation(store, assignment_id)
                return False
            status = "failed"
            continue
        reviewed_sha = replacement
        session["reviewedSha"] = replacement
        merge_subject, merge_body, _merge_hash = canonical_merge_metadata(store.state, assignment, reviewed_sha)
        store.state["providerDeadlines"].pop(assignment_id, None)
        store.save()
        provider_approve(store, assignment_id, pr, reviewed_sha)
        status = wait_for_checks(store, assignment_id, pr, reviewed_sha)
    if status == "merged":
        current = store.state["taskStates"][assignment_id]["pr"]
        update_pr_metadata(store, assignment, current, reviewed_sha)
        mark_integrated(store, assignment, pr, reviewed_sha, "passed")
        relay_console.emit("DONE", f"operation=merge assignment={assignment_id} pr={pr['number']} source=provider")
        clear_operation(store, assignment_id)
        return True
    if status == "bypassable":
        log = store.path.parent / "logs" / "provider.log"
        start_operation(store, assignment_id, "merge-bypass", store.state["providerTimeoutSeconds"])
        relay_console.emit("START", f"operation=merge-bypass assignment={assignment_id} pr={pr['number']} deadline={store.state['providerTimeoutSeconds']}s")
        try:
            validate_publication_proof(store, assignment, reviewed_sha)
            pr_merge(store, f"{assignment_id}:merge-bypass:{reviewed_sha}", pr["number"], reviewed_sha, merge_subject, merge_body)
            merged_pr = inspect_merged_pr(store, assignment_id, pr["number"], reviewed_sha)
        except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            provider_status = f"merge-bypass-denied; log: {log}"
            store.update(lambda state: state["taskStates"][assignment_id].update(phase="waiting-provider", providerStatus=provider_status))
            relay_console.emit("BLOCKED", f"operation=merge-bypass assignment={assignment_id} pr={pr['number']} reason=denied log={log}")
            clear_operation(store, assignment_id, "merge-bypass")
            return False
        mark_integrated(store, assignment, merged_pr, reviewed_sha, "bypassed")
        relay_console.emit("DONE", f"operation=merge-bypass assignment={assignment_id} pr={pr['number']}")
        clear_operation(store, assignment_id, "merge-bypass")
        return True
    if status != "passed":
        store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user" if status in {"failed", "sha-drift"} else "waiting-provider", providerStatus=status))
        relay_console.emit("BLOCKED", f"operation=provider-checks assignment={assignment_id} pr={pr['number']} reason={status} log={store.path.parent / 'logs' / 'provider.log'}")
        clear_operation(store, assignment_id)
        return False
    start_operation(store, assignment_id, "merge", store.state["providerTimeoutSeconds"])
    relay_console.emit("START", f"operation=merge assignment={assignment_id} pr={pr['number']} deadline={store.state['providerTimeoutSeconds']}s")
    validate_publication_proof(store, assignment, reviewed_sha)
    pr_merge(store, f"{assignment_id}:merge", pr["number"], subject=merge_subject, body=merge_body)
    try:
        merged_pr = inspect_merged_pr(store, assignment_id, pr["number"], reviewed_sha)
    except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        provider_status = f"merge-proof-pending; log: {store.path.parent / 'logs' / 'provider.log'}"
        store.update(lambda state: state["taskStates"][assignment_id].update(phase="waiting-provider", providerStatus=provider_status))
        clear_operation(store, assignment_id, "merge")
        return False
    mark_integrated(store, assignment, merged_pr, reviewed_sha, "passed")
    relay_console.emit("DONE", f"operation=merge assignment={assignment_id} pr={pr['number']}")
    clear_operation(store, assignment_id, "merge")
    return True


def cleanup_worktree(store: StateStore, assignment_id: str) -> None:
    with store.lock:
        record = store.state["worktrees"].get(assignment_id)
        if not record:
            return
        root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / store.state["campaignId"]
        path = safe_within(Path(record.get("root", record["path"])), root)
        repository = Path(store.state["repository"])
        removed = git(repository, "worktree", "remove", "--force", str(path), timeout=store.state["providerTimeoutSeconds"], check=False)
        if removed.returncode and path.exists():
            return
        git(repository, "branch", "-D", record["branch"], timeout=store.state["providerTimeoutSeconds"], check=False)
        cleanup_assignment_environment(store, assignment_id)
        store.state["worktrees"].pop(assignment_id, None)
        store.save()


def cleanup_baseline_worktree(store: StateStore) -> None:
    with store.lock:
        record = store.state.get("worktrees", {}).get("BASELINE")
        baseline = store.state.get("baselineValidation", {})
        raw_root = (record or {}).get("root") or baseline.get("worktreeRoot")
        if not raw_root:
            return
        campaign_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / store.state["campaignId"]
        root = safe_within(Path(raw_root), campaign_root)
        repository_root = safe_within(Path(store.state.get("repositoryRoot", store.state["repository"])), Path(store.state.get("repositoryRoot", store.state["repository"])))
        removed = git(repository_root, "worktree", "remove", "--force", str(root), timeout=store.state["validationTimeoutSeconds"], check=False)
        if removed.returncode and root.exists():
            return
        cleanup_assignment_environment(store, "BASELINE")
        store.state.get("worktrees", {}).pop("BASELINE", None)
        store.save()


def run_baseline_validation(store: StateStore) -> bool:
    commands = store.state.get("campaignValidationCommands", [])
    baseline = store.state.get("baselineValidation")
    if not commands:
        raise RuntimeError("campaign validation state is missing")
    expected_hash = commands_hash(commands)
    if baseline is None:
        raise RuntimeError("campaign validation state is missing")
    if baseline.get("baseSha") != store.state["baseSha"] or baseline.get("commandsHash") != expected_hash:
        raise RuntimeError("campaign validation state/ledger drift")
    if baseline.get("phase") == "passed":
        return True
    if baseline.get("phase") == "blocked":
        store.update(lambda state: state.__setitem__("phase", "needs-user"))
        return False
    cleanup_baseline_worktree(store)
    if "BASELINE" in store.state.get("worktrees", {}):
        raise RuntimeError("baseline worktree cleanup failed")
    campaign_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / store.state["campaignId"]
    worktree_root = safe_within(campaign_root / "BASELINE", campaign_root)
    worktree = safe_within(worktree_root / Path(store.state.get("repositoryPrefix", "")), worktree_root)
    def starting(state: dict) -> None:
        state["phase"] = "baseline-validation"
        state["baselineValidation"].update(
            phase="worktree", currentCommand=None, startedAt=datetime.now(timezone.utc).isoformat(),
            deadline=time.time() + state["validationTimeoutSeconds"], completedAt=None, error=None, log=None,
            worktreeRoot=str(worktree_root),
        )
        state["worktrees"]["BASELINE"] = {"path": str(worktree), "root": str(worktree_root), "baseSha": state["baseSha"], "detached": True}
    store.update(starting)
    try:
        worktree_root.parent.mkdir(parents=True, exist_ok=True)
        repository_root = Path(store.state.get("repositoryRoot", store.state["repository"])).resolve()
        git(repository_root, "worktree", "add", "--detach", str(worktree_root), store.state["baseSha"], timeout=store.state["validationTimeoutSeconds"])
        worktree.mkdir(parents=True, exist_ok=True)
        top = Path(git(worktree_root, "rev-parse", "--show-toplevel", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()).resolve()
        if top != worktree_root.resolve() or safe_within(worktree, worktree_root) != worktree.resolve():
            raise RuntimeError("baseline worktree mismatch")
        run_validations(store, {"id": "BASELINE", "validationCommands": commands}, worktree, "baseline", commands, baseline)
        store.update(lambda state: state["baselineValidation"].update(
            phase="passed", completedAt=datetime.now(timezone.utc).isoformat(), currentCommand=None,
            deadline=None, error=None, log=None,
        ))
        return True
    except KeyboardInterrupt:
        store.update(lambda state: state["baselineValidation"].update(phase="interrupted", error="interrupted"))
        raise
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        def blocked(state: dict) -> None:
            record = state["baselineValidation"]
            record.update(phase="blocked", completedAt=datetime.now(timezone.utc).isoformat(), error=str(error), log=record.get("validationLog"))
            state["phase"] = "needs-user"
        store.update(blocked)
        return False
    finally:
        cleanup_baseline_worktree(store)


def process_assignment(store: StateStore, semaphore: threading.Semaphore, assignment: dict, mode: str) -> bool:
    assignment_id = assignment["id"]
    def initialize(state: dict) -> None:
        task_state = state["taskStates"].setdefault(assignment_id, {"phase": "ready", "mode": mode, "pushed": False, "merged": False})
        task_state.setdefault("fixAttemptsStarted", 0)
    store.update(initialize)
    task_state = store.state["taskStates"][assignment_id]
    if task_state["phase"] == "integrated":
        return True
    try:
        worktree, branch = create_worktree(store, assignment)
        task_state.update(worktree=str(worktree), branch=branch)
        store.save()
        sha = task_state.get("candidateSha")
        while not sha and not task_state.get("coordinatorFailureBlocked") and (task_state.get("pendingWorkerSha") or worker_attempt_available(store.state, assignment_id)):
            failure = task_state.get("validationFailure")
            if failure and (fix_attempts_started(store.state, assignment_id) >= store.state["fixLoopLimit"] or not worker_attempt_available(store.state, assignment_id)):
                break
            task_state["phase"] = "implementing"
            store.save()
            try:
                if task_state.get("pendingWorkerSha"):
                    result = {"candidateSha": task_state["pendingWorkerSha"]}
                else:
                    candidate = clean_validation_candidate(store, assignment_id, worktree) if failure else ""
                    if failure:
                        if not candidate:
                            raise RuntimeError("validation candidate is missing or dirty")
                        reserve_fix(store, assignment_id)
                    elif mode == "bug" and not fix_attempts_started(store.state, assignment_id):
                        reserve_fix(store, assignment_id)
                    result = invoke_with_replacements(
                        store, semaphore, worktree, assignment_id, "worker",
                        worker_prompt(mode, assignment, candidate or "", [failure] if failure else None, task_state.get("error", "")), mode=mode,
                    )
                    task_state.update(pendingWorkerSha=result["candidateSha"], phase="candidate-validation")
                    if result.get("summary") is not None:
                        task_state["workerSummary"] = result["summary"]
                    store.save()
                sha = validate_candidate(store, assignment, worktree, result)
                task_state.pop("pendingWorkerSha", None)
                store.save()
            except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
                task_state["error"] = str(error)
                failure = task_state.get("validationFailure")
                is_validation_failure = isinstance(failure, dict) or "validation command " in str(error)
                candidate = clean_validation_candidate(store, assignment_id, worktree)
                if is_validation_failure:
                    task_state.pop("pendingWorkerSha", None)
                elif candidate:
                    identity = coordinator_failure_identity(error, candidate)
                    previous = task_state.get("coordinatorFailure", {})
                    count = previous.get("count", 0) + 1 if previous.get("identity") == identity else 1
                    task_state["coordinatorFailure"] = {"identity": identity, "count": count, "candidateSha": candidate, "evidence": str(error)}
                    task_state["pendingWorkerSha"] = candidate
                    if count >= 2:
                        task_state["coordinatorFailureBlocked"] = True
                else:
                    task_state.pop("validationCandidateSha", None)
                store.save()
                sha = None
        if not sha:
            task_state["phase"] = "blocked" if task_state.get("coordinatorFailureBlocked") else stop_phase(task_state.get("error", "attempt budget exhausted"))
            store.save()
            relay_console.emit("BLOCKED", f"operation=worker assignment={assignment_id} reason={task_state.get('error', 'attempt budget exhausted')}")
            clear_operation(store, assignment_id)
            return False
        if not run_review(store, semaphore, assignment, worktree, sha):
            task_state["phase"] = store.state["reviewSessions"][assignment_id]["phase"]
            store.save()
            relay_console.emit("BLOCKED", f"operation=internal-review assignment={assignment_id} reason={task_state.get('error', 'review requires user')}")
            clear_operation(store, assignment_id)
            return False
        session = store.state["reviewSessions"][assignment_id]
        pr = publish_candidate(store, assignment, worktree, branch, session["reviewedSha"])
        task_state["phase"] = "approved"
        store.save()
        relay_console.emit("DONE", f"operation=internal-review assignment={assignment_id} result=approved sha={session['reviewedSha'][:12]}")
        if not merge_assignment(store, semaphore, assignment, worktree, branch, pr, session["reviewedSha"]):
            return False
        if mode == "task":
            with store.lock:
                update_task_ledger(store, assignment_id, status="integrated", branch=branch, pullRequest=str(pr.get("url", pr.get("number"))), candidate=session["reviewedSha"])
        else:
            with store.lock:
                bugs = load_bugs(store)
                for bug in bugs:
                    if bug["id"] == assignment_id:
                        bug.update(status="resolved", branch=branch, pullRequest=str(pr.get("url", pr.get("number"))), candidate=session["reviewedSha"])
                write_bugs(store, bugs)
        cleanup_worktree(store, assignment_id)
        return True
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        task_state.update(phase=stop_phase(error), error=str(error))
        store.save()
        log = re.search(r"log:\s*([^\r\n]+)", str(error))
        relay_console.emit("FAILED", f"operation={task_state.get('operation', 'assignment')} assignment={assignment_id} reason={str(error).splitlines()[0]}" + (f" log={log.group(1)}" if log else ""))
        clear_operation(store, assignment_id)
        return False


def validate_audit_scopes(value: dict) -> list[dict]:
    scopes = value.get("scopes") if isinstance(value, dict) else None
    if not isinstance(scopes, list):
        raise ValueError("audit plan needs scopes")
    ids = []
    for scope in scopes:
        required = {"scopeId", "scope", "requirements", "paths", "commands", "completionCondition"}
        if not isinstance(scope, dict) or required - scope.keys() or not re.fullmatch(r"AUDIT-\d{4}", scope.get("scopeId", "")) or not all(isinstance(scope[key], list) and all(isinstance(item, str) for item in scope[key]) for key in ("requirements", "paths", "commands")) or not scope["commands"] or any(not command.strip() for command in scope["commands"]) or not isinstance(scope["scope"], str) or not isinstance(scope["completionCondition"], str) or any(not valid_relative_path(path) for path in scope["paths"]):
            raise ValueError("invalid audit scope")
        ids.append(scope["scopeId"])
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate audit scope")
    return scopes


def validate_audit_plan(value: dict) -> dict:
    validate_audit_scopes(value)
    return value


def run_audit(store: StateStore, semaphore: threading.Semaphore, tasks: list[dict]) -> list[dict]:
    audit_sha = store.state.get("auditBaseSha") or git(Path(store.state["repository"]), "rev-parse", "HEAD", timeout=store.state["providerTimeoutSeconds"]).stdout.strip()
    if not store.state.get("auditBaseSha"):
        store.update(lambda state: state.__setitem__("auditBaseSha", audit_sha))
    if store.state["auditPlanCompleted"]:
        scopes = list(store.state["auditScopes"].values())
    else:
        if not store.state["auditPlanStarted"]:
            store.update(lambda state: state.update(auditPlanStarted=True, auditCallLimit=1 + state["formatRetryAllowance"]))
        try:
            result = invoke_with_replacements(store, semaphore, Path(store.state["repository"]), "AUDIT", "audit-planner", role_prompt("audit-planner", {"id": "AUDIT", "requirements": [], "allowedPaths": [], "acceptanceCriteria": [], "validationCommands": []}, audit_sha, {"tasks": tasks, "bugs": load_bugs(store)}), audit=True, validator=validate_audit_plan)
            scopes = validate_audit_scopes(result)
        except (RuntimeError, ValueError):
            store.update(lambda state: state.update(phase="needs-user"))
            return []
        def fixed(state: dict) -> None:
            state["auditScopes"] = {scope["scopeId"]: {**scope, "started": False, "completed": False, "findings": []} for scope in scopes}
            state["auditCallLimit"] = 1 + len(scopes) + state["formatRetryAllowance"]
            state["auditPlanCompleted"] = True
        store.update(fixed)
    pending = [scope for scope in store.state["auditScopes"].values() if not scope["completed"]]
    findings = []
    if pending:
        with ThreadPoolExecutor(max_workers=min(store.state["workerLimit"], len(pending))) as pool:
            futures = {}
            for scope in pending:
                scope["started"] = True
                store.save()
                assignment = {"id": scope["scopeId"], "allowedPaths": scope["paths"], "acceptanceCriteria": [scope["completionCondition"]], "validationCommands": scope["commands"]}
                futures[pool.submit(invoke_with_replacements, store, semaphore, Path(store.state["repository"]), scope["scopeId"], "audit-worker", role_prompt("audit-worker", assignment, audit_sha, scope), audit=True)] = scope
            for future in as_completed(futures):
                scope, result = futures[future], future.result()
                scope.update(completed=True, findings=result["findings"])
                findings.extend(result["findings"])
                store.save()
    else:
        findings = [finding for scope in store.state["auditScopes"].values() for finding in scope["findings"]]
    if not store.state.get("auditDispositionsCompleted"):
        dispositions = []
        for finding in findings:
            scope = next((item for item in store.state["auditScopes"].values() if finding in item.get("findings", [])), None)
            if scope is None:
                raise ValueError(f"audit finding has no scope: {finding['id']}")
            outside = [path for path in finding["repairPaths"] if not allowed_change(path, scope["paths"], scope_directories(store, scope["paths"]))]
            dispositions.append(finding if not outside else finding | {"action": "needs-user", "reason": f"Repair path outside audit scope: {', '.join(outside)}", "repairPaths": []})
        accepted = record_findings(store, "audit", dispositions)
        validation_commands = {}
        for bug in accepted:
            scope = next(item for item in store.state["auditScopes"].values() if any(finding["id"] == bug["sourceFindingId"] for finding in item.get("findings", [])))
            validation_commands[bug["id"]] = list(scope["commands"])
        store.update(lambda state: (state.setdefault("auditBugValidationCommands", {}).update(validation_commands), state.__setitem__("auditDispositionsCompleted", True)))
        if any(finding["action"] == "needs-user" for finding in dispositions):
            store.update(lambda state: state.__setitem__("phase", "needs-user"))
            return []
        return accepted
    return [bug for bug in load_bugs(store) if bug["source"] == "audit" and bug["status"] == "active"]


def audit_bug_validation_commands(store: StateStore, bug: dict) -> list[str]:
    commands = store.state.get("auditBugValidationCommands", {}).get(bug["id"])
    if not commands:
        scope = next((item for item in store.state.get("auditScopes", {}).values() if any(finding.get("id") == bug.get("sourceFindingId") for finding in item.get("findings", []))), None)
        commands = (scope or {}).get("commands")
    if not commands or any(not isinstance(command, str) or not command.strip() for command in commands):
        raise ValueError(f"audit bug has no executable validation commands: {bug['id']}")
    return list(commands)


def bug_assignment(store: StateStore, bug: dict) -> dict:
    return {"id": bug["id"], "title": bug["title"], "status": "ready", "priority": bug["severity"], "dependencies": [], "allowedPaths": bug["allowedPaths"], "acceptanceCriteria": [f"Resolve: {bug['failure']}", f"Meet requirement: {bug['requirement']}"], "validationCommands": audit_bug_validation_commands(store, bug), "auditFinding": bug}


def target_instructions(repo: Path, create: bool = False) -> tuple[str, bytes]:
    path = repo / "AGENTS.md"
    if not os.path.lexists(path) and create:
        create_exclusive(path, TARGET_AGENTS)
    if not path.is_file():
        raise RuntimeError("AGENTS.md must be a regular UTF-8 file")
    content = path.read_bytes()
    try:
        return content.decode("utf-8"), content
    except UnicodeDecodeError as error:
        raise RuntimeError("AGENTS.md must be UTF-8") from error


def provider_preflight(store: StateStore) -> None:
    if store.state.get("preflightCompleted"):
        relay_console.emit("DONE", f"operation=provider-preflight provider={provider_name(store.state)} identity=cached authentication=not-rechecked")
        if store.state.get("provider") == "azure-devops" and store.state["mergeMethod"] == "rebase":
            raise RuntimeError("Azure DevOps does not support Relay's rebase merge method; use squash or merge")
        return
    repository = Path(store.state["repository"])
    remote = git(repository, "config", "--get", "remote.origin.url", timeout=store.state["providerTimeoutSeconds"]).stdout.strip()
    try:
        identity = detect_provider(remote)
    except ValueError:
        if not os.environ.get("RELAY_ALLOW_FAKE_PROVIDER"):
            raise
        identity = {"provider": "github", "githubRepository": "fake/relay"}
    store.update(lambda state: state.update(identity))
    relay_console.emit("START", f"operation=provider-preflight provider={provider_name(store.state)}")
    if identity["provider"] == "azure-devops":
        if store.state["mergeMethod"] == "rebase":
            raise RuntimeError("Azure DevOps does not support Relay's rebase merge method; use squash or merge")
        provider_with_retries(store, "preflight:auth", "devops", "project", "show", "--project", identity["azureProject"], "--organization", _azure_organization(identity), "--output", "json")
        provider_with_retries(store, "preflight:repo", "repos", "show", "--repository", identity["azureRepository"], "--project", identity["azureProject"], "--organization", _azure_organization(identity), "--output", "json")
    else:
        provider_with_retries(store, "preflight:auth", "auth", "status")
        provider_with_retries(store, "preflight:repo", "repo", "view", identity["githubRepository"])
    store.update(lambda state: state.__setitem__("preflightCompleted", True))
    relay_console.emit("DONE", f"operation=provider-preflight provider={provider_name(store.state)}")


def validate_agents_bootstrap(store: StateStore, worktree: Path, sha: str) -> None:
    bootstrap = store.state["agentsBootstrap"]
    base = bootstrap["baseSha"]
    if git(worktree, "merge-base", "--is-ancestor", base, sha, timeout=store.state["validationTimeoutSeconds"], check=False).returncode:
        raise RuntimeError("AGENTS.md bootstrap does not descend from its base")
    changed = target_changes(store, worktree, base, sha)
    blob = git(worktree, "show", f"{sha}:{repository_path(store.state, 'AGENTS.md')}", timeout=store.state["validationTimeoutSeconds"]).stdout.encode()
    if changed != ["AGENTS.md"] or hashlib.sha256(blob).hexdigest() != bootstrap["contentHash"] or blob != TARGET_AGENTS.encode():
        raise RuntimeError("AGENTS.md bootstrap candidate is not the exact generated file")


def _agents_bootstrap_needs_user(store: StateStore, status: str) -> bool:
    def update(state: dict) -> None:
        state["agentsBootstrap"].update(phase="needs-user", providerStatus=status)
        state["phase"] = "needs-user"
    store.update(update)
    relay_console.emit("BLOCKED", f"operation={store.state['agentsBootstrap'].get('operation', 'bootstrap')} assignment=AGENTS reason={status} log={store.path.parent / 'logs' / 'provider.log'}")
    clear_operation(store, "AGENTS")
    return False


def reconcile_agents_bootstrap(store: StateStore) -> bool:
    repository = Path(store.state["repository"])
    agents = repository / "AGENTS.md"
    try:
        git_provider_with_retries(store, "AGENTS:reconcile-fetch", repository, "fetch", "origin", "main")
        remote_sha = git(repository, "rev-parse", "origin/main", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()
        relative_agents = repository_path(store.state, "AGENTS.md")
        shown = git(repository, "show", f"{remote_sha}:{relative_agents}", timeout=store.state["validationTimeoutSeconds"], check=False)
        if shown.returncode:
            raise RuntimeError("merged AGENTS.md is missing from target directory")
        remote_blob = shown.stdout.encode()
        if remote_blob != TARGET_AGENTS.encode():
            raise RuntimeError("merged AGENTS.md does not match generated content")
        head = git(repository, "rev-parse", "HEAD", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()
        if os.path.lexists(agents) and not agents.is_file():
            return _agents_bootstrap_needs_user(store, "generated-file-modified")
        working = agents.read_bytes() if agents.is_file() else b""
        if working and working != TARGET_AGENTS.encode() and (head != remote_sha or working.replace(b"\r\n", b"\n") != TARGET_AGENTS.encode()):
            return _agents_bootstrap_needs_user(store, "generated-file-modified")
        if head != remote_sha:
            if os.path.lexists(agents):
                agents.unlink()
            git(repository, "merge", "--ff-only", "origin/main", timeout=store.state["validationTimeoutSeconds"])
        elif not agents.is_file():
            git(repository, "restore", "--source", "origin/main", "--", "AGENTS.md", timeout=store.state["validationTimeoutSeconds"])
        if agents.read_bytes().replace(b"\r\n", b"\n") == TARGET_AGENTS.encode():
            agents.write_bytes(TARGET_AGENTS.encode())
        content = agents.read_bytes()
        if content != TARGET_AGENTS.encode():
            raise RuntimeError("working AGENTS.md does not match generated content")
        cleanup_worktree(store, "AGENTS")
        store.update(lambda state: (state["agentsBootstrap"].update(phase="complete", providerStatus="passed"), state.update(phase="build", targetInstructions=content.decode("utf-8"))))
        relay_console.emit("DONE", "operation=merge assignment=AGENTS")
        clear_operation(store, "AGENTS")
        return True
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        return _agents_bootstrap_needs_user(store, str(error))


def bootstrap_agents(store: StateStore) -> bool:
    bootstrap = store.state.get("agentsBootstrap")
    if not bootstrap or bootstrap["phase"] == "complete":
        return True
    if bootstrap["phase"] == "needs-user":
        store.update(lambda state: state.__setitem__("phase", "needs-user"))
        return False
    repository = Path(store.state["repository"])
    agents = repository / "AGENTS.md"
    if bootstrap["phase"] == "reconciling":
        return reconcile_agents_bootstrap(store)
    if not agents.is_file() or agents.read_bytes() != TARGET_AGENTS.encode():
        return _agents_bootstrap_needs_user(store, "generated-file-modified")
    store.update(lambda state: state.update(phase="agents-bootstrap"))
    try:
        worktree = Path(bootstrap["worktree"])
        worktree_root = Path(bootstrap.get("worktreeRoot", worktree))
        branch = bootstrap["branch"]
        if not bootstrap.get("candidateSha"):
            if not worktree_root.exists():
                worktree_root.parent.mkdir(parents=True, exist_ok=True)
                branch_exists = git(repository, "show-ref", "--verify", f"refs/heads/{branch}", timeout=store.state["providerTimeoutSeconds"], check=False).returncode == 0
                if branch_exists:
                    git(repository, "worktree", "add", str(worktree_root), branch, timeout=store.state["providerTimeoutSeconds"])
                else:
                    git(repository, "worktree", "add", "-b", branch, str(worktree_root), bootstrap["baseSha"], timeout=store.state["providerTimeoutSeconds"])
            top = git(worktree_root, "rev-parse", "--show-toplevel", timeout=store.state["providerTimeoutSeconds"]).stdout.strip()
            if Path(top).resolve() != worktree_root.resolve():
                raise RuntimeError("AGENTS.md bootstrap worktree mismatch")
            worktree.mkdir(parents=True, exist_ok=True)
            store.update(lambda state: state["worktrees"].__setitem__("AGENTS", {"path": str(worktree), "root": str(worktree_root), "branch": branch, "baseSha": bootstrap["baseSha"]}))
            worktree_agents = worktree / "AGENTS.md"
            if os.path.lexists(worktree_agents):
                if not worktree_agents.is_file() or worktree_agents.read_bytes() != TARGET_AGENTS.encode():
                    raise RuntimeError("unexpected AGENTS.md in bootstrap worktree")
            else:
                create_exclusive(worktree_agents, TARGET_AGENTS)
            head = git(worktree, "rev-parse", "HEAD", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()
            if head == bootstrap["baseSha"]:
                git(worktree, "add", "AGENTS.md", timeout=store.state["validationTimeoutSeconds"])
                git(worktree, "commit", "-m", "Add Relay target instructions", timeout=store.state["validationTimeoutSeconds"])
                head = git(worktree, "rev-parse", "HEAD", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()
            validate_agents_bootstrap(store, worktree, head)
            store.update(lambda state: state["agentsBootstrap"].update(phase="publish", candidateSha=head))
        sha = bootstrap["candidateSha"]
        validate_agents_bootstrap(store, worktree, sha)
        if bootstrap.get("pushedSha") != sha:
            remote = git_provider_with_retries(store, "AGENTS:ls-remote", worktree, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
            remote_sha = remote.stdout.split()[0] if remote.stdout.strip() else ""
            if remote_sha and remote_sha != sha:
                return _agents_bootstrap_needs_user(store, "remote-branch-drift")
            if not remote_sha:
                git_provider_with_retries(store, f"AGENTS:push:{sha}", worktree, "push", "--set-upstream", "origin", branch)
            store.update(lambda state: state["agentsBootstrap"].update(phase="pull-request", pushedSha=sha))
        pr = bootstrap.get("pr")
        if not pr:
            matches = pr_discover(store, "AGENTS:pr-list", branch)
            if matches:
                pr = matches[0]
            else:
                body = store.path.parent / ".AGENTS-pr.md"
                atomic_write(body, f"Relay generated target instructions\n\nCandidate: {sha}\n")
                try:
                    pr = pr_create(store, "AGENTS:pr-create", branch, "Add Relay target instructions", body)
                finally:
                    body.unlink(missing_ok=True)
            if pr.get("headRefOid") != sha:
                return _agents_bootstrap_needs_user(store, "sha-drift")
            store.update(lambda state: (state["agentsBootstrap"].update(phase="checks", pr=pr), state["pullRequests"].__setitem__("AGENTS", pr)))
        status = wait_for_checks(store, "AGENTS", pr, sha)
        if status not in {"passed", "merged"}:
            terminal = "needs-user" if status in {"failed", "sha-drift", "repair-required"} else "waiting-provider"
            store.update(lambda state: (state["agentsBootstrap"].update(phase=terminal, providerStatus=status), state.__setitem__("phase", terminal)))
            return False
        if status == "passed":
            store.update(lambda state: state["agentsBootstrap"].update(phase="merging", providerStatus="passed"))
            pr_merge(store, "AGENTS:merge", pr["number"])
        store.update(lambda state: (state["agentsBootstrap"].update(phase="reconciling", providerStatus="passed"), state["pullRequests"]["AGENTS"].update(state="MERGED")))
        return reconcile_agents_bootstrap(store)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return _agents_bootstrap_needs_user(store, str(error))


def recovery_worktree(store: StateStore, assignment_id: str) -> tuple[Path, dict]:
    record = store.state.get("worktrees", {}).get(assignment_id)
    if not record:
        raise RuntimeError(f"recovery worktree is missing: {assignment_id}")
    campaign_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / store.state["campaignId"]
    root = safe_within(Path(record.get("root", record["path"])), campaign_root)
    worktree = safe_within(Path(record["path"]), root)
    if not worktree.is_dir():
        raise RuntimeError(f"recovery worktree does not exist: {assignment_id}")
    top = Path(git(worktree, "rev-parse", "--show-toplevel", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()).resolve()
    if top != root:
        raise RuntimeError(f"recovery worktree mismatch: {assignment_id}")
    return worktree, record


def _recovery_snapshot(store: StateStore, assignment: dict) -> dict:
    assignment_id = assignment["id"]
    worktree, record = recovery_worktree(store, assignment_id)
    head = git(worktree, "rev-parse", "HEAD", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()
    dirty = git(worktree, "status", "--porcelain=v1", "--untracked-files=all", timeout=store.state["validationTimeoutSeconds"]).stdout
    if dirty:
        raise RuntimeError(f"recovery refused unexpected worktree changes: {assignment_id}")
    task_state = store.state["taskStates"][assignment_id]
    session = store.state.get("reviewSessions", {}).get(assignment_id, {})
    candidates = {
        value for value in (
            record.get("baseSha"), task_state.get("candidateSha"), task_state.get("pendingWorkerSha"),
            task_state.get("validationCandidateSha"), session.get("initialCandidateSha"),
            session.get("currentCandidateSha"), session.get("pendingRepairSha"), session.get("reviewedSha"),
        ) if isinstance(value, str) and value
    }
    if head not in candidates:
        raise RuntimeError(f"recovery refused candidate SHA drift: {assignment_id}")
    ancestry = git(worktree, "merge-base", "--is-ancestor", record["baseSha"], head, timeout=store.state["validationTimeoutSeconds"], check=False)
    if ancestry.returncode:
        raise RuntimeError(f"recovery refused candidate ancestry drift: {assignment_id}")
    changed = target_changes(store, worktree, record["baseSha"], head)
    allowed = assignment["allowedPaths"]
    if "approvedRepairPaths" in session:
        approved = session["approvedRepairPaths"]
        if not isinstance(approved, list) or any(not isinstance(path, str) or not valid_relative_path(path) for path in approved):
            raise RuntimeError(f"recovery refused invalid approved repair scope: {assignment_id}")
        maximum = maximum_repair_paths(store, assignment)
        directories = scope_directories(store, maximum)
        unapproved = sorted(path for path in approved if not allowed_change(path, maximum, directories))
        if unapproved:
            raise RuntimeError(f"recovery refused approved repair paths outside maximum scope for {assignment_id}: {', '.join(unapproved)}")
        allowed = approved
    outside = sorted(path for path in changed if not allowed_change(path, allowed, scope_directories(store, allowed)))
    if outside:
        raise RuntimeError(f"recovery refused scope drift for {assignment_id}: {', '.join(outside)}")
    return {"headSha": head, "branch": record["branch"]}


def _inspect_pr_readonly(store: StateStore, identifier: str | int) -> dict:
    if store.state.get("provider") == "azure-devops":
        args = ["repos", "pr", "show", "--id", str(identifier), *_azure_context(store.state, repository=False, project=False), "--output", "json"]
        tool = "az"
    else:
        args = ["pr", "view", str(identifier), "--json", "number,url,headRefOid,state", "--repo", store.state.get("githubRepository", "fake/relay")]
        tool = "gh"
    return normalize_pr(store.state, _provider_json(run_tool(tool, *args, timeout=store.state["providerTimeoutSeconds"])))


def plan_recovery(store: StateStore, tasks: list[dict], deferred: list[str], grants: list[str]) -> list[dict]:
    if store.state.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise RuntimeError("recovery supports schema 3 only")
    if store.state.get("activeProcesses"):
        raise RuntimeError("recovery requires an inactive campaign")
    bugs = load_bugs(store)
    assignments = {task["id"]: task for task in tasks}
    assignments.update({bug["id"]: bug_assignment(store, bug) for bug in bugs if bug.get("source") == "audit" and bug.get("status") == "active"})
    active_bugs = {bug["id"]: bug for bug in bugs if bug.get("status") == "active"}
    if len(deferred) != len(set(deferred)) or any(bug_id not in active_bugs for bug_id in deferred):
        raise ValueError("--defer-blocker requires unique active campaign bug IDs")
    if len(grants) != len(set(grants)) or any(assignment_id not in assignments for assignment_id in grants):
        raise ValueError("--grant-attempt requires unique campaign assignment IDs")

    actions = []
    handled = set()
    for assignment_id in grants:
        task_state = store.state.get("taskStates", {}).get(assignment_id)
        if not task_state or task_state.get("phase") != "needs-user":
            raise RuntimeError(f"attempt grant requires a needs-user assignment: {assignment_id}")
        snapshot = _recovery_snapshot(store, assignments[assignment_id]) if assignment_id in store.state.get("worktrees", {}) else {}
        actions.append({"action": "grant", "assignmentId": assignment_id, "grant": store.state.get("recoveryAttemptGrants", {}).get(assignment_id, 0) + 1, **snapshot})
        handled.add(assignment_id)

    selected = set(deferred)
    for assignment_id, session in sorted(store.state.get("reviewSessions", {}).items()):
        blockers = set(session.get("acceptedBlockerIds", []))
        chosen = blockers & selected
        if not chosen:
            continue
        if chosen != blockers:
            raise RuntimeError(f"defer must select every blocker for {assignment_id}")
        snapshot = _recovery_snapshot(store, assignments[assignment_id])
        candidate = session.get("initialCandidateSha")
        if not candidate:
            raise RuntimeError(f"defer has no reviewed candidate: {assignment_id}")
        actions.append({"action": "defer", "assignmentId": assignment_id, "bugIds": sorted(chosen), "candidateSha": candidate, **snapshot})
        handled.add(assignment_id)

    baseline = store.state.get("baselineValidation") or {}
    if baseline.get("phase") in {"blocked", "interrupted"}:
        actions.append({"action": "resume", "assignmentId": "BASELINE", "fromPhase": baseline["phase"], "toPhase": "pending", "baseSha": baseline.get("baseSha"), "commandsHash": baseline.get("commandsHash")})

    safe_phases = {"ready", "implementing", "candidate-validation", "slice-review", "approved", "push-and-open-pr", "provider-checks", "resume-provider", "waiting-provider"}
    for assignment_id, task_state in sorted(store.state.get("taskStates", {}).items()):
        if assignment_id not in assignments or assignment_id in handled or task_state.get("phase") == "integrated":
            continue
        phase = task_state.get("phase")
        session = store.state.get("reviewSessions", {}).get(assignment_id, {})
        if re.fullmatch(r"(?:repair|verify)-\d+", str(phase)):
            safe = True
        else:
            safe = phase in safe_phases
        if phase == "needs-user":
            error = task_state.get("error") or task_state.get("providerStatus") or ""
            if task_state.get("validationFailure") and task_state.get("validationCandidateSha"):
                target = "candidate-validation"
            elif _worktree_setup_failure(error) and assignment_id not in store.state.get("worktrees", {}):
                actions.append({"action": "resume", "assignmentId": assignment_id, "fromPhase": phase, "toPhase": "ready"})
                handled.add(assignment_id)
                continue
            elif task_state.get("pr") or store.state.get("pullRequests", {}).get(assignment_id) or _publication_retry_key(error, assignment_id):
                target = "approved"
            else:
                continue
        elif safe:
            target = phase
            if phase == "implementing":
                target = "candidate-validation" if task_state.get("pendingWorkerSha") else "ready"
            elif phase in {"provider-checks", "resume-provider", "waiting-provider", "push-and-open-pr"}:
                target = "approved"
        else:
            raise RuntimeError(f"recovery refused removed or unknown phase {phase!r} for {assignment_id}; replan from the incomplete requirements and backlog")
        snapshot = _recovery_snapshot(store, assignments[assignment_id])
        action = {"action": "resume", "assignmentId": assignment_id, "fromPhase": phase, "toPhase": target, **snapshot}
        if target == "candidate-validation":
            action["candidateSha"] = task_state.get("pendingWorkerSha") or task_state.get("validationCandidateSha")
        if target == "approved":
            pr = task_state.get("pr") or store.state.get("pullRequests", {}).get(assignment_id)
            candidate = session.get("reviewedSha") or task_state.get("candidateSha")
            if not pr or not candidate:
                raise RuntimeError(f"provider recovery is missing PR or reviewed candidate: {assignment_id}")
            live = _inspect_pr_readonly(store, pr["number"])
            if live["headRefOid"] != candidate:
                raise RuntimeError(f"recovery refused provider SHA drift: {assignment_id}")
            action.update(prNumber=live["number"], providerState=live["state"], candidateSha=candidate)
            if retry_key := _publication_retry_key(task_state.get("error"), assignment_id):
                action["providerRetryKey"] = retry_key
        actions.append(action)
        handled.add(assignment_id)

    unresolved = sorted(
        assignment_id for assignment_id, task_state in store.state.get("taskStates", {}).items()
        if assignment_id in assignments and task_state.get("phase") not in {"integrated"} and assignment_id not in handled
    )
    if unresolved:
        raise RuntimeError(f"recovery needs grant, defer, or replanning for: {', '.join(unresolved)}")
    if not actions:
        raise RuntimeError("recovery found no safe action")
    return actions


def print_recovery(actions: list[dict]) -> None:
    for action in actions:
        detail = f"assignment={action['assignmentId']} action={action['action']}"
        if action.get("toPhase"):
            detail += f" phase={action.get('fromPhase')}->{action['toPhase']}"
        if action.get("bugIds"):
            detail += f" blockers={','.join(action['bugIds'])}"
        print(f"RECOVER {detail}")


def apply_recovery(store: StateStore, tasks: list[dict], actions: list[dict]) -> None:
    deferred = [bug_id for action in actions if action["action"] == "defer" for bug_id in action["bugIds"]]
    grants = [action["assignmentId"] for action in actions if action["action"] == "grant"]
    if plan_recovery(store, tasks, deferred, grants) != actions:
        raise RuntimeError("campaign changed after recovery preview")
    bugs = load_bugs(store)
    assignments = {task["id"]: task for task in tasks}
    assignments.update({bug["id"]: bug_assignment(store, bug) for bug in bugs if bug.get("source") == "audit" and bug.get("status") == "active"})
    store.update(lambda state: state.__setitem__("pendingRecovery", {"actions": actions, "completed": []}))
    for action in actions:
        assignment_id = action["assignmentId"]
        if assignment_id == "BASELINE":
            baseline = store.state["baselineValidation"]
            if baseline.get("phase") != action["fromPhase"] or baseline.get("baseSha") != action["baseSha"] or baseline.get("commandsHash") != action["commandsHash"]:
                raise RuntimeError("baseline changed after recovery preview")
            baseline.update(phase="pending", currentCommand=None, error=None, log=None)
        else:
            task_state = store.state["taskStates"][assignment_id]
            if action["action"] == "grant":
                if assignment_id in store.state.get("worktrees", {}):
                    snapshot = _recovery_snapshot(store, assignments[assignment_id])
                    if any(snapshot.get(key) != action.get(key) for key in snapshot):
                        raise RuntimeError(f"assignment changed after recovery preview: {assignment_id}")
                store.state.setdefault("recoveryAttemptGrants", {})[assignment_id] = action["grant"]
                task_state.update(phase="ready")
                task_state.pop("error", None)
            elif action["action"] == "defer":
                snapshot = _recovery_snapshot(store, assignments[assignment_id])
                if any(snapshot.get(key) != action.get(key) for key in snapshot):
                    raise RuntimeError(f"assignment changed after recovery preview: {assignment_id}")
                worktree, _ = recovery_worktree(store, assignment_id)
                if snapshot["headSha"] != action["candidateSha"]:
                    git(worktree, "reset", "--hard", action["candidateSha"], timeout=store.state["validationTimeoutSeconds"])
                for bug in bugs:
                    if bug["id"] in action["bugIds"]:
                        bug.update(status="backlog", deferralReason="Deferred by explicit recovery.")
                session = store.state["reviewSessions"][assignment_id]
                session.update(phase="approved", reviewedSha=action["candidateSha"], currentCandidateSha=action["candidateSha"], acceptedBlockerIds=[])
                task_state.update(phase="approved", candidateSha=action["candidateSha"])
                task_state.pop("error", None)
            else:
                if assignment_id in store.state.get("worktrees", {}):
                    snapshot = _recovery_snapshot(store, assignments[assignment_id])
                    if any(snapshot.get(key) != action.get(key) for key in snapshot):
                        raise RuntimeError(f"assignment changed after recovery preview: {assignment_id}")
                if task_state.get("phase") != action["fromPhase"]:
                    raise RuntimeError(f"assignment phase changed after recovery preview: {assignment_id}")
                task_state["phase"] = action["toPhase"]
                if action.get("candidateSha") and action["toPhase"] == "candidate-validation":
                    task_state["pendingWorkerSha"] = action["candidateSha"]
                if retry_key := action.get("providerRetryKey"):
                    attempts = store.state.get("providerAttemptCounters", {}).get(retry_key, 0)
                    store.state["providerAttemptCounters"][retry_key] = max(0, attempts - 1)
                task_state.pop("error", None)
        store.state["pendingRecovery"]["completed"].append(f"{action['action']}:{assignment_id}")
        store.save()
    if any(action["action"] == "defer" for action in actions):
        write_bugs(store, bugs)
    store.update(lambda state: (state.setdefault("recoveryHistory", []).append({"recoveredAt": datetime.now(timezone.utc).isoformat(), "actions": actions}), state.__setitem__("pendingRecovery", None), state.__setitem__("phase", "build")))


def reconcile(store: StateStore) -> None:
    repository = Path(store.state["repository"])
    if git(repository, "rev-parse", "--show-toplevel", timeout=store.state["providerTimeoutSeconds"], check=False).returncode == 0:
        root, prefix = repository_layout(repository, store.state["providerTimeoutSeconds"])
        if store.state.get("repositoryRoot") != str(root) or store.state.get("repositoryPrefix") != prefix:
            store.state.update(repositoryRoot=str(root), repositoryPrefix=prefix)
            store.save()
        exclude_relay_files(repository)
    metadata, ledger_tasks = parse_tasks((Path(store.state["repository"]) / "tasks.md").read_text(encoding="utf-8"), runtime=True)
    store.state.setdefault("requirementsHash", metadata["requirementsHash"])
    if "campaignValidationCommands" not in store.state or "baselineValidation" not in store.state:
        raise RuntimeError("campaign state is missing schema-3 validation fields; create a fresh plan")
    if store.state["campaignValidationCommands"] != metadata["campaignValidationCommands"]:
        raise RuntimeError("tasks.md campaign validation commands do not match campaign state")
    baseline = store.state.get("baselineValidation")
    if baseline and baseline.get("phase") in {"worktree", "running", "interrupted"}:
        baseline["phase"] = "interrupted"
        baseline["error"] = baseline.get("error") or "interrupted"
        store.save()
        cleanup_baseline_worktree(store)
        if "BASELINE" in store.state.get("worktrees", {}):
            raise RuntimeError("baseline worktree cleanup failed")
    recover_pending_ledger(store)
    if store.state["activeProcesses"]:
        for process in store.state["activeProcesses"].values():
            process["interrupted"] = True
        store.state.setdefault("interruptedProcesses", []).extend(store.state["activeProcesses"].values())
        store.state["activeProcesses"] = {}
    store.state.pop("tasks", None)
    store.state.pop("bugs", None)
    for task_state in store.state.get("taskStates", {}).values():
        task_state.setdefault("fixAttemptsStarted", 0)
        if re.fullmatch(r"validation-repair-\d+", str(task_state.get("phase"))):
            task_state.update(phase="needs-user", error="removed validation-repair phase requires replanning")
        for field in OPERATION_FIELDS:
            task_state.pop(field, None)
    if store.state.get("agentsBootstrap"):
        for field in OPERATION_FIELDS:
            store.state["agentsBootstrap"].pop(field, None)
    store.save()
    for task in load_tasks(store):
        task_state = store.state.get("taskStates", {}).get(task["id"], {})
        values = {
            "attempt": f"{store.state['attemptCounters'].get(task['id'], 0)}/{store.state['taskAttemptLimit']}",
            "fixLoop": f"{fix_attempts_started(store.state, task['id'])}/{store.state['fixLoopLimit']}",
        }
        if task_state.get("phase") == "integrated":
            pr = store.state.get("pullRequests", {}).get(task["id"], {})
            values.update(status="integrated", branch=task_state.get("branch", "pending"), pullRequest=str(pr.get("url", pr.get("number", "pending"))), candidate=task_state.get("candidateSha", "pending"))
        if task["attempt"] != store.state["attemptCounters"].get(task["id"], 0) or task["fixLoop"] != fix_attempts_started(store.state, task["id"]) or (task_state.get("phase") == "integrated" and task["status"] != "integrated"):
            update_task_ledger(store, task["id"], **values)
    bugs = load_bugs(store)
    changed = False
    for bug in bugs:
        task_state = store.state.get("taskStates", {}).get(bug["id"], {})
        if task_state.get("phase") == "integrated" and bug["status"] != "resolved":
            pr = store.state.get("pullRequests", {}).get(bug["id"], {})
            bug.update(status="resolved", branch=task_state.get("branch", "pending"), pullRequest=str(pr.get("url", pr.get("number", "pending"))), candidate=task_state.get("candidateSha", "pending"))
            changed = True
    if changed:
        write_bugs(store, bugs)


def heartbeat_loop(store: StateStore, stop: threading.Event) -> None:
    save_interval = min(30, max(1, store.state["agentTimeoutSeconds"] // 2))
    last_save = time.monotonic()
    relay_console.update(runtime_progress(store.state, load_bugs(store)))
    while not stop.wait(1):
        if relay_console.interactive():
            relay_console.update(runtime_progress(store.state, load_bugs(store)))
        if time.monotonic() - last_save >= save_interval:
            store.save()
            last_save = time.monotonic()


def run_assignments(store: StateStore, semaphore: threading.Semaphore, assignments: list[dict], mode: str) -> None:
    pending = {item["id"]: item for item in assignments if item["status"] != "satisfied"}
    satisfied = {item["id"] for item in assignments if item["status"] == "satisfied"}
    if pending:
        store.update(lambda state: state.__setitem__("phase", "build"))
    running, active_paths = {}, set()
    with ThreadPoolExecutor(max_workers=max(1, len(pending))) as pool:
        while pending or running:
            integrated = satisfied | {assignment_id for assignment_id, value in store.state["taskStates"].items() if value["phase"] == "integrated"}
            launched = False
            for assignment in sorted(pending.values(), key=lambda item: (int(item["priority"][1]), item["id"])):
                assignment_id = assignment["id"]
                phase = store.state["taskStates"].get(assignment_id, {}).get("phase")
                if phase in {"blocked", "needs-user", "waiting-provider"}:
                    pending.pop(assignment_id)
                    continue
                if set(assignment.get("dependencies", [])) <= integrated and not paths_conflict(assignment["allowedPaths"], active_paths):
                    running[pool.submit(process_assignment, store, semaphore, assignment, mode)] = assignment
                    active_paths.update(assignment["allowedPaths"])
                    pending.pop(assignment_id)
                    launched = True
            if running:
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    assignment = running.pop(future)
                    for path in assignment["allowedPaths"]:
                        active_paths.discard(path)
                    future.result()
            elif pending and not launched:
                break


def execute_campaign(store: StateStore, tasks: list[dict]) -> int:
    if not store.state.get("campaignValidationCommands") or not store.state.get("baselineValidation"):
        raise RuntimeError("campaign state is missing schema-3 validation fields; create a fresh plan")
    semaphore = threading.Semaphore(store.state["workerLimit"])
    require_validation_shell(store.state["validationTimeoutSeconds"])
    if not run_baseline_validation(store):
        return 2
    if (store.state.get("agentsBootstrap") or {}).get("phase") == "needs-user":
        store.update(lambda state: state.__setitem__("phase", "needs-user"))
        return 2
    provider_preflight(store)
    if not bootstrap_agents(store):
        return 2
    by_id = {task["id"]: task for task in tasks}
    run_assignments(store, semaphore, tasks, "task")
    unfinished = [value for key, value in store.state["taskStates"].items() if key in by_id and value["phase"] != "integrated"]
    if unfinished:
        terminal = "waiting-provider" if all(value["phase"] == "waiting-provider" for value in unfinished) else "blocked" if any(value["phase"] == "blocked" for value in unfinished) else "needs-user"
        store.update(lambda state: state.__setitem__("phase", terminal))
        return 1 if terminal == "blocked" else 2
    repository = Path(store.state["repository"])
    git_provider_with_retries(store, "audit:fetch", repository, "fetch", "origin", "main")
    git(repository, "merge", "--ff-only", "origin/main", timeout=store.state["validationTimeoutSeconds"])
    store.update(lambda state: state.__setitem__("phase", "audit"))
    try:
        bugs = run_audit(store, semaphore, tasks)
        if store.state["phase"] in {"blocked", "needs-user"}:
            return 1 if store.state["phase"] == "blocked" else 2
        if bugs:
            run_assignments(store, semaphore, [bug_assignment(store, bug) for bug in bugs], "bug")
        unresolved = [bug for bug in load_bugs(store) if bug["status"] == "active"]
        if unresolved:
            store.update(lambda state: state.__setitem__("phase", "needs-user"))
            return 2
        publish_backlog(store, load_bugs(store))
        store.update(lambda state: state.__setitem__("phase", "complete"))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        phase = stop_phase(error)
        store.update(lambda state: state.update(phase=phase, error=str(error)))
        return 1 if phase == "blocked" else 2


def blocked_assignments(state: dict) -> list[tuple[str, str]]:
    blocked = []
    baseline = state.get("baselineValidation") or {}
    if baseline.get("phase") in {"blocked", "interrupted"}:
        blocked.append(("BASELINE", str(baseline.get("error") or baseline["phase"])))
    if state.get("error"):
        blocked.append(("CAMPAIGN", str(state["error"])))
    bootstrap = state.get("agentsBootstrap") or {}
    if bootstrap.get("phase") in {"needs-user", "waiting-provider"}:
        blocked.append(("AGENTS", str(bootstrap.get("providerStatus") or bootstrap["phase"])))
    for assignment_id, task in state.get("taskStates", {}).items():
        if task.get("phase") in {"blocked", "needs-user", "waiting-provider"}:
            blocked.append((assignment_id, str(task.get("error") or task.get("providerStatus") or task["phase"])))
    return sorted(blocked) or [("CAMPAIGN", "action required")]


def _shell_join(arguments: list[str]) -> str:
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def _normalized_error(value: object) -> str:
    text = " ".join(str(value or "action required").split())
    return re.sub(r";?\s*log:\s*[^\r\n]+$", "", text).strip()


def _worktree_setup_failure(value: object) -> bool:
    text = str(value or "")
    return text.startswith("worktree setup failed:") or ("'worktree', 'add'" in text and "non-zero exit status" in text)


def _provider_exhaustion(value: object) -> tuple[str, str] | None:
    match = re.match(r"^(Azure DevOps|GitHub) operation exhausted attempts: ([^;\s]+)", _normalized_error(value))
    return (match.group(1), match.group(2)) if match else None


def _publication_retry_key(value: object, assignment_id: str) -> str | None:
    failure = _provider_exhaustion(value)
    if not failure or not failure[1].startswith(f"{assignment_id}:"):
        return None
    operation = failure[1][len(assignment_id) + 1:]
    return failure[1] if operation in {"pr-list", "pr-create"} or re.fullmatch(r"pr-(?:refresh|edit):[0-9a-f]+", operation) else None


def _blocker_detail(value: object) -> tuple[str, str]:
    if _worktree_setup_failure(value):
        return "worktree-setup", "Git could not create assignment worktrees before Worker launch"
    provider = _provider_exhaustion(value)
    if provider:
        operation = provider[1].split(":", 1)[-1].split(":", 1)[0]
        labels = {"pr-list": "PR discovery", "pr-create": "PR creation", "pr-refresh": "PR refresh", "pr-edit": "PR metadata update"}
        if operation in labels:
            return "provider-publication", f"{provider[0]} {labels[operation]} exhausted attempts"
    return "assignment", _normalized_error(value)


def _blocker_log(state: dict, value: object) -> str:
    match = re.search(r"log:\s*([^\r\n]+)", str(value or ""))
    if match:
        return match.group(1)
    return str(state_path(state, "logs/provider.log")) if _provider_exhaustion(value) else "not-recorded"


def campaign_summary(state: dict, tasks: list[dict], bugs: list[dict]) -> tuple[list[str], str]:
    task_states = state.get("taskStates", {})
    interrupted = {item.get("assignmentId") for item in state.get("activeProcesses", {}).values() if item.get("interrupted")}
    counts = Counter()
    bullets = []
    blocked_groups: dict[tuple[str, str, str], list[str]] = {}
    for task in sorted(tasks, key=lambda item: item["id"]):
        task_state = task_states.get(task["id"], {})
        label = display_assignment(state, task["id"])
        phase = task_state.get("phase")
        if task["status"] == "satisfied":
            status = "satisfied"
            detail = f"no implementation was required — {' '.join(task['acceptanceCriteria'][0].split())}"
        elif phase == "integrated":
            status = "completed"
            summary = task_state.get("workerSummary")
            validation = "focused and campaign validation passed"
            detail = f"{summary}; {validation}" if summary else f"reviewed candidate {task_state.get('candidateSha', 'unknown')} integrated; {validation}"
        elif phase == "waiting-provider":
            status = "waiting"
            pr = task_state.get("pr") or state.get("pullRequests", {}).get(task["id"], {})
            detail = f"{task_state.get('providerStatus', 'provider action pending')}"
            if pr:
                detail += f"; PR {pr.get('url', pr.get('number', 'unknown'))}"
            if task_state.get("operationDeadline"):
                detail += f"; deadline {task_state['operationDeadline']}"
            detail += f"; log {state_path(state, 'logs/provider.log')}"
        elif phase in {"blocked", "needs-user"} or task["id"] in interrupted:
            status = "blocked"
            parts = [task_state.get("workerSummary")]
            if task["id"] in interrupted:
                parts.append(f"interrupted during {task_state.get('operation', phase or 'assignment')}")
            else:
                parts.append(_normalized_error(task_state.get("error") or task_state.get("providerStatus")))
            if task_state.get("validationFailure"):
                parts.append(f"{task_state['validationFailure']['category']} validation")
            if task_state.get("validationLog"):
                parts.append(f"log {task_state['validationLog']}")
            detail = "; ".join(part for part in parts if part)
        else:
            status = "not-run"
            unmet = [dependency for dependency in task["dependencies"] if task_states.get(dependency, {}).get("phase") != "integrated" and next((item for item in tasks if item["id"] == dependency), {}).get("status") != "satisfied"]
            detail = f"unmet dependencies: {', '.join(unmet)}" if unmet else "campaign stopped before launch"
        fixes = fix_attempts_started(state, task["id"])
        if fixes:
            detail += f"; fix loop {fixes}/{state.get('fixLoopLimit', fixes)}"
        counts[status] += 1
        if status == "blocked" and not task_state.get("validationFailure"):
            blocker = task_state.get("error") or task_state.get("providerStatus") or detail
            category, reason = _blocker_detail(blocker)
            blocked_groups.setdefault((category, reason, _blocker_log(state, blocker)), []).append(label)
        else:
            bullets.append(f"- {task['id']} {status}: {task['title']} — {detail}")
    bullets = [re.sub(r"^- ((?:TASK|BUG)-\d{4}) ", lambda match: f"- {display_assignment(state, match.group(1))} ", line) for line in bullets]
    bug_counts = Counter(bug["status"] for bug in bugs)
    lines = [f"phase={state.get('phase', 'unknown')} tasks={counts['completed']}/{len(tasks)} completed blocked={counts['blocked']} waiting={counts['waiting']} satisfied={counts['satisfied']} not-run={counts['not-run']} bugs={len(bugs)} backlog={bug_counts['backlog']}"]
    for assignment_id in sorted(set(state.get("attemptCounters", {})) | set(task_states)):
        lines.append(
            f"- BUDGET {display_assignment(state, assignment_id)} attempts={state.get('attemptCounters', {}).get(assignment_id, 0)}/{state.get('taskAttemptLimit', 0)} "
            f"fixes={fix_attempts_started(state, assignment_id)}/{state.get('fixLoopLimit', 0)}"
        )
    baseline = state.get("baselineValidation")
    if baseline:
        phase = baseline.get("phase", "unknown")
        if phase == "passed":
            lines.append(f"- BASELINE passed: {baseline.get('baseSha', state.get('baseSha', 'unknown'))} commands={len(state.get('campaignValidationCommands', []))}")
        elif phase == "blocked":
            lines.append(f"- BASELINE blocked: {_normalized_error(baseline.get('error'))}; command={baseline.get('currentCommand')}; log={baseline.get('log') or baseline.get('validationLog') or 'not-recorded'}")
        elif phase == "interrupted":
            lines.append(f"- BASELINE interrupted: command={baseline.get('currentCommand')} state={baseline.get('operation', 'interrupted')}")
    validation_groups: dict[tuple[str, str, str], dict] = {}
    for assignment_id, task_state in sorted(task_states.items()):
        failure = task_state.get("validationFailure")
        if task_state.get("phase") != "needs-user" or not isinstance(failure, dict):
            continue
        key = (str(failure.get("category")), str(failure.get("commandHash")), str(failure.get("outcome")))
        group = validation_groups.setdefault(key, {"command": failure.get("command", "unknown"), "required": failure.get("requiredExternalChange", "make the command pass"), "ids": [], "logs": []})
        group["ids"].append(assignment_id)
        group["logs"].append(task_state.get("validationLog", "not-recorded"))
    for (category, _command_hash, outcome), group in sorted(validation_groups.items()):
        lines.append(f"- VALIDATION BLOCKER category={category} outcome={outcome} command={group['command']} affected={','.join(display_assignment(state, assignment_id) for assignment_id in group['ids'])} required={group['required']} logs={','.join(group['logs'])}")
    for (category, reason, log), assignment_ids in sorted(blocked_groups.items()):
        lines.append(f"- BLOCKED category={category} affected={','.join(assignment_ids)} reason={reason} log={log}")
    lines.extend(bullets)
    for bug in sorted(bugs, key=lambda item: item["id"]):
        status = "waiting-provider" if task_states.get(bug["id"], {}).get("phase") == "waiting-provider" else bug["status"]
        bug_state = task_states.get(bug["id"], {})
        if status == "resolved":
            detail = bug_state.get("workerSummary") or f"{bug['requirement']} satisfied"
        elif status == "backlog":
            detail = f"{bug['failure']} Deferred because {bug.get('deferralReason') or 'Reason not recorded by the originating campaign'}"
        elif status == "waiting-provider":
            detail = f"{bug_state.get('providerStatus', 'provider action pending')}; {bug['evidence']}"
        else:
            detail = f"{_normalized_error(bug_state.get('error') or bug_state.get('providerStatus') or status)}; {bug['evidence']}"
        fixes = fix_attempts_started(state, bug["id"])
        if fixes:
            detail += f"; fix loop {fixes}/{state.get('fixLoopLimit', fixes)}"
        lines.append(f"- {display_assignment(state, bug['id'])} {status}: {detail}")
    executable = sys.executable
    run_path = str(Path(__file__).resolve())
    repo = state["repository"]
    recovery = [executable, run_path, "--repo", repo, "--recover"]
    preview, confirm = _shell_join(recovery), _shell_join([*recovery, "--confirm"])
    baseline = state.get("baselineValidation") or {}
    if state.get("phase") == "complete":
        cleanup = _shell_join([executable, run_path, "--repo", repo, "--cleanup"])
        confirm_cleanup = _shell_join([executable, run_path, "--repo", repo, "--cleanup", "--confirm"])
        next_line = f"Review {Path(repo) / 'BACKLOG.md'}, then preview cleanup with: {cleanup}; then confirm with: {confirm_cleanup}" if bug_counts["backlog"] else f"Preview cleanup with: {cleanup}; then confirm with: {confirm_cleanup}"
    elif state.get("phase") == "interrupted":
        next_line = f"Resume with: {_shell_join([executable, run_path, '--repo', repo])}"
    elif state.get("phase") == "blocked":
        next_line = "Inspect the recorded coordinator or infrastructure evidence; no automatic recovery command is safe."
    elif state.get("phase") in {"needs-user", "waiting-provider"} or baseline.get("phase") in {"blocked", "interrupted"}:
        next_line = f"Preview resume/defer recovery with: {preview}; then confirm with: {confirm}. Use --grant-attempt or --defer-blocker only for the explicit assignment or blocker selected."
    else:
        next_line = f"Continue with: {_shell_join([executable, run_path, '--repo', repo])}"
    return lines, next_line


def state_path(state: dict, relative: str) -> Path:
    return Path(state["repository"]) / ".relay" / relative


def emit_campaign_summary(store: StateStore, tasks: list[dict]) -> None:
    try:
        lines, next_line = campaign_summary(store.state, tasks, load_bugs(store))
        for line in lines:
            relay_console.emit("SUMMARY", line)
        relay_console.emit("NEXT", next_line)
    except Exception as error:
        try:
            relay_console.emit("SUMMARY", f"unavailable reason={str(error).splitlines()[0]}")
        except Exception:
            print(f"SUMMARY unavailable: {str(error).splitlines()[0]}", file=sys.stderr)


def report_stopped(state: dict) -> None:
    blocked = blocked_assignments(state)
    relay_console.emit("STOPPED", f"phase={state.get('phase', 'unknown')} assignments={len(blocked)}")
    groups: dict[tuple[str, str, str], list[str]] = {}
    for assignment_id, reason in blocked:
        category, compact = _blocker_detail(reason)
        log = _blocker_log(state, reason)
        groups.setdefault((category, compact, log), []).append(assignment_id)
    for (category, reason, log), assignment_ids in sorted(groups.items()):
        relay_console.emit("BLOCKED", f"category={category} affected={','.join(sorted(assignment_ids))} reason={reason} log={log}")


def validate_cleanup_provider_proof(state: dict, assignment_id: str) -> None:
    task_state = state.get("taskStates", {}).get(assignment_id, {})
    candidate = task_state.get("candidateSha")
    metadata = task_state.get("prMetadata")
    proof = task_state.get("providerProof")
    if not candidate or not isinstance(metadata, dict) or not isinstance(proof, dict):
        raise RuntimeError(f"cleanup requires provider proof for {assignment_id}")
    record = proof.get("mergedProviderRecord")
    persisted = state.get("pullRequests", {}).get(assignment_id)
    if (
        proof.get("finalCandidate") != candidate
        or proof.get("prMetadataHash") != metadata.get("hash")
        or metadata.get("candidateSha") != candidate
        or not proof.get("mergeMetadataHash")
        or not isinstance(record, dict)
        or record.get("state") != "MERGED"
        or record.get("headRefOid") != candidate
        or persisted != record
    ):
        raise RuntimeError(f"cleanup provider proof is stale for {assignment_id}")


def permanent_cleanup(repo: Path, confirm: bool) -> int:
    root, relay = repo.resolve(), repo.resolve() / ".relay"
    state_path = relay / "state.json"
    if not state_path.is_file():
        raise RuntimeError("no Relay campaign to clean")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if Path(state["repository"]).resolve() != root or state["phase"] != "complete" or state["activeProcesses"] or state["worktrees"]:
        raise RuntimeError("cleanup requires a complete inactive campaign with no worktrees")
    if any(value.get("state") == "OPEN" for value in state["pullRequests"].values()):
        raise RuntimeError("cleanup refuses open Relay PRs")
    tasks, bugs = root / "tasks.md", root / "bugs.md"
    if not tasks.is_file() or not MARKER.search(tasks.read_text(encoding="utf-8")):
        raise RuntimeError("tasks.md ownership mismatch")
    bug_marker = f"<!-- relay: campaign={state['campaignId']} repository={hashlib.sha256(str(root).encode()).hexdigest()[:12]} -->"
    if not bugs.is_file() or bug_marker not in bugs.read_text(encoding="utf-8"):
        raise RuntimeError("bugs.md ownership mismatch")
    for assignment_id, task_state in state.get("taskStates", {}).items():
        if task_state.get("phase") == "integrated":
            validate_cleanup_provider_proof(state, assignment_id)
    plans = []
    for item in root.iterdir():
        if "plan" not in item.name.lower() or not item.is_file():
            continue
        try:
            parse_tasks(item.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        plans.append(item)
    targets = [safe_within(item, root) for item in (tasks, bugs, *sorted(plans), relay)]
    for target in targets:
        print(f"{'REMOVE' if confirm else 'WOULD REMOVE'} {target}")
    if not confirm:
        return 0
    tasks.unlink()
    bugs.unlink()
    for plan in plans:
        plan.unlink()
    cleanup_campaign_environment(state)
    shutil.rmtree(relay)
    prefix = state.get("repositoryPrefix", "")
    found = git(root, "rev-parse", "--git-path", "info/exclude", check=False)
    value = Path(found.stdout.strip()) if found.returncode == 0 else Path(".git/info/exclude")
    exclude = value if value.is_absolute() else root / value
    if exclude.is_file():
        entries = {f"{prefix}/{name}" if prefix else name for name in ("tasks.md", "bugs.md", ".relay/")}
        lines = [line for line in exclude.read_text(encoding="utf-8").splitlines() if line not in entries]
        atomic_write(exclude, "\n".join(lines) + ("\n" if lines else ""))
    return 0


def initialize_campaign(repo: Path, text: str, args: argparse.Namespace) -> tuple[StateStore, list[dict]]:
    metadata, tasks = parse_tasks(text)
    if metadata["taskAttemptLimit"] != args.task_attempts or metadata["fixLoopLimit"] != args.fix_loops:
        raise RuntimeError("plan limits do not match --task-attempts and --fix-loops")
    head = git(repo, "rev-parse", "HEAD", timeout=args.provider_timeout).stdout.strip()
    if head != metadata["baseSha"]:
        raise RuntimeError(f"planned base {metadata['baseSha']} does not match HEAD {head}")
    for name in ("tasks.md", "bugs.md"):
        path = repo / name
        if path.exists():
            raise RuntimeError(f"refusing existing {path}")
    instructions, agents_content = target_instructions(repo, create=True)
    relay = repo / ".relay"
    relay.mkdir(parents=True, exist_ok=False)
    (relay / "logs").mkdir()
    state = initial_state(repo, metadata, args)
    state["pathDirectories"] = sorted({
        normalized_path(path) for task in tasks for path in task["allowedPaths"]
        if not path_has_magic(path) and (repo / path).is_dir()
    })
    state["taskTotal"] = len(tasks)
    repository_root, prefix = repository_layout(repo, args.provider_timeout)
    state.update(repositoryRoot=str(repository_root), repositoryPrefix=prefix)
    state["campaignId"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    state["targetInstructions"] = instructions
    tracked_agents = git(repo, "cat-file", "-e", f"{metadata['baseSha']}:{repository_path(state, 'AGENTS.md')}", timeout=args.provider_timeout, check=False).returncode == 0
    generated = agents_content == TARGET_AGENTS.encode()
    marked = agents_content.startswith(GENERATED_AGENTS_MARKER.encode())
    if marked and not generated:
        state["phase"] = "needs-user"
        state["agentsBootstrap"] = {"phase": "needs-user", "contentHash": TARGET_AGENTS_SHA256, "providerStatus": "generated-file-modified", "terminalCondition": "exact generated AGENTS.md merged into main"}
    elif generated and not tracked_agents:
        branch = f"relay/agents-bootstrap-{state['campaignId']}"
        campaign_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / state["campaignId"]
        worktree_root = safe_within(campaign_root / "AGENTS", campaign_root)
        worktree = safe_within(worktree_root / Path(prefix), worktree_root)
        state["agentsBootstrap"] = {
            "phase": "pending", "contentHash": TARGET_AGENTS_SHA256, "baseSha": metadata["baseSha"],
            "branch": branch, "worktree": str(worktree), "worktreeRoot": str(worktree_root), "candidateSha": "", "pushedSha": "", "pr": None,
            "providerStatus": "pending", "terminalCondition": "exact generated AGENTS.md merged into main",
        }
    store = StateStore(relay / "state.json", state)
    atomic_write(repo / "tasks.md", text)
    atomic_write(repo / "bugs.md", render_bugs(state["campaignId"], repo))
    exclude_relay_files(repo)
    store.save()
    return store, tasks


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = args.repo.resolve()
    if not repo.is_dir():
        parser().error("repository does not exist")
    if (args.defer_blocker or args.grant_attempt) and not args.recover:
        parser().error("--defer-blocker and --grant-attempt require --recover")
    if args.recover and (args.cleanup or args.dry_run or args.plan):
        parser().error("--recover cannot be combined with --cleanup, --dry-run, or --plan")
    if args.cleanup:
        try:
            return permanent_cleanup(repo, args.confirm)
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
            relay_console.emit("FAILED", operation="cleanup", reason=str(error).splitlines()[0])
            return 1
    stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    relay_state = repo / ".relay" / "state.json"
    try:
        if args.dry_run:
            text = stdin_text
            if not text:
                plan_path = (args.plan or repo / "PLAN.md").resolve()
                text = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else ""
            if not text:
                raise ValueError("a plan on stdin or in PLAN.md is required for --dry-run")
            parse_tasks(text)
            return 0
        if relay_state.exists():
            if stdin_text.strip() or args.plan:
                raise RuntimeError("resume does not accept a new plan")
            state = json.loads(relay_state.read_text(encoding="utf-8"))
            if state.get("schemaVersion") != STATE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported campaign state schema {state.get('schemaVersion')!r}; extract incomplete requirements and backlog items, "
                    "archive the old campaign externally, and generate a fresh schema-3 plan; do not edit state.json"
                )
            if Path(state["repository"]).resolve() != repo:
                raise RuntimeError("campaign repository mismatch")
            store, tasks = StateStore(relay_state, state), None
            resuming = True
        else:
            text = stdin_text
            if not text:
                plan_path = (args.plan or repo / "PLAN.md").resolve()
                if not plan_path.is_file():
                    raise ValueError(f"plan does not exist: {plan_path}")
                text = plan_path.read_text(encoding="utf-8")
            if not text:
                raise ValueError("a plan on stdin or in PLAN.md is required for a new campaign")
            # Read and validate all stdin before creating any target file.
            parse_tasks(text)
            store, tasks = initialize_campaign(repo, text, args)
            resuming = False
        if args.recover and not resuming:
            raise RuntimeError("recovery requires an existing campaign")
        if args.recover and not args.confirm:
            print_recovery(plan_recovery(store, load_tasks(store), args.defer_blocker, args.grant_attempt))
            return 0
        relay_console.emit("START", f"operation=campaign campaign={store.state['campaignId']} workers={store.state['workerLimit']} tasks={store.state.get('taskTotal', len(tasks or []))}")
        with coordinator_lock(store.path.parent):
            if resuming:
                reconcile(store)
                tasks = load_tasks(store)
            if args.recover:
                actions = plan_recovery(store, tasks, args.defer_blocker, args.grant_attempt)
                print_recovery(actions)
                apply_recovery(store, tasks, actions)
                relay_console.emit("NEXT", f"Resume with: {_shell_join([sys.executable, str(Path(__file__).resolve()), '--repo', str(repo)])}")
                return 0
            stop = threading.Event()
            heartbeat = threading.Thread(target=heartbeat_loop, args=(store, stop), daemon=True)
            heartbeat.start()
            try:
                try:
                    result = execute_campaign(store, tasks)
                    if result:
                        report_stopped(store.state)
                    else:
                        relay_console.emit("COMPLETE", f"operation=campaign integrated={store.state.get('taskTotal', len(tasks))}/{store.state.get('taskTotal', len(tasks))}")
                    emit_campaign_summary(store, tasks)
                    return result
                except KeyboardInterrupt:
                    terminate_children()
                    for process in store.state["activeProcesses"].values():
                        process["interrupted"] = True
                    store.update(lambda state: state.update(phase="interrupted", interruptedAt=datetime.now(timezone.utc).isoformat()))
                    relay_console.emit("STOPPED", "operation=campaign reason=interrupted")
                    emit_campaign_summary(store, tasks)
                    return 130
            finally:
                stop.set()
                heartbeat.join()
                relay_console.close()
    except Exception as error:
        if not (args.recover and not args.confirm) and "store" in locals() and isinstance(store, StateStore):
            with contextlib.suppress(Exception):
                store.update(lambda state: state.update(phase="blocked", error=str(error), blockedEvidence={"type": type(error).__name__, "message": str(error)}))
        relay_console.emit("FAILED", operation="campaign", reason=str(error).splitlines()[0])
        relay_console.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
