#!/usr/bin/env python3
"""Read-only Relay campaign status."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from run import parse_bugs, parse_tasks


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Show a Relay campaign without changing it.")
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
    active = [item for item in state["activeProcesses"].values() if item.get("status", "running") == "running"]
    queued = [item for item in state["activeProcesses"].values() if item.get("status") == "queued"]
    processes = Counter(f"{item['role']}" + (f" ({item['mode']})" if item.get("mode") else "") for item in active)
    phases = Counter(value["phase"] for value in state.get("taskStates", {}).values())
    bugs = Counter(f"{item['severity']} {item['status']}" for item in ledger_bugs)
    print(f"Relay campaign: {state['campaignId']}\nPhase:          {state['phase']}\nElapsed:        {age(state['createdAt'])}\nHeartbeat age:  {age(state['heartbeat'])}")
    print(f"\nProcesses\n  Configured: {state['workerLimit']}\n  Active:     {len(active)}\n  Queued:     {len(queued)}")
    for role, count in sorted(processes.items()):
        print(f"  {role}: {count}")
    for process_id, process in sorted(state["activeProcesses"].items()):
        if process.get("status") == "queued":
            print(f"  {process_id}: queued")
            continue
        deadline = datetime.fromisoformat(process["startedAt"]).timestamp() + process["deadlineSeconds"]
        print(f"  {process_id}: deadline-in={max(0, int(deadline - datetime.now().timestamp()))}s")
    print("\nReview sessions")
    for assignment_id, session in sorted(state["reviewSessions"].items()):
        print(f"  {assignment_id}: {session['phase']} calls={session['reviewCallsStarted']}/{session['reviewCallLimit']} fixes={session['repairAttemptsStarted']}/{state['fixLoopLimit']}")
    integrated = {key for key, value in state.get("taskStates", {}).items() if value["phase"] == "integrated"}
    ready = sum(task["status"] != "satisfied" and set(task["dependencies"]) <= integrated and task["id"] not in integrated and state.get("taskStates", {}).get(task["id"], {}).get("phase") not in {"needs-user", "waiting-provider"} for task in tasks)
    print(f"\nTasks\n  Total: {len(tasks)}\n  Ready: {ready}")
    for phase, count in sorted(phases.items()):
        print(f"  {phase}: {count}")
    print("\nAttempts and deadlines")
    for assignment_id, count in sorted(state["attemptCounters"].items()):
        print(f"  {assignment_id}: attempts={count}/{state['taskAttemptLimit']} validations={state.get('validationCommandsStarted', {}).get(assignment_id, 0)} agent={state['agentTimeoutSeconds']}s validation={state['validationTimeoutSeconds']}s")
    for assignment_id, deadline in sorted(state.get("providerDeadlines", {}).items()):
        print(f"  {assignment_id}: provider-check-deadline-in={max(0, int(deadline - datetime.now().timestamp()))}s")
    print("\nBugs")
    for label, count in sorted(bugs.items()):
        print(f"  {label}: {count}")
    print("\nWorktrees")
    for assignment_id, record in sorted(state["worktrees"].items()):
        print(f"  {assignment_id}: {record['path']}")
    print("\nPull requests")
    for assignment_id, pr in sorted(state["pullRequests"].items()):
        provider = state["taskStates"].get(assignment_id, {}).get("providerStatus", "unknown")
        print(f"  {assignment_id}: #{pr.get('number')} {pr.get('state', 'unknown')} checks={provider} {pr.get('url', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
