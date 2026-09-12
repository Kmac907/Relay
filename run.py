#!/usr/bin/env python3
"""Deterministic Relay coordinator."""
from __future__ import annotations

import argparse
import contextlib
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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from pathlib import Path

MARKER = re.compile(r"<!-- relay: planned-base=([0-9a-f]{7,64}) requirements=([0-9a-f]{6,64}) -->")
TASK_HEADING = re.compile(r"^## (TASK-\d{4}) — (.+)$")
BUG_HEADING = re.compile(r"^## (BUG-\d{4}) — (.+)$")
BUG_MARKER = re.compile(r"<!-- relay: campaign=([A-Za-z0-9._-]+) repository=([0-9a-f]{12}) -->")
TASK_FIELDS = ("Status", "Priority", "Dependencies", "Allowed paths", "Acceptance criteria", "Validation", "Attempt", "Fix loop", "Branch", "Pull request", "Candidate")
WORKER_MODES = {"task", "bug", "repair"}
TERMINAL_REVIEW_PHASES = {"approved", "needs-user"}
CONSOLE_LOCK = threading.Lock()
CHILD_LOCK = threading.Lock()
ACTIVE_CHILDREN: set[subprocess.Popen] = set()

AGENT_SCHEMAS = {
    "worker": {"mode": str, "assignmentId": str, "status": str, "candidateSha": str, "changedPaths": list, "validation": list, "summary": str},
    "contract-reviewer": {"assignmentId": str, "candidateSha": str, "findings": list},
    "risk-reviewer": {"assignmentId": str, "candidateSha": str, "findings": list},
    "triage-pm": {"assignmentId": str, "decisions": list},
    "verification-reviewer": {"assignmentId": str, "candidateSha": str, "status": str},
    "audit-planner": {"scopes": list},
    "audit-worker": {"scopeId": str, "findings": list},
}

FINDING_FIELDS = {"id": str, "severity": str, "location": str, "failure": str, "reproduction": str, "requirement": str, "evidence": str, "candidateIntroduced": bool}
ROLE_PROMPTS = {
    "contract-reviewer": "Check only acceptance criteria, required behavior/tests, and candidate-introduced regressions. Do not inspect unrelated code.",
    "risk-reviewer": "Independently check only candidate correctness, regression, security/data-loss, changed error paths, and missing candidate tests.",
    "triage-pm": "Decide each supplied finding once: accept-blocker, backlog, discard, or needs-user. Unsupported and pre-existing findings cannot block.",
    "verification-reviewer": "Verify only the accepted blocker and exact repair delta. Return resolved, unresolved, or invalid-result; do not reopen full review.",
    "audit-planner": "Define one finite list of explicit audit scopes. Never request an unrestricted search or another audit.",
    "audit-worker": "Inspect only the assigned finite scope, read-only, and return evidence-backed findings. Do not create more work.",
}


def _json_object(properties: dict[str, object]) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _string_array() -> dict:
    return {"type": "array", "items": {"type": "string"}}


FINDING_JSON = _json_object({
    "id": {"type": "string"}, "severity": {"type": "string"}, "location": {"type": "string"},
    "failure": {"type": "string"}, "reproduction": {"type": "string"}, "requirement": {"type": "string"},
    "evidence": {"type": "string"}, "candidateIntroduced": {"type": "boolean"},
})
ROLE_JSON_SCHEMAS = {
    "worker": _json_object({
        "mode": {"type": "string"}, "assignmentId": {"type": "string"}, "status": {"type": "string"},
        "candidateSha": {"type": "string"}, "changedPaths": _string_array(),
        "validation": {"type": "array", "items": _json_object({"command": {"type": "string"}, "exitCode": {"type": "integer"}})},
        "summary": {"type": "string"},
    }),
    "contract-reviewer": _json_object({"assignmentId": {"type": "string"}, "candidateSha": {"type": "string"}, "findings": {"type": "array", "items": FINDING_JSON}}),
    "risk-reviewer": _json_object({"assignmentId": {"type": "string"}, "candidateSha": {"type": "string"}, "findings": {"type": "array", "items": FINDING_JSON}}),
    "triage-pm": _json_object({"assignmentId": {"type": "string"}, "decisions": {"type": "array", "items": _json_object({"findingId": {"type": "string"}, "action": {"type": "string"}, "reason": {"type": "string"}})}}),
    "verification-reviewer": _json_object({"assignmentId": {"type": "string"}, "candidateSha": {"type": "string"}, "status": {"type": "string"}}),
    "audit-planner": _json_object({"scopes": {"type": "array", "items": _json_object({"scopeId": {"type": "string"}, "scope": {"type": "string"}, "requirements": _string_array(), "paths": _string_array(), "commands": _string_array(), "completionCondition": {"type": "string"}})}}),
    "audit-worker": _json_object({"scopeId": {"type": "string"}, "findings": {"type": "array", "items": FINDING_JSON}}),
}


def console(event: str, detail: str) -> None:
    with CONSOLE_LOCK:
        print(f"{datetime.now():%H:%M:%S}  {event:<10} {detail}", flush=True)


