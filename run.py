#!/usr/bin/env python3
"""Deterministic Relay coordinator."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

MARKER = re.compile(r"<!-- relay: planned-base=([0-9a-f]{7,64}) requirements=([0-9a-f]{6,64}) -->")
TASK_HEADING = re.compile(r"^## (TASK-\d{4}) — (.+)$")
BUG_HEADING = re.compile(r"^## (BUG-\d{4}) — (.+)$")
TASK_FIELDS = ("Status", "Priority", "Dependencies", "Allowed paths", "Acceptance criteria", "Validation", "Attempt", "Fix loop", "Branch", "Pull request", "Candidate")
WORKER_MODES = {"task", "bug", "repair"}
TERMINAL_REVIEW_PHASES = {"approved", "needs-user"}

AGENT_SCHEMAS = {
    "worker": {"mode": str, "assignmentId": str, "status": str, "candidateSha": str, "changedPaths": list, "validation": list, "summary": str},
    "contract-reviewer": {"assignmentId": str, "candidateSha": str, "findings": list},
    "risk-reviewer": {"assignmentId": str, "candidateSha": str, "findings": list},
    "triage-pm": {"assignmentId": str, "decisions": list},
    "verification-reviewer": {"assignmentId": str, "candidateSha": str, "status": str},
    "audit-planner": {"scopes": list},
    "audit-worker": {"scopeId": str, "findings": list},
}


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run or resume a bounded Relay campaign.")
    result.add_argument("--repo", required=True, type=Path)
    result.add_argument("--workers", type=positive, default=3)
    result.add_argument("--fix-loops", type=positive, default=2)
    result.add_argument("--task-attempts", type=positive, default=3)
    result.add_argument("--format-retries", type=positive, default=2)
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


def parse_tasks(text: str) -> tuple[dict, list[dict]]:
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
        if task["status"] not in {"ready", "blocked", "satisfied"} or task["priority"] not in {"P0", "P1", "P2", "P3"}:
            raise ValueError(f"invalid task metadata for {task['id']}")
        if set(task["dependencies"]) - known or task["id"] in task["dependencies"]:
            raise ValueError(f"invalid dependency for {task['id']}")
        if not task["allowedPaths"] or not task["acceptanceCriteria"] or not task["validationCommands"]:
            raise ValueError(f"empty contract for {task['id']}")
    return {"baseSha": marker.group(1), "requirementsHash": marker.group(2)}, tasks


def validate_agent_result(role: str, value: object, assignment_id: str | None = None, mode: str | None = None) -> dict:
    schema = AGENT_SCHEMAS[role]
    if not isinstance(value, dict):
        raise ValueError("agent result must be an object")
    for key, kind in schema.items():
        if key not in value or not isinstance(value[key], kind):
            raise ValueError(f"invalid {role} result field: {key}")
    if assignment_id is not None and value.get("assignmentId") != assignment_id:
        raise ValueError("agent changed assignment ID")
    if role == "worker" and (mode not in WORKER_MODES or value["mode"] != mode):
        raise ValueError("agent changed Worker mode")
    return value


def review_call_limit(fix_loops: int, format_retries: int) -> int:
    return 2 + 1 + 2 * fix_loops + format_retries


def legal_review_targets(phase: str, fix_loop_limit: int) -> set[str]:
    if phase == "initial-review":
        return {"triage", "needs-user"}
    if phase == "triage":
        return {"approved", "repair-1", "needs-user"}
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
        (task for task in tasks if task["status"] == "ready" and set(task["dependencies"]) <= integrated and not paths_conflict(task["allowedPaths"], active_paths)),
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
        "activeProcesses": {}, "worktrees": {}, "candidateShas": {}, "attemptCounters": {},
        "reviewSessions": {}, "pullRequests": {}, "providerAttemptCounters": {},
        "auditPlanStarted": False, "auditPlanCompleted": False, "auditCallsStarted": 0,
        "auditCallLimit": 0, "auditScopes": {}, "pendingLedgerOperation": None,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cleanup:
        return 0
    text = sys.stdin.read()
    if not text:
        parser().error("a plan on stdin is required for a new campaign")
    parse_tasks(text)
    if args.dry_run:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
