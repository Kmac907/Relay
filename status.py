#!/usr/bin/env python3
"""Read-only Relay campaign status."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from run import _blocker_detail, _blocker_log, _publication_retry_key, display_assignment, parse_bugs, parse_tasks, repair_attempts_started, review_call_limit


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Show Relay work, human decisions, provider waits, and blocked operational failures without changing state.")
    result.add_argument("--repo", required=True, type=Path)
    return result


def age(iso: str) -> str:
    seconds = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()))
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = args.repo.resolve()
    state_path, tasks_path = repo / ".relay" / "state.json", repo / "tasks.md"
    if not state_path.is_file() or not tasks_path.is_file():
        parser().error("no Relay campaign found")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    _, tasks = parse_tasks(tasks_path.read_text(encoding="utf-8"), runtime=True)
    _, ledger_bugs = parse_bugs((repo / "bugs.md").read_text(encoding="utf-8"))
    now = datetime.now().timestamp()
    task_states = state.get("taskStates", {})
    label = lambda assignment_id: display_assignment(state, assignment_id)
    task_ids = {task["id"] for task in tasks}
    integrated = {key for key, value in task_states.items() if key in task_ids and value.get("phase") == "integrated"}
    print(f"Overall: {len(integrated)}/{len(tasks)} integrated")

    operations = []
    bootstrap = state.get("agentsBootstrap") or {}
    represented = set()
    for process in state.get("activeProcesses", {}).values():
        assignment_id = process.get("assignmentId", "unknown")
        operations.append((assignment_id, task_states.get(assignment_id, bootstrap if assignment_id == "AGENTS" else {}) | process | {"operation": process.get("role", "worker")}))
        represented.add(assignment_id)
    if bootstrap.get("operation") and "AGENTS" not in represented:
        operations.append(("AGENTS", bootstrap))
    for assignment_id, task in task_states.items():
        if task.get("operation") and assignment_id not in represented:
            operations.append((assignment_id, task))
    if operations:
        print("\nActive operations")
        for assignment_id, operation in sorted(operations):
            started = operation.get("operationStartedAt") or operation.get("startedAt") or operation.get("reservedAt")
            elapsed = age(started) if started else "not-started"
            deadline = operation.get("operationDeadline")
            command = f" command={operation['validationPosition']}/{operation.get('validationTotal', '?')}" if operation.get("validationPosition") else ""
            print(f"  {label(assignment_id)}: {operation.get('operation', 'unknown')}{command} elapsed={elapsed}" + (f" deadline-in={max(0, int(deadline - now))}s" if deadline else ""))

    waits = []
    for assignment_id, task in (("AGENTS", bootstrap), *sorted(task_states.items())):
        deadline = state.get("providerDeadlines", {}).get(assignment_id) or task.get("operationDeadline")
        if task and (task.get("operation") == "provider-checks" or task.get("phase") in {"approved", "provider-checks", "waiting-provider", "resume-provider"}) and (deadline or task.get("providerStatus")):
            pr = state.get("pullRequests", {}).get(assignment_id) or task.get("pr") or {}
            waits.append((assignment_id, pr, task, deadline))
    if waits:
        print("\nExternal/provider waits")
        for assignment_id, pr, task, deadline in waits:
            remaining = f"{max(0, int(deadline - now))}s" if deadline else "not-started"
            print(f"  {label(assignment_id)}: PR #{pr.get('number', '?')} status={task.get('providerStatus', 'pending')} deadline-in={remaining} next={task.get('nextAction', 'poll' if deadline else 'resume')}")

    blockers = []
    if state.get("error"):
        blockers.append(("CAMPAIGN", str(state["error"])))
    if bootstrap.get("phase") in {"blocked", "needs-user"}:
        blockers.append(("AGENTS", str(bootstrap.get("providerStatus") or bootstrap["phase"])))
    blockers += [(assignment_id, str(task.get("error") or task.get("providerStatus") or task["phase"])) for assignment_id, task in task_states.items() if task.get("phase") in {"blocked", "needs-user"}]
    if blockers:
        print("\nBlockers")
        groups = {}
        for assignment_id, reason in sorted(blockers):
            category, concise = _blocker_detail(reason)
            groups.setdefault((category, concise, _blocker_log(state, reason)), []).append(label(assignment_id))
        for (category, reason, log), assignment_ids in sorted(groups.items()):
            print(f"  category={category} affected={','.join(assignment_ids)} reason={reason} log={log}")

    bugs = Counter(f"{item['severity']} {item['status']}" for item in ledger_bugs)
    provider = "Azure DevOps" if state.get("provider") == "azure-devops" else "GitHub" if state.get("provider") == "github" or state.get("githubRepository") else "not-detected"
    print(f"\nCampaign\n  ID: {state['campaignId']}\n  Provider: {provider}\n  Phase: {state['phase']}\n  Elapsed: {age(state['createdAt'])}\n  Heartbeat age: {age(state['heartbeat'])}")
    if bootstrap:
        pr = bootstrap.get("pr") or {}
        print(f"\nAGENTS.md bootstrap\n  Phase: {bootstrap['phase']}\n  PR: {pr.get('url', pr.get('number', 'not-created'))}\n  Checks: {bootstrap.get('providerStatus', 'pending')}")
    ready = sum(task["status"] != "satisfied" and set(task["dependencies"]) <= integrated and task["id"] not in integrated and state.get("taskStates", {}).get(task["id"], {}).get("phase") not in {"blocked", "needs-user", "waiting-provider"} for task in tasks)
    print(f"\nTasks\n  Total: {len(tasks)}\n  Ready: {ready}")
    phases = Counter(value.get("phase", "unknown") for key, value in task_states.items() if key in task_ids)
    for phase, count in sorted(phases.items()): print(f"  {phase}: {count}")
    for task in tasks:
        if task.get("sourceRef"):
            print(f"  {label(task['id'])}: sourceRef={task['sourceRef']}")
    budget_ids = set(state.get("attemptCounters", {})) | {assignment_id for assignment_id, task in task_states.items() if task.get("validationRepairAttemptsStarted") or task.get("validationRepairCallsStarted")}
    if budget_ids or state.get("providerDeadlines"):
        print("\nBudgets and deadlines")
        for assignment_id in sorted(budget_ids):
            session = state.get("reviewSessions", {}).get(assignment_id)
            calls = session.get("reviewCallsStarted", 0) if session else task_states.get(assignment_id, {}).get("validationRepairCallsStarted", 0)
            call_limit = session.get("reviewCallLimit") if session else review_call_limit(state["fixLoopLimit"], state["formatRetryAllowance"])
            print(f"  {label(assignment_id)}: attempts={state.get('attemptCounters', {}).get(assignment_id, 0)}/{state['taskAttemptLimit']} fixes={repair_attempts_started(state, assignment_id)}/{state['fixLoopLimit']} review-calls={calls}/{call_limit} validations={state.get('validationCommandsStarted', {}).get(assignment_id, 0)} agent={state['agentTimeoutSeconds']}s validation={state['validationTimeoutSeconds']}s")
        for assignment_id, deadline in sorted(state.get("providerDeadlines", {}).items()): print(f"  {label(assignment_id)}: provider-check-deadline-in={max(0, int(deadline - now))}s")
    if state.get("reviewSessions"):
        print("\nReview sessions")
        for assignment_id, session in sorted(state["reviewSessions"].items()): print(f"  {label(assignment_id)}: {session['phase']} calls={session['reviewCallsStarted']}/{session['reviewCallLimit']} fixes={session['repairAttemptsStarted']}/{state['fixLoopLimit']}")
    if state.get("recoveryAttemptGrants") or state.get("recoveryHistory") or state.get("pendingRecovery"):
        print("\nRecovery")
        for assignment_id, count in sorted(state.get("recoveryAttemptGrants", {}).items()):
            print(f"  {label(assignment_id)}: grants={count} started={state.get('recoveryAttemptsStarted', {}).get(assignment_id, 0)}")
        for assignment_id, paths in sorted(state.get("recoveryAllowedPaths", {}).items()):
            print(f"  {label(assignment_id)}: adopted user deletions={','.join(paths)}")
        pending = state.get("pendingRecovery") or {}
        for action in pending.get("actions", []):
            key = f"{action['action']}:{action['assignmentId']}"
            print(f"  {label(action['assignmentId'])}: {action['action']} status={'done' if key in pending.get('completed', []) else 'required'}")
        for index, recovery in enumerate(state.get("recoveryHistory", []), 1):
            actions = ", ".join(f"{item['assignmentId']}:{item['action']}" for item in recovery["actions"])
            print(f"  history-{index} {recovery['recoveredAt']}: {actions}")
    recoverable = sorted(assignment_id for assignment_id, task in task_states.items() if task.get("phase") == "needs-user")
    if recoverable:
        print("\nRecoverable assignments")
        for assignment_id in recoverable:
            task = task_states[assignment_id]
            if _publication_retry_key(task.get("error"), assignment_id):
                action = "restore provider access, then use safe publication recovery"
            elif task.get("terminalValidationCandidateSha") and not task.get("terminalValidationReplayUsed"):
                action = "correct the validation condition, then use the printed replay or explicit grant command"
            else:
                action = "inspect with run.py --recover and an explicit disposition"
            print(f"  {label(assignment_id)}: {action}")
    if bugs:
        print("\nBugs")
        for label, count in sorted(bugs.items()): print(f"  {label}: {count}")
    if state.get("worktrees"):
        print("\nWorktrees")
        for assignment_id, record in sorted(state["worktrees"].items()): print(f"  {label(assignment_id)}: {record['path']}")
    if state.get("pullRequests"):
        print("\nPull requests")
        for assignment_id, pr in sorted(state["pullRequests"].items()): print(f"  {label(assignment_id)}: #{pr.get('number')} {pr.get('state', 'unknown')} checks={task_states.get(assignment_id, {}).get('providerStatus', bootstrap.get('providerStatus', 'unknown') if assignment_id == 'AGENTS' else 'unknown')} {pr.get('url', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