def bounded_run(command, *, timeout: int, cwd: Path | None = None, check: bool = True, input: str | None = None, shell: bool = False) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, cwd=cwd, shell=shell, stdin=subprocess.PIPE if input is not None else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with CHILD_LOCK:
        ACTIVE_CHILDREN.add(process)
    try:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
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
    result = argparse.ArgumentParser(description="Run or resume a bounded Relay campaign.")
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
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--cleanup", action="store_true")
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
        tasks.append({
            "id": match.group(1), "title": match.group(2), "status": _field(block, "Status"),
            "priority": _field(block, "Priority"), "dependencies": dependencies,
            "allowedPaths": _sublist(block, "Allowed paths"),
            "acceptanceCriteria": _sublist(block, "Acceptance criteria"),
            "validationCommands": _sublist(block, "Validation"),
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
    return {"baseSha": marker.group(1), "requirementsHash": marker.group(2)}, tasks


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
    if role == "worker" and (mode not in WORKER_MODES or value["mode"] != mode):
        raise ValueError("agent changed Worker mode")
    if role in {"contract-reviewer", "risk-reviewer", "audit-worker"}:
        for finding in value["findings"]:
            if not isinstance(finding, dict) or any(not isinstance(finding.get(key), kind) for key, kind in FINDING_FIELDS.items()):
                raise ValueError("invalid evidence-backed finding")
            if finding["severity"] not in {"P0", "P1", "P2", "P3"}:
                raise ValueError("invalid finding severity")
    if role == "triage-pm" and any(not isinstance(item, dict) or set(("findingId", "action", "reason")) - item.keys() or item.get("action") not in {"accept-blocker", "backlog", "discard", "needs-user"} for item in value["decisions"]):
        raise ValueError("invalid triage decision")
    if role == "verification-reviewer" and value["status"] not in {"resolved", "unresolved", "invalid-result"}:
        raise ValueError("invalid verification status")
    return value


def review_call_limit(fix_loops: int, format_retries: int) -> int:
    return 2 + 1 + 2 * fix_loops + format_retries


def legal_review_targets(phase: str, fix_loop_limit: int) -> set[str]:
    if phase == "initial-review":
        return {"triage", "needs-user"}
    if phase == "triage":
        return {"approved", "needs-user"} | ({"repair-1"} if fix_loop_limit else set())
    match = re.fullmatch(r"repair-(\d+)", phase)
    if match:
        number = int(match.group(1))
        return {f"verify-{number}", "needs-user"} if number <= fix_loop_limit else set()
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
    if target not in legal_review_targets(phase, fix_loop_limit):
        raise ValueError(f"illegal review transition: {phase} -> {target}")
    session["phase"] = target


def ready_tasks(tasks: list[dict], integrated: set[str], active_paths: set[str] | None = None) -> list[dict]:
    active_paths = active_paths or set()
    return sorted(
        (task for task in tasks if task["status"] in {"ready", "blocked"} and set(task["dependencies"]) <= integrated and not paths_conflict(task["allowedPaths"], active_paths)),
        key=lambda item: (int(item["priority"][1]), item["id"]),
    )


def paths_conflict(paths: list[str], active: set[str]) -> bool:
    def overlaps(left: str, right: str) -> bool:
        left, right = left.replace("\\", "/").rstrip("/"), right.replace("\\", "/").rstrip("/")
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")
    return any(overlaps(path, other) for path in paths for other in active)


def initial_state(repo: Path, metadata: dict, args: argparse.Namespace) -> dict:
    return {
        "schemaVersion": 1, "campaignId": "", "repository": str(repo.resolve()), "phase": "build",
        "baseSha": metadata["baseSha"], "workerLimit": args.workers, "taskAttemptLimit": args.task_attempts,
        "fixLoopLimit": args.fix_loops, "formatRetryAllowance": args.format_retries,
        "agentTimeoutSeconds": args.agent_timeout, "validationTimeoutSeconds": args.validation_timeout,
        "providerTimeoutSeconds": args.provider_timeout, "providerCheckTimeoutSeconds": args.provider_check_timeout,
        "providerAttemptLimit": args.provider_attempts, "mergeMethod": args.merge_method,
        "createdAt": datetime.now(timezone.utc).isoformat(), "heartbeat": datetime.now(timezone.utc).isoformat(),
        "activeProcesses": {}, "worktrees": {}, "candidateShas": {}, "attemptCounters": {}, "validationCommandsStarted": {}, "taskStates": {},
        "reviewSessions": {}, "pullRequests": {}, "providerAttemptCounters": {}, "providerOperationsStarted": 0, "providerDeadlines": {},
        "auditPlanStarted": False, "auditPlanCompleted": False, "auditCallsStarted": 0,
        "auditCallLimit": 0, "auditScopes": {}, "pendingLedgerOperation": None,
    }


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


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


@contextlib.contextmanager
def coordinator_lock(relay: Path):
    lock = relay / "coordinator.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        os.close(descriptor)
    except FileExistsError as error:
        raise RuntimeError(f"another Relay coordinator holds {lock}") from error
    try:
        yield lock
    finally:
        lock.unlink(missing_ok=True)


def tool_command(tool: str) -> list[str]:
    return shlex.split(os.environ.get(f"RELAY_{tool.upper()}", tool), posix=os.name != "nt")


def run_tool(tool: str, *args: str, timeout: int, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return bounded_run(tool_command(tool) + list(args), cwd=cwd, check=check, timeout=timeout)


def git(repo: Path, *args: str, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    return run_tool("git", "-C", str(repo), *args, timeout=timeout, check=check)


def safe_within(path: Path, root: Path) -> Path:
    resolved, resolved_root = path.resolve(), root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"path escapes safe root: {resolved}")
    return resolved


def exclude_relay_files(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    entries = ["tasks.md", "bugs.md", ".relay/"]
    missing = [entry for entry in entries if entry not in existing.splitlines()]
    if missing:
        atomic_write(exclude, existing + ("" if not existing or existing.endswith("\n") else "\n") + "\n".join(missing) + "\n")


def render_bugs(campaign: str, repo: Path, bugs: list[dict] | None = None) -> str:
    lines = ["# Bugs", "", f"<!-- relay: campaign={campaign} repository={hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]} -->", ""]
    for bug in bugs or []:
        lines += [
            f"## {bug['id']} — {bug['title']}", "", f"- Severity: {bug['severity']}", f"- Status: {bug['status']}",
            f"- Source: {bug['source']}", f"- Location: {bug['location']}", f"- Observable failure: {bug['failure']}",
            f"- Reproduction: `{bug['reproduction']}`", f"- Requirement: {bug['requirement']}", f"- Evidence: {bug['evidence']}",
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
            "reproduction": reproduction[1:-1] if reproduction.startswith("`") and reproduction.endswith("`") else reproduction,
            "requirement": values["Requirement"], "evidence": values["Evidence"], "branch": values["Branch"],
            "pullRequest": values["Pull request"], "candidate": values["Candidate"],
        })
    ids = [bug["id"] for bug in bugs]
    if len(ids) != len(set(ids)) or any(bug["severity"] not in {"P0", "P1", "P2", "P3"} or bug["status"] not in {"active", "backlog", "resolved", "needs-user", "waiting-provider"} for bug in bugs):
        raise ValueError("invalid bug ledger")
    return {"campaignId": marker.group(1), "repositoryHash": marker.group(2)}, bugs


def update_task_ledger(path: Path, assignment_id: str, **values: str) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index(f"## {assignment_id} ")
    next_start = text.find("\n## ", start + 1)
    end = len(text) if next_start < 0 else next_start
    block = text[start:end]
    names = {"status": "Status", "branch": "Branch", "pullRequest": "Pull request", "candidate": "Candidate"}
    for key, value in values.items():
        label = names[key]
        block, count = re.subn(rf"^- {re.escape(label)}:.*$", f"- {label}: {value}", block, count=1, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"ledger field missing: {label}")
    atomic_write(path, text[:start] + block + text[end:])


def worker_prompt(mode: str, assignment: dict, candidate_sha: str = "", blockers: list[dict] | None = None) -> str:
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
Implement only this assignment, run validation, commit the candidate locally, and return the required JSON."""


def role_prompt(role: str, assignment: dict, candidate_sha: str, context: object) -> str:
    return f"""Role: {role}
Assignment ID: {assignment['id']}
You are read-only. Do not edit, spawn agents, write ledgers, push, merge, or request a new review session.
{ROLE_PROMPTS[role]}
Candidate SHA: {candidate_sha}
Contract: {json.dumps(assignment)}
Bounded context: {json.dumps(context)}
Return only the required JSON."""


def _consume_agent_call(store: StateStore, assignment_id: str, role: str, mode: str | None, review: bool, audit: bool) -> tuple[int, str]:
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
            if state["attemptCounters"].get(assignment_id, 0) >= state["taskAttemptLimit"]:
                raise RuntimeError("implementation attempt limit exhausted")
            state["attemptCounters"][assignment_id] = state["attemptCounters"].get(assignment_id, 0) + 1
            number = state["attemptCounters"][assignment_id]
        process_id = f"{assignment_id}:{role}:{number}"
        state["activeProcesses"][process_id] = {"assignmentId": assignment_id, "role": role, "mode": mode, "startedAt": datetime.now(timezone.utc).isoformat(), "deadlineSeconds": state["agentTimeoutSeconds"]}
        result.update(number=number, process_id=process_id)
    store.update(change)
    console("START", f"role={role}" + (f" mode={mode}" if mode else "") + f" assignment={assignment_id} call={result['number']}")
    return result["number"], result["process_id"]


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
    try:
        with semaphore:
            completed = bounded_run(command, input=prompt, timeout=store.state["agentTimeoutSeconds"])
        atomic_write(log, completed.stdout + ("\n--- stderr ---\n" + completed.stderr if completed.stderr else ""))
        if completed.returncode or not output.is_file():
            raise RuntimeError(f"{role} failed with exit code {completed.returncode}")
        result = json.loads(output.read_text(encoding="utf-8"))
        return validate_agent_result(role, result, assignment_id if role != "audit-planner" else None, mode)
    except subprocess.TimeoutExpired as error:
        atomic_write(log, f"timed out after {store.state['agentTimeoutSeconds']} seconds\n")
        raise RuntimeError(f"{role} timed out") from error
    finally:
        schema.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        store.update(lambda state: state["activeProcesses"].pop(process_id, None))


def invoke_with_replacements(store: StateStore, semaphore: threading.Semaphore, repo: Path, assignment_id: str, role: str, prompt: str, *, mode: str | None = None, review: bool = False, audit: bool = False) -> dict:
    error = None
    for _ in range(store.state["formatRetryAllowance"] + 1):
        try:
            return invoke_agent(store, semaphore, repo, assignment_id, role, prompt, mode=mode, review=review, audit=audit)
        except (ValueError, json.JSONDecodeError, RuntimeError) as caught:
            error = caught
            if review:
                session = store.state["reviewSessions"][assignment_id]
                if session["reviewCallsStarted"] >= session["reviewCallLimit"]:
                    break
            elif audit and store.state["auditCallsStarted"] >= store.state["auditCallLimit"]:
                break
    raise RuntimeError(f"{role} exhausted structured-output budget: {error}") from error


def create_worktree(store: StateStore, assignment: dict) -> tuple[Path, str]:
    assignment_id = assignment["id"]
    with store.lock:
        existing = store.state["worktrees"].get(assignment_id)
        if existing:
            return Path(existing["path"]), existing["branch"]
        campaign_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / store.state["campaignId"]
        path = safe_within(campaign_root / assignment_id, campaign_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"relay/{assignment_id}"
        repository = Path(store.state["repository"])
        git_provider_with_retries(store, f"{assignment_id}:fetch", repository, "fetch", "origin", "main")
        remote_base = git(repository, "rev-parse", "origin/main", timeout=store.state["providerTimeoutSeconds"], check=False)
        assignment_base = remote_base.stdout.strip() if remote_base.returncode == 0 else store.state["baseSha"]
        git(repository, "worktree", "add", "-b", branch, str(path), assignment_base, timeout=store.state["providerTimeoutSeconds"])
        store.state["worktrees"][assignment_id] = {"path": str(path), "branch": branch, "baseSha": assignment_base}
        store.save()
        return path, branch


def allowed_change(path: str, allowed: list[str]) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    return any(normalized == item.replace("\\", "/").strip("/") or normalized.startswith(item.replace("\\", "/").strip("/") + "/") for item in allowed)


def validate_candidate(store: StateStore, assignment: dict, worktree: Path, result: dict) -> str:
    sha = git(worktree, "rev-parse", "HEAD", timeout=store.state["validationTimeoutSeconds"]).stdout.strip()
    if result["candidateSha"] != sha:
        raise ValueError("reported candidate does not equal worktree HEAD")
    assignment_base = store.state["worktrees"][assignment["id"]]["baseSha"]
    ancestry = git(worktree, "merge-base", "--is-ancestor", assignment_base, sha, timeout=store.state["validationTimeoutSeconds"], check=False)
    if ancestry.returncode:
        raise ValueError("candidate does not descend from expected base")
    changed = [item for item in git(worktree, "diff", "--name-only", f"{assignment_base}..{sha}", timeout=store.state["validationTimeoutSeconds"]).stdout.splitlines() if item]
    if sorted(changed) != sorted(result["changedPaths"]) or any(not allowed_change(item, assignment["allowedPaths"]) for item in changed):
        raise ValueError("candidate changed paths outside assignment scope")
    for command in assignment["validationCommands"]:
        store.update(lambda state, value=command: state["validationCommandsStarted"].__setitem__(assignment["id"], state["validationCommandsStarted"].get(assignment["id"], 0) + 1))
        completed = bounded_run(command, cwd=worktree, shell=True, timeout=store.state["validationTimeoutSeconds"])
        if completed.returncode:
            raise RuntimeError(f"validation failed: {command}\n{completed.stdout}\n{completed.stderr}")
    store.update(lambda state: (state["candidateShas"].__setitem__(assignment["id"], sha), state["taskStates"][assignment["id"]].update(candidateSha=sha, phase="push-and-open-pr")))
    console("CANDIDATE", f"assignment={assignment['id']} sha={sha[:12]} validation=passed")
    return sha


def provider_call(store: StateStore, key: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = {}
    def consume(state: dict) -> None:
        count = state["providerAttemptCounters"].get(key, 0)
        if count >= state["providerAttemptLimit"]:
            raise RuntimeError(f"provider attempt limit exhausted: {key}")
        state["providerAttemptCounters"][key] = count + 1
        result["count"] = count + 1
    store.update(consume)
    return run_tool("gh", *args, timeout=store.state["providerTimeoutSeconds"], check=check)


def provider_with_retries(store: StateStore, key: str, *args: str) -> subprocess.CompletedProcess:
    error = None
    while store.state["providerAttemptCounters"].get(key, 0) < store.state["providerAttemptLimit"]:
        try:
            return provider_call(store, key, *args)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as caught:
            error = caught
    raise RuntimeError(f"provider operation exhausted attempts: {key}") from error


def git_provider_with_retries(store: StateStore, key: str, repo: Path, *args: str) -> subprocess.CompletedProcess:
    last = None
    while store.state["providerAttemptCounters"].get(key, 0) < store.state["providerAttemptLimit"]:
        store.update(lambda state: state["providerAttemptCounters"].__setitem__(key, state["providerAttemptCounters"].get(key, 0) + 1))
        try:
            last = git(repo, *args, timeout=store.state["providerTimeoutSeconds"], check=False)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode == 0:
            return last
    raise RuntimeError(f"Git provider operation exhausted attempts: {key}; {last.stderr if last else ''}")


def publish_candidate(store: StateStore, assignment: dict, worktree: Path, branch: str, sha: str) -> dict:
    assignment_id, task_state = assignment["id"], store.state["taskStates"][assignment["id"]]
    if task_state.get("pushedSha") != sha:
        remote = git_provider_with_retries(store, f"{assignment_id}:ls-remote", worktree, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        if not remote.stdout.startswith(sha):
            git_provider_with_retries(store, f"{assignment_id}:push:{sha}", worktree, "push", "--set-upstream", "origin", branch)
        store.update(lambda state: state["taskStates"][assignment_id].update(pushed=True, pushedSha=sha))
        console("PUSHED", f"assignment={assignment_id} branch={branch}")
    if task_state.get("pr"):
        return task_state["pr"]
    body = store.path.parent / f".{assignment_id}-pr.md"
    atomic_write(body, f"Relay assignment {assignment_id}\n\nCandidate: {sha}\n")
    try:
        existing = provider_call(store, f"{assignment_id}:pr-list", "pr", "list", "--head", branch, "--state", "all", "--json", "number,url,headRefOid,state", check=False)
        matches = json.loads(existing.stdout) if existing.returncode == 0 and existing.stdout.strip() else []
        if matches:
            pr = matches[0]
        else:
            provider_with_retries(store, f"{assignment_id}:pr-create", "pr", "create", "--base", "main", "--head", branch, "--title", f"{assignment_id}: {assignment['title']}", "--body-file", str(body))
            view = provider_with_retries(store, f"{assignment_id}:pr-view", "pr", "view", branch, "--json", "number,url,headRefOid,state")
            pr = json.loads(view.stdout)
        store.update(lambda state: (state["taskStates"][assignment_id].__setitem__("pr", pr), state["pullRequests"].__setitem__(assignment_id, pr)))
        console("PR", f"assignment={assignment_id} number={pr.get('number')}")
        return pr
    finally:
        body.unlink(missing_ok=True)


def record_findings(store: StateStore, assignment_id: str, findings: list[dict], decisions: list[dict]) -> list[dict]:
    actions = {item["findingId"]: item["action"] for item in decisions}
    accepted = []
    bugs = store.state.setdefault("bugs", [])
    known = {bug["sourceFindingId"] for bug in bugs}
    for finding in findings:
        action = actions.get(finding["id"], "discard")
        if action == "accept-blocker" and (finding["severity"] not in {"P0", "P1"} or not finding["candidateIntroduced"] or not all(finding[key] for key in ("location", "failure", "reproduction", "requirement", "evidence"))):
            action = "discard"
        if finding["severity"] == "P2" and action == "accept-blocker":
            action = "backlog"
        if finding["severity"] == "P3":
            action = "discard"
        if action in {"accept-blocker", "backlog"}:
            if finding["id"] not in known:
                bug = {
                    "id": f"BUG-{len(bugs) + 1:04d}", "title": finding["failure"][:80], "severity": finding["severity"],
                    "status": "active" if action == "accept-blocker" else "backlog", "source": assignment_id,
                    "sourceFindingId": finding["id"], "location": finding["location"], "failure": finding["failure"],
                    "reproduction": finding["reproduction"], "requirement": finding["requirement"], "evidence": finding["evidence"],
                }
                bugs.append(bug)
                known.add(finding["id"])
            else:
                bug = next(item for item in bugs if item["sourceFindingId"] == finding["id"])
            if action == "accept-blocker":
                accepted.append(bug)
    store.save()
    atomic_write(store.path.parent.parent / "bugs.md", render_bugs(store.state["campaignId"], Path(store.state["repository"]), bugs))
    return accepted


def ensure_review_session(store: StateStore, assignment_id: str, sha: str) -> dict:
    if assignment_id not in store.state["reviewSessions"]:
        def create(state: dict) -> None:
            state["reviewSessions"][assignment_id] = {
                "reviewSessionId": f"{assignment_id}-REVIEW-1", "initialCandidateSha": sha, "reviewedSha": "",
                "phase": "initial-review", "initialReviewAssignmentsStarted": 0, "initialReviewAssignmentsCompleted": 0,
                "initialAssignmentsStartedRoles": [], "initialResults": {}, "triageCompleted": False, "acceptedBlockerIds": [], "repairAttemptsStarted": 0,
                "reviewCallsStarted": 0, "reviewCallLimit": review_call_limit(state["fixLoopLimit"], state["formatRetryAllowance"]),
            }
        store.update(create)
    return store.state["reviewSessions"][assignment_id]


def run_review(store: StateStore, semaphore: threading.Semaphore, assignment: dict, worktree: Path, sha: str) -> bool:
    assignment_id = assignment["id"]
    session = ensure_review_session(store, assignment_id, sha)
    try:
        if session["phase"] == "initial-review":
            roles = [role for role in ("contract-reviewer", "risk-reviewer") if role not in session["initialResults"]]
            if roles:
                def start_logical_assignments(state: dict) -> None:
                    current = state["reviewSessions"][assignment_id]
                    started = current.setdefault("initialAssignmentsStartedRoles", [])
                    new_roles = [role for role in roles if role not in started]
                    started.extend(new_roles)
                    current["initialReviewAssignmentsStarted"] += len(new_roles)
                store.update(start_logical_assignments)
                with ThreadPoolExecutor(max_workers=len(roles)) as pool:
                    futures = {pool.submit(invoke_with_replacements, store, semaphore, worktree, assignment_id, role, role_prompt(role, assignment, sha, {}), review=True): role for role in roles}
                    for future in as_completed(futures):
                        role, result = futures[future], future.result()
                        if result["candidateSha"] != sha:
                            raise ValueError(f"{role} changed candidate SHA")
                        store.update(lambda state, r=role, value=result: (state["reviewSessions"][assignment_id]["initialResults"].__setitem__(r, value), state["reviewSessions"][assignment_id].__setitem__("initialReviewAssignmentsCompleted", state["reviewSessions"][assignment_id]["initialReviewAssignmentsCompleted"] + 1)))
            transition_review(session, "triage", store.state["fixLoopLimit"])
            store.save()
        if session["phase"] == "triage":
            findings = [finding for result in session["initialResults"].values() for finding in result["findings"]]
            triage = invoke_with_replacements(store, semaphore, worktree, assignment_id, "triage-pm", role_prompt("triage-pm", assignment, sha, findings), review=True)
            if any(item["action"] == "needs-user" for item in triage["decisions"]):
                transition_review(session, "needs-user", store.state["fixLoopLimit"])
                session["triageCompleted"] = True
                store.save()
                return False
            with store.lock:
                accepted = record_findings(store, assignment_id, findings, triage["decisions"])
            def triaged(state: dict) -> None:
                current = state["reviewSessions"][assignment_id]
                current["triageCompleted"] = True
                current["acceptedBlockerIds"] = [item["id"] for item in accepted]
                transition_review(current, "repair-1" if accepted and state["fixLoopLimit"] else "needs-user" if accepted else "approved", state["fixLoopLimit"])
                current["reviewedSha"] = sha if not accepted else ""
            store.update(triaged)
            console("TRIAGE", f"assignment={assignment_id} blockers={len(accepted)}")
        while session["phase"].startswith("repair-"):
            number = int(session["phase"].split("-")[1])
            if session["repairAttemptsStarted"] >= store.state["fixLoopLimit"]:
                transition_review(session, "needs-user", store.state["fixLoopLimit"])
                store.save()
                return False
            store.update(lambda state: state["reviewSessions"][assignment_id].__setitem__("repairAttemptsStarted", state["reviewSessions"][assignment_id]["repairAttemptsStarted"] + 1))
            console("REPAIR", f"role=worker mode=repair assignment={assignment_id} fix={number}/{store.state['fixLoopLimit']}")
            blockers = [bug for bug in store.state.get("bugs", []) if bug["id"] in session["acceptedBlockerIds"]]
            repair = invoke_with_replacements(store, semaphore, worktree, assignment_id, "worker", worker_prompt("repair", assignment, sha, blockers), mode="repair", review=True)
            repaired_sha = validate_candidate(store, assignment, worktree, repair)
            transition_review(session, f"verify-{number}", store.state["fixLoopLimit"])
            store.save()
            verification = invoke_with_replacements(store, semaphore, worktree, assignment_id, "verification-reviewer", role_prompt("verification-reviewer", assignment, repaired_sha, {"blockers": blockers, "previousCandidate": sha, "repairDiff": f"{sha}..{repaired_sha}"}), review=True)
            console("VERIFY", f"role=verification-reviewer assignment={assignment_id} fix={number}/{store.state['fixLoopLimit']}")
            if verification["candidateSha"] != repaired_sha:
                raise ValueError("verification reviewer changed candidate SHA")
            sha = repaired_sha
            if verification["status"] == "resolved":
                transition_review(session, "approved", store.state["fixLoopLimit"])
                session["reviewedSha"] = sha
                with store.lock:
                    for bug in blockers:
                        bug["status"] = "resolved"
                    store.save()
                    atomic_write(store.path.parent.parent / "bugs.md", render_bugs(store.state["campaignId"], Path(store.state["repository"]), store.state.get("bugs", [])))
                break
            target = f"repair-{number + 1}" if number < store.state["fixLoopLimit"] else "needs-user"
            transition_review(session, target, store.state["fixLoopLimit"])
            store.save()
        return session["phase"] == "approved"
    except (RuntimeError, ValueError, json.JSONDecodeError):
        if session["phase"] not in TERMINAL_REVIEW_PHASES:
            session["phase"] = "needs-user"
            store.save()
        return False


def wait_for_checks(store: StateStore, assignment_id: str, pr: dict, reviewed_sha: str) -> str:
    if assignment_id not in store.state["providerDeadlines"]:
        store.update(lambda state: state["providerDeadlines"].__setitem__(assignment_id, time.time() + state["providerCheckTimeoutSeconds"]))
    deadline = store.state["providerDeadlines"][assignment_id]
    first = True
    while first or time.time() < deadline:
        first = False
        try:
            store.update(lambda state: state.__setitem__("providerOperationsStarted", state.get("providerOperationsStarted", 0) + 1))
            view = run_tool("gh", "pr", "view", str(pr["number"]), "--json", "headRefOid,mergeStateStatus,statusCheckRollup,state", timeout=store.state["providerTimeoutSeconds"], check=False)
        except (RuntimeError, subprocess.TimeoutExpired):
            key = f"{assignment_id}:check-errors"
            store.update(lambda state: state["providerAttemptCounters"].__setitem__(key, state["providerAttemptCounters"].get(key, 0) + 1))
            if store.state["providerAttemptCounters"][key] >= store.state["providerAttemptLimit"]:
                return "waiting-provider"
            continue
        if view.returncode:
            key = f"{assignment_id}:check-errors"
            store.update(lambda state: state["providerAttemptCounters"].__setitem__(key, state["providerAttemptCounters"].get(key, 0) + 1))
            if store.state["providerAttemptCounters"][key] >= store.state["providerAttemptLimit"]:
                return "waiting-provider"
            continue
        data = json.loads(view.stdout)
        if data["headRefOid"] != reviewed_sha:
            return "sha-drift"
        if data.get("state") == "MERGED":
            return "merged"
        checks = data.get("statusCheckRollup") or []
        states = {str(item.get("conclusion") or item.get("state") or item.get("status", "")).upper() for item in checks}
        if states & {"FAILURE", "FAILED", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
            return "failed"
        if states & {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED"}:
            time.sleep(min(10, max(0, deadline - time.time())))
            continue
        if data.get("mergeStateStatus") in {"BEHIND", "DIRTY"}:
            return "repair-required"
        if data.get("mergeStateStatus") in {"BLOCKED", "UNKNOWN"}:
            return "waiting-provider"
        return "passed"
    return "waiting-provider"


def merge_assignment(store: StateStore, semaphore: threading.Semaphore, assignment: dict, worktree: Path, branch: str, pr: dict, reviewed_sha: str) -> bool:
    assignment_id = assignment["id"]
    session = store.state["reviewSessions"][assignment_id]
    status = wait_for_checks(store, assignment_id, pr, reviewed_sha)
    while status in {"failed", "repair-required"}:
        if session["repairAttemptsStarted"] >= store.state["fixLoopLimit"] or session["reviewCallsStarted"] + 2 > session["reviewCallLimit"]:
            store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user", providerStatus=status))
            return False
        store.update(lambda state: state["reviewSessions"][assignment_id].__setitem__("repairAttemptsStarted", state["reviewSessions"][assignment_id]["repairAttemptsStarted"] + 1))
        blocker = [{"id": f"PROVIDER-{session['repairAttemptsStarted']}", "failure": status, "evidence": "GitHub checks or merge readiness failed"}]
        try:
            git_provider_with_retries(store, f"{assignment_id}:repair-fetch:{session['repairAttemptsStarted']}", worktree, "fetch", "origin", "main")
            current_base = git(worktree, "rev-parse", "origin/main", timeout=store.state["providerTimeoutSeconds"], check=False)
            if current_base.returncode == 0:
                store.state["worktrees"][assignment_id]["baseSha"] = current_base.stdout.strip()
                store.save()
            result = invoke_with_replacements(store, semaphore, worktree, assignment_id, "worker", worker_prompt("repair", assignment, reviewed_sha, blocker), mode="repair", review=True)
            replacement = validate_candidate(store, assignment, worktree, result)
            publish_candidate(store, assignment, worktree, branch, replacement)
            verification = invoke_with_replacements(store, semaphore, worktree, assignment_id, "verification-reviewer", role_prompt("verification-reviewer", assignment, replacement, {"previousCandidate": reviewed_sha, "repairDiff": f"{reviewed_sha}..{replacement}", "providerFailure": status}), review=True)
            if verification["candidateSha"] != replacement:
                raise ValueError("verification reviewer changed candidate SHA")
        except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user", providerStatus=status))
            return False
        if verification["status"] != "resolved":
            if session["repairAttemptsStarted"] >= store.state["fixLoopLimit"]:
                store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user", providerStatus=status))
                return False
            status = "failed"
            continue
        reviewed_sha = replacement
        session["reviewedSha"] = replacement
        store.state["providerDeadlines"].pop(assignment_id, None)
        store.save()
        status = wait_for_checks(store, assignment_id, pr, reviewed_sha)
    if status == "merged":
        store.update(lambda state: (state["taskStates"][assignment_id].update(phase="integrated", merged=True, providerStatus="passed"), state["pullRequests"][assignment_id].update(state="MERGED")))
        return True
    if status != "passed":
        store.update(lambda state: state["taskStates"][assignment_id].update(phase="needs-user" if status in {"failed", "sha-drift"} else "waiting-provider", providerStatus=status))
        return False
    provider_with_retries(store, f"{assignment_id}:merge", "pr", "merge", str(pr["number"]), f"--{store.state['mergeMethod']}", "--delete-branch")
    store.update(lambda state: (state["taskStates"][assignment_id].update(phase="integrated", merged=True, providerStatus="passed"), state["pullRequests"][assignment_id].update(state="MERGED")))
    console("MERGED", f"assignment={assignment_id} pr={pr['number']}")
    return True


def cleanup_worktree(store: StateStore, assignment_id: str) -> None:
    with store.lock:
        record = store.state["worktrees"].get(assignment_id)
        if not record:
            return
        root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / store.state["campaignId"]
        path = safe_within(Path(record["path"]), root)
        repository = Path(store.state["repository"])
        git(repository, "worktree", "remove", "--force", str(path), timeout=store.state["providerTimeoutSeconds"], check=False)
        git(repository, "branch", "-D", record["branch"], timeout=store.state["providerTimeoutSeconds"], check=False)
        store.state["worktrees"].pop(assignment_id, None)
        store.save()


def process_assignment(store: StateStore, semaphore: threading.Semaphore, assignment: dict, mode: str) -> bool:
    assignment_id = assignment["id"]
    def initialize(state: dict) -> None:
        state["taskStates"].setdefault(assignment_id, {"phase": "ready", "mode": mode, "pushed": False, "merged": False})
    store.update(initialize)
    task_state = store.state["taskStates"][assignment_id]
    if task_state["phase"] == "integrated":
        return True
    try:
        worktree, branch = create_worktree(store, assignment)
        task_state.update(worktree=str(worktree), branch=branch)
        store.save()
        sha = task_state.get("candidateSha")
        while not sha and store.state["attemptCounters"].get(assignment_id, 0) < store.state["taskAttemptLimit"]:
            task_state["phase"] = "implementing"
            store.save()
            try:
                result = invoke_with_replacements(store, semaphore, worktree, assignment_id, "worker", worker_prompt(mode, assignment), mode=mode)
                if result["status"] != "candidate":
                    raise ValueError("Worker did not return a candidate")
                task_state["phase"] = "candidate-validation"
                store.save()
                sha = validate_candidate(store, assignment, worktree, result)
            except (RuntimeError, ValueError, json.JSONDecodeError):
                sha = None
        if not sha:
            task_state["phase"] = "needs-user"
            store.save()
            return False
        pr = publish_candidate(store, assignment, worktree, branch, sha)
        task_state["phase"] = "initial-review"
        store.save()
        if not run_review(store, semaphore, assignment, worktree, sha):
            task_state["phase"] = "needs-user"
            store.save()
            return False
        session = store.state["reviewSessions"][assignment_id]
        task_state["phase"] = "approved"
        store.save()
        if not merge_assignment(store, semaphore, assignment, worktree, branch, pr, session["reviewedSha"]):
            return False
        if mode == "task":
            with store.lock:
                update_task_ledger(store.path.parent.parent / "tasks.md", assignment_id, status="integrated", branch=branch, pullRequest=str(pr.get("url", pr.get("number"))), candidate=session["reviewedSha"])
        else:
            with store.lock:
                for bug in store.state.get("bugs", []):
                    if bug["id"] == assignment_id:
                        bug.update(status="resolved", branch=branch, pullRequest=str(pr.get("url", pr.get("number"))), candidate=session["reviewedSha"])
                store.save()
                atomic_write(store.path.parent.parent / "bugs.md", render_bugs(store.state["campaignId"], Path(store.state["repository"]), store.state.get("bugs", [])))
        cleanup_worktree(store, assignment_id)
        return True
    except (RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        task_state.update(phase="needs-user", error=str(error))
        store.save()
        return False


def validate_audit_scopes(value: dict) -> list[dict]:
    scopes = value.get("scopes") if isinstance(value, dict) else None
    if not isinstance(scopes, list):
        raise ValueError("audit plan needs scopes")
    ids = []
    for scope in scopes:
        required = {"scopeId", "scope", "requirements", "paths", "commands", "completionCondition"}
        if not isinstance(scope, dict) or required - scope.keys() or not re.fullmatch(r"AUDIT-\d{4}", scope.get("scopeId", "")) or not all(isinstance(scope[key], list) and all(isinstance(item, str) for item in scope[key]) for key in ("requirements", "paths", "commands")) or not isinstance(scope["scope"], str) or not isinstance(scope["completionCondition"], str) or any(not valid_relative_path(path) for path in scope["paths"]):
            raise ValueError("invalid audit scope")
        ids.append(scope["scopeId"])
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate audit scope")
    return scopes


def run_audit(store: StateStore, semaphore: threading.Semaphore, tasks: list[dict]) -> list[dict]:
    if store.state["auditPlanCompleted"]:
        scopes = list(store.state["auditScopes"].values())
    else:
        if not store.state["auditPlanStarted"]:
            store.update(lambda state: state.update(auditPlanStarted=True, auditCallLimit=1 + state["formatRetryAllowance"]))
        try:
            result = invoke_with_replacements(store, semaphore, Path(store.state["repository"]), "AUDIT", "audit-planner", role_prompt("audit-planner", {"id": "AUDIT", "requirements": [], "allowedPaths": [], "acceptanceCriteria": [], "validationCommands": []}, store.state["baseSha"], {"tasks": tasks, "bugs": store.state.get("bugs", [])}), audit=True)
            scopes = validate_audit_scopes(result)
        except (RuntimeError, ValueError):
            store.update(lambda state: state.update(phase="needs-user"))
            return []
        def fixed(state: dict) -> None:
            state["auditScopes"] = {scope["scopeId"]: {**scope, "started": False, "completed": False, "findings": []} for scope in scopes}
            state["auditCallLimit"] = 1 + len(scopes) + 1 + state["formatRetryAllowance"]
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
                futures[pool.submit(invoke_with_replacements, store, semaphore, Path(store.state["repository"]), scope["scopeId"], "audit-worker", role_prompt("audit-worker", assignment, store.state["baseSha"], scope), audit=True)] = scope
            for future in as_completed(futures):
                scope, result = futures[future], future.result()
                scope.update(completed=True, findings=result["findings"])
                findings.extend(result["findings"])
                store.save()
    else:
        findings = [finding for scope in store.state["auditScopes"].values() for finding in scope["findings"]]
    if not store.state.get("auditTriageCompleted"):
        triage_assignment = {"id": "AUDIT", "allowedPaths": [], "acceptanceCriteria": [], "validationCommands": []}
        triage = invoke_with_replacements(store, semaphore, Path(store.state["repository"]), "AUDIT", "triage-pm", role_prompt("triage-pm", triage_assignment, store.state["baseSha"], findings), audit=True)
        if any(item["action"] == "needs-user" for item in triage["decisions"]):
            store.update(lambda state: state.update(phase="needs-user", auditTriageCompleted=True))
            return []
        actions = {item["findingId"]: item["action"] for item in triage["decisions"]}
        accepted = []
        for finding in findings:
            action = actions.get(finding["id"], "discard")
            if action == "accept-blocker" and finding["severity"] in {"P0", "P1"} and all(finding[key] for key in ("location", "failure", "reproduction", "requirement", "evidence")):
                bug = {
                    "id": f"BUG-{len(store.state.setdefault('bugs', [])) + 1:04d}", "title": finding["failure"][:80],
                    "severity": finding["severity"], "status": "active", "source": "audit", "sourceFindingId": finding["id"],
                    "location": finding["location"], "failure": finding["failure"], "reproduction": finding["reproduction"],
                    "requirement": finding["requirement"], "evidence": finding["evidence"],
                }
                store.state["bugs"].append(bug)
                scope = next((item for item in store.state["auditScopes"].values() if finding in item.get("findings", [])), None)
                bug["allowedPaths"] = (scope or {}).get("paths") or [finding["location"].replace("\\", "/").split(":", 1)[0]]
                accepted.append(bug)
            elif action == "backlog" and finding["severity"] == "P2":
                store.state.setdefault("bugs", []).append({"id": f"BUG-{len(store.state['bugs']) + 1:04d}", "title": finding["failure"][:80], "severity": "P2", "status": "backlog", "source": "audit", "sourceFindingId": finding["id"], "location": finding["location"], "failure": finding["failure"], "reproduction": finding["reproduction"], "requirement": finding["requirement"], "evidence": finding["evidence"]})
        store.state["auditTriageCompleted"] = True
        store.save()
        atomic_write(store.path.parent.parent / "bugs.md", render_bugs(store.state["campaignId"], Path(store.state["repository"]), store.state.get("bugs", [])))
        return accepted
    return [bug for bug in store.state.get("bugs", []) if bug["source"] == "audit" and bug["status"] == "active"]


def bug_assignment(bug: dict) -> dict:
    return {"id": bug["id"], "title": bug["title"], "status": "ready", "priority": bug["severity"], "dependencies": [], "allowedPaths": bug["allowedPaths"], "acceptanceCriteria": [f"Resolve: {bug['failure']}", f"Meet requirement: {bug['requirement']}"], "validationCommands": [bug["reproduction"]]}


def github_preflight(store: StateStore) -> None:
    if store.state.get("preflightCompleted"):
        return
    repository = Path(store.state["repository"])
    remote = git(repository, "remote", "get-url", "origin", timeout=store.state["providerTimeoutSeconds"]).stdout.strip()
    if not os.environ.get("RELAY_ALLOW_FAKE_PROVIDER") and not re.search(r"(?:github\.com[:/])[^/]+/[^/]+(?:\.git)?$", remote):
        raise RuntimeError("origin is not a GitHub repository")
    provider_with_retries(store, "preflight:auth", "auth", "status")
    provider_with_retries(store, "preflight:repo", "repo", "view")
    store.update(lambda state: state.__setitem__("preflightCompleted", True))


def reconcile(store: StateStore) -> None:
    if store.state["activeProcesses"]:
        for process in store.state["activeProcesses"].values():
            process["interrupted"] = True
        store.state.setdefault("interruptedProcesses", []).extend(store.state["activeProcesses"].values())
        store.state["activeProcesses"] = {}
    store.state["pendingLedgerOperation"] = None
    for task_state in store.state.get("taskStates", {}).values():
        if task_state.get("phase") == "waiting-provider":
            task_state["phase"] = "resume-provider"
    store.save()


def heartbeat_loop(store: StateStore, stop: threading.Event) -> None:
    while not stop.wait(min(30, max(1, store.state["agentTimeoutSeconds"] // 2))):
        store.save()
        console("ACTIVE", f"processes={len(store.state['activeProcesses'])}/{store.state['workerLimit']} phase={store.state['phase']}")


def run_assignments(store: StateStore, semaphore: threading.Semaphore, assignments: list[dict], mode: str) -> None:
    pending = {item["id"]: item for item in assignments if item["status"] != "satisfied"}
    satisfied = {item["id"] for item in assignments if item["status"] == "satisfied"}
    running, active_paths = {}, set()
    with ThreadPoolExecutor(max_workers=max(1, len(pending))) as pool:
        while pending or running:
            integrated = satisfied | {assignment_id for assignment_id, value in store.state["taskStates"].items() if value["phase"] == "integrated"}
            launched = False
            for assignment in sorted(pending.values(), key=lambda item: (int(item["priority"][1]), item["id"])):
                assignment_id = assignment["id"]
                phase = store.state["taskStates"].get(assignment_id, {}).get("phase")
                if phase in {"needs-user", "waiting-provider"}:
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
    semaphore = threading.Semaphore(store.state["workerLimit"])
    github_preflight(store)
    by_id = {task["id"]: task for task in tasks}
    run_assignments(store, semaphore, tasks, "task")
    unfinished = [value for key, value in store.state["taskStates"].items() if key in by_id and value["phase"] != "integrated"]
    if unfinished:
        terminal = "waiting-provider" if all(value["phase"] == "waiting-provider" for value in unfinished) else "needs-user"
        store.update(lambda state: state.__setitem__("phase", terminal))
        return 2
    store.update(lambda state: state.__setitem__("phase", "audit"))
    try:
        bugs = run_audit(store, semaphore, tasks)
        if store.state["phase"] == "needs-user":
            return 2
        if bugs:
            run_assignments(store, semaphore, [bug_assignment(bug) for bug in bugs], "bug")
        unresolved = [bug for bug in store.state.get("bugs", []) if bug["status"] == "active"]
        if unresolved:
            store.update(lambda state: state.__setitem__("phase", "needs-user"))
            return 2
        store.update(lambda state: state.__setitem__("phase", "complete"))
        return 0
    except (RuntimeError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        store.update(lambda state: state.update(phase="needs-user", error=str(error)))
        return 2


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
    targets = [safe_within(item, root) for item in (tasks, bugs, relay)]
    for target in targets:
        print(f"{'REMOVE' if confirm else 'WOULD REMOVE'} {target}")
    if not confirm:
        return 0
    tasks.unlink()
    bugs.unlink()
    shutil.rmtree(relay)
    exclude = root / ".git" / "info" / "exclude"
    if exclude.is_file():
        lines = [line for line in exclude.read_text(encoding="utf-8").splitlines() if line not in {"tasks.md", "bugs.md", ".relay/"}]
        atomic_write(exclude, "\n".join(lines) + ("\n" if lines else ""))
    return 0


def initialize_campaign(repo: Path, text: str, args: argparse.Namespace) -> tuple[StateStore, list[dict]]:
    metadata, tasks = parse_tasks(text)
    head = git(repo, "rev-parse", "HEAD", timeout=args.provider_timeout).stdout.strip()
    if head != metadata["baseSha"]:
        raise RuntimeError(f"planned base {metadata['baseSha']} does not match HEAD {head}")
    for name in ("tasks.md", "bugs.md"):
        path = repo / name
        if path.exists():
            raise RuntimeError(f"refusing existing {path}")
    relay = repo / ".relay"
    relay.mkdir(parents=True, exist_ok=False)
    (relay / "logs").mkdir()
    state = initial_state(repo, metadata, args)
    state["campaignId"] = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    state["tasks"] = tasks
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
    if args.cleanup:
        try:
            return permanent_cleanup(repo, args.confirm)
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
            print(f"run.py: {error}", file=sys.stderr)
            return 1
    text = sys.stdin.read() if not sys.stdin.isatty() else ""
    relay_state = repo / ".relay" / "state.json"
    try:
        if args.dry_run:
            if not text:
                raise ValueError("a plan on stdin is required for --dry-run")
            parse_tasks(text)
            return 0
        if relay_state.exists():
            if text.strip():
                raise RuntimeError("resume does not accept a new plan")
            state = json.loads(relay_state.read_text(encoding="utf-8"))
            if Path(state["repository"]).resolve() != repo:
                raise RuntimeError("campaign repository mismatch")
            store, tasks = StateStore(relay_state, state), state["tasks"]
            resuming = True
        else:
            if not text:
                raise ValueError("a plan on stdin is required for a new campaign")
            # Read and validate all stdin before creating any target file.
            parse_tasks(text)
            store, tasks = initialize_campaign(repo, text, args)
            resuming = False
        print(f"Relay\nRepository:   {repo}\nWorkers:      {store.state['workerLimit']} configured\nFix loops:    {store.state['fixLoopLimit']}\nReview calls: {review_call_limit(store.state['fixLoopLimit'], store.state['formatRetryAllowance'])} maximum per candidate")
        with coordinator_lock(store.path.parent):
            if resuming:
                reconcile(store)
            stop = threading.Event()
            heartbeat = threading.Thread(target=heartbeat_loop, args=(store, stop), daemon=True)
            heartbeat.start()
            try:
                try:
                    return execute_campaign(store, tasks)
                except KeyboardInterrupt:
                    terminate_children()
                    for process in store.state["activeProcesses"].values():
                        process["interrupted"] = True
                    store.update(lambda state: state.update(phase="interrupted", interruptedAt=datetime.now(timezone.utc).isoformat()))
                    return 130
            finally:
                stop.set()
                heartbeat.join()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"run.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
