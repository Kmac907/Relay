#!/usr/bin/env python3
"""Build in parallel, audit once, repair in parallel, validate, merge, clean up."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from collections import Counter

from plan import agent, decode, execute, items, progress, project, say

INTEGRATION = "relay/integration"


class Summary:
    """In-memory display only; never used to decide or resume work."""
    def __init__(self):
        self.lock = threading.Lock()
        self.phase = "startup"
        self.states = {}
        self.error = ""
        self.retained = None
        self.started = time.monotonic()

    def begin(self, phase, identities=()):
        with self.lock:
            self.phase = phase
            self.states = dict.fromkeys(identities, ("queued", ""))

    def set(self, identity, state, detail=""):
        with self.lock:
            self.states[identity] = (state, " ".join(detail.split())[:180])

    def __call__(self):
        with self.lock:
            if self.retained is not None:
                return (f"PROJECT MERGED | validation=passed | cleanup={'partial' if self.retained else 'complete'} "
                        f"| retained={self.retained} | total={int(time.monotonic() - self.started)}s")
            counts = Counter(state for state, _ in self.states.values())
            active = "; ".join(f"{key}:{state}" + (f" ({detail})" if detail else "")
                               for key, (state, detail) in self.states.items() if state != "merged")
            return (f"{self.phase.upper()} | agents={counts['agent']} validating={counts['validating']} "
                    f"conflicts={counts['resolving']} waiting={counts['ci'] + counts['merging'] + counts['queued'] + counts['publishing']} "
                    f"merged={counts['merged']}/{len(self.states)} blocked={counts['blocked']} "
                    f"total={int(time.monotonic() - self.started)}s"
                    + (f" | {active}" if active else "") + (f" | BLOCKED: {self.error}" if self.error else ""))


def check(repo: Path, command: str, log: Path) -> None:
    shell = ("pwsh", "-NoProfile", "-NonInteractive", "-Command") if os.name == "nt" else ("sh", "-c")
    execute(repo, *shell, command, log=log)


def pull_request(repo: Path, branch: str, base: str, title: str, body: Path) -> str:
    url = execute(repo, "gh", "pr", "create", "--head", branch, "--base", base,
                  "--title", title, "--body-file", str(body))
    say(f"[PR] {url}")
    return url


def wait_checks(repo: Path, url: str) -> None:
    checks = json.loads(execute(repo, "gh", "pr", "view", url, "--json", "statusCheckRollup"))
    if checks["statusCheckRollup"]:
        execute(repo, "gh", "pr", "checks", url, "--watch")


def merge(repo: Path, url: str, sha: str) -> None:
    execute(repo, "gh", "pr", "merge", url, "--merge", "--match-head-commit", sha)
    if json.loads(execute(repo, "gh", "pr", "view", url, "--json", "state"))["state"] != "MERGED":
        raise RuntimeError(f"PR has not merged: {url}")


def worktree(repo: Path, path: Path, base: str, branch: str | None = None) -> None:
    mode = ["-b", branch] if branch else ["--detach"]
    execute(repo, "git", "worktree", "add", *mode, str(path), base)
    source, destination = repo / "AGENTS.md", path / "AGENTS.md"
    if not destination.exists() or destination.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
        destination.write_bytes(source.read_bytes())


def cleanup_worktree(repo: Path, path: Path) -> None:
    root = path.resolve()
    if root == repo.resolve() or Path(execute(path, "git", "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError(f"not a temporary worktree root: {path}")
    # Only untracked Python bytecode; never delete source or tracked cache files.
    for name in execute(path, "git", "ls-files", "--others", "-z").split("\0"):
        relative = Path(name)
        if "__pycache__" not in relative.parts or relative.suffix != ".pyc":
            continue
        cache = path / relative
        if cache.is_symlink() or not cache.resolve().is_relative_to(root):
            continue
        cache.unlink()
        try:
            cache.parent.rmdir()
        except OSError:
            pass  # Other files in the directory must be retained.
    # Repair only checkout-format differences from older Relay AGENTS.md copies.
    if (execute(path, "git", "ls-tree", "--name-only", "HEAD", "--", "AGENTS.md")
            and execute(path, "git", "status", "--porcelain", "--", "AGENTS.md")
            and not execute(path, "git", "diff", "HEAD", "--", "AGENTS.md")
            and not execute(path, "git", "diff", "--cached", "--", "AGENTS.md")):
        execute(path, "git", "restore", "--worktree", "--", "AGENTS.md")
    execute(repo, "git", "worktree", "remove", str(path))


def integrate(repo, url, sha, item, kind, directory, context, merge_lock, summary):
    identity = item["id"]
    summary.set(identity, "ci", url)
    wait_checks(repo, url)
    try:
        summary.set(identity, "merging", url)
        with merge_lock:
            merge(repo, url, sha)
        return
    except RuntimeError:
        pr = json.loads(execute(repo, "gh", "pr", "view", url, "--json",
                                "mergeable,baseRefName,headRefName,headRefOid"))
        if pr["mergeable"] != "CONFLICTING" or pr["headRefOid"] != sha:
            raise

    summary.set(identity, "resolving", url)
    say(f"[{identity}] agent resolving merge conflict: {url}")
    execute(repo, "git", "fetch", "origin", pr["baseRefName"], pr["headRefName"])
    target = execute(repo, "git", "rev-parse", "origin/" + pr["baseRefName"])
    path = directory / f"{kind}-{identity}-merge"
    worktree(repo, path, sha)
    instructions = (path / "AGENTS.md").read_bytes()
    try:
        execute(path, "git", "merge", "--no-commit", "--no-ff", target)
    except RuntimeError as error:
        conflicts = execute(path, "git", "diff", "--name-only", "--diff-filter=U").splitlines()
        if not conflicts:
            raise
        agent(path, "merge", {**context, "item": item, "pr": url, "base": target,
                             "conflicts": conflicts, "git_output": str(error)},
              directory / f"{kind}-{identity}-merge.txt", write=True)
    if execute(path, "git", "diff", "--name-only", "--diff-filter=U"):
        raise RuntimeError(f"{url}: agent left unresolved conflicts; work retained at {path}")
    execute(path, "git", "diff", "--cached", "--check")
    if execute(path, "git", "rev-parse", "HEAD") != sha or execute(path, "git", "rev-parse", "MERGE_HEAD") != target:
        raise RuntimeError(f"{url}: agent changed the merge history; work retained at {path}")
    if (path / "AGENTS.md").read_bytes() != instructions:
        raise RuntimeError(f"{url}: agent changed AGENTS.md; work retained at {path}")
    for index, command in enumerate(item["validation"]):
        summary.set(identity, "validating", command)
        check(path, command, directory / f"{kind}-{identity}-merge-check-{index}.log")
    execute(path, "git", "add", "--all")
    execute(path, "git", "commit", "-m", f"Resolve integration conflicts for {identity}")
    resolved = execute(path, "git", "rev-parse", "HEAD")
    execute(path, "git", "push", "origin", f"HEAD:refs/heads/{pr['headRefName']}")
    summary.set(identity, "ci", url)
    wait_checks(repo, url)
    summary.set(identity, "merging", url)
    with merge_lock:
        merge(repo, url, resolved)


def worker(repo: Path, item: dict, kind: str, base: str, directory: Path,
           context: dict, merge_lock: threading.Lock, summary: Summary | None = None) -> None:
    summary = summary or Summary()
    identity = item["id"]
    path = directory / f"{kind}-{identity}"
    branch = f"relay/{kind}/{identity}"
    existing = json.loads(execute(repo, "gh", "pr", "list", "--head", branch,
                                  "--base", INTEGRATION, "--state", "all",
                                  "--json", "url,state,headRefOid,mergeCommit"))
    if existing:
        pr = existing[0]
        if pr["state"] == "MERGED":
            execute(repo, "git", "merge-base", "--is-ancestor", pr["mergeCommit"]["oid"], base)
            say(f"[{identity}] already integrated {pr['url']}")
            summary.set(identity, "merged", pr["url"])
            return
        if pr["state"] != "OPEN":
            raise RuntimeError(f"{identity}: existing PR was closed without merging: {pr['url']}")
        say(f"[{identity}] continuing existing PR {pr['url']}")
        integrate(repo, pr["url"], pr["headRefOid"], item, kind, directory, context, merge_lock, summary)
        summary.set(identity, "merged", pr["url"])
        say(f"[{identity}] merged {pr['url']}")
        return
    say(f"[{identity}] starting worktree={path}")
    worktree(repo, path, base, branch)
    instructions = (path / "AGENTS.md").read_bytes()
    response = directory / f"{kind}-{identity}.txt"
    summary.set(identity, "agent")
    agent(path, kind, {**context, "item": item}, response, write=True)
    if (path / "AGENTS.md").read_bytes() != instructions:
        raise RuntimeError(f"{identity}: agent changed AGENTS.md")
    if execute(path, "git", "rev-parse", "HEAD") != base:
        raise RuntimeError(f"{identity}: agent changed Git history; expected uncommitted implementation")
    for index, command in enumerate(item["validation"]):
        summary.set(identity, "validating", command)
        say(f"[{identity}] validating: {command}")
        check(path, command, directory / f"{kind}-{identity}-check-{index}.log")
    execute(path, "git", "add", "--all")
    changed = execute(path, "git", "diff", "--cached", "--name-only").splitlines()
    if {"requirements.md", "plan.md", "tasks.json", "bugs.json"}.intersection(changed):
        raise RuntimeError(f"{identity}: changed a planning artifact")
    if not [name for name in changed if name != "AGENTS.md"]:
        raise RuntimeError(f"{identity}: no implementation changes")
    execute(path, "git", "commit", "-m", f"{identity}: {item.get('title', 'bug fix')}")
    sha = execute(path, "git", "rev-parse", "HEAD")
    summary.set(identity, "publishing")
    execute(path, "git", "push", "origin", branch)
    url = pull_request(repo, branch, INTEGRATION, f"{identity}: {item.get('title', 'bug fix')}", response)
    integrate(repo, url, sha, item, kind, directory, context, merge_lock, summary)
    summary.set(identity, "merged", url)
    say(f"[{identity}] merged {url}")


def parallel(repo: Path, entries: list[dict], kind: str, directory: Path, context: dict, summary=None) -> bool:
    summary = summary or Summary()
    summary.begin("build" if kind == "task" else "bug fixes", [item["id"] for item in entries])
    if not entries:
        return True
    execute(repo, "git", "fetch", "origin", INTEGRATION)
    base = execute(repo, "git", "rev-parse", f"origin/{INTEGRATION}")
    lock = threading.Lock()
    say(f"[{kind.upper()}] items={len(entries)} running in parallel")
    passed = True
    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        futures = {executor.submit(worker, repo, item, kind, base, directory, context, lock, summary): item for item in entries}
        for future in as_completed(futures):
            identity = futures[future]["id"]
            try:
                future.result()
            except (OSError, ValueError, RuntimeError) as error:
                passed = False
                summary.set(identity, "blocked", str(error))
                say(f"[{identity}] blocked: {error}\nWork retained: {directory / (kind + '-' + identity)}")
    return passed


def pipeline(repo: Path, data: dict, directory: Path, summary=None) -> bool:
    summary = summary or Summary()
    tasks = items(data, "tasks")
    requirements = (repo / "requirements.md").read_text(encoding="utf-8")
    context = {"requirements": requirements, "plan": (repo / "plan.md").read_text(encoding="utf-8")}
    execute(repo, "gh", "auth", "status")
    execute(repo, "git", "fetch", "origin", "main")
    project_pr = None
    if execute(repo, "git", "ls-remote", "--heads", "origin", INTEGRATION):
        execute(repo, "git", "fetch", "origin", INTEGRATION)
        existing = json.loads(execute(repo, "gh", "pr", "list", "--head", INTEGRATION,
                                      "--base", "main", "--state", "all",
                                      "--json", "url,state,headRefOid,mergeCommit"))
        project_pr = existing[0] if existing else None
        if project_pr and project_pr["state"] == "MERGED":
            if project_pr["headRefOid"] != execute(repo, "git", "rev-parse", f"origin/{INTEGRATION}"):
                raise RuntimeError("integration changed after the project merged; start a new campaign instead of reusing old task/bug IDs")
            execute(repo, "git", "merge-base", "--is-ancestor", project_pr["mergeCommit"]["oid"], "origin/main")
            say(f"[RUN] project already merged: {project_pr['url']}; no agents or validation run; artifacts retained")
            summary.begin("already merged; not revalidated", ["project"])
            summary.set("project", "merged", project_pr["url"])
            return True
        if project_pr and project_pr["state"] != "OPEN":
            raise RuntimeError(f"project PR was closed without merging: {project_pr['url']}")
        say(f"[RUN] continuing {INTEGRATION}")
    else:
        execute(repo, "git", "push", "origin", f"refs/remotes/origin/main:refs/heads/{INTEGRATION}")
    if not parallel(repo, tasks, "task", directory, context, summary=summary):
        return False

    audit_tree = directory / "audit"
    bugs_file = repo / "bugs.json"
    if bugs_file.exists():
        bugs = items(json.loads(bugs_file.read_text(encoding="utf-8")), "bugs")
        say(f"[AUDIT] reusing bugs.json bugs={len(bugs)}; no new audit")
    else:
        execute(repo, "git", "fetch", "origin", INTEGRATION)
        base = execute(repo, "git", "rev-parse", f"origin/{INTEGRATION}")
        worktree(repo, audit_tree, base)
        say("[AUDIT] running")
        summary.begin("audit", ["audit"])
        summary.set("audit", "agent")
        raw = agent(audit_tree, "audit", {**context, "tasks": tasks}, directory / "audit.json")
        bugs = items(decode(raw), "bugs")
        bugs_file.write_text(json.dumps({"bugs": bugs}, indent=2) + "\n", encoding="utf-8")
        say(f"[AUDIT] complete bugs={len(bugs)}")
    if not parallel(repo, bugs, "bug", directory, context, summary=summary):
        return False

    execute(repo, "git", "fetch", "origin", INTEGRATION)
    final_sha = execute(repo, "git", "rev-parse", f"origin/{INTEGRATION}")
    final_tree = directory / "final"
    worktree(repo, final_tree, final_sha)
    validations = dict.fromkeys(command for item in tasks + bugs for command in item["validation"])
    summary.begin("final validation", ["project"])
    for index, command in enumerate(validations):
        say(f"[FINAL] validating: {command}")
        summary.set("project", "validating", command)
        check(final_tree, command, directory / f"final-check-{index}.log")
    if execute(final_tree, "git", "diff", "HEAD", "--name-only"):
        raise RuntimeError("final validation modified tracked files; validated code must match the project PR")
    say("[FINAL] validation passed")
    body = directory / "project-pr.md"
    body.write_text(context["plan"], encoding="utf-8")
    summary.begin("project PR", ["project"])
    summary.set("project", "publishing")
    url = project_pr["url"] if project_pr else pull_request(repo, INTEGRATION, "main", "Relay project integration", body)
    integrate(repo, url, final_sha, {"id": "project", "description": "Integrate the validated project into main",
              "validation": list(validations)}, "project", directory, context, threading.Lock(), summary)
    summary.set("project", "merged", url)
    say(f"[FINAL] merged {url}")
    paths = [directory / f"task-{item['id']}" for item in tasks]
    paths += [directory / f"bug-{item['id']}" for item in bugs] + [audit_tree, final_tree]
    paths += [directory / f"task-{item['id']}-merge" for item in tasks]
    paths += [directory / f"bug-{item['id']}-merge" for item in bugs] + [directory / "project-project-merge"]
    summary.phase = "cleanup"
    retained = 0
    for path in paths:
        if not path.exists():
            continue
        try:
            cleanup_worktree(repo, path)
        except (OSError, ValueError, RuntimeError) as error:
            retained += 1
            say(f"[CLEANUP] retained {path}: {error}")
    summary.retained = retained
    say(f"[RUN] project merged; cleanup={'partial' if retained else 'complete'}; logs={directory}")
    summary.phase = "cleanup partial" if retained else "complete"
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-spinner", action="store_true")
    args = parser.parse_args(argv)
    summary = Summary()
    try:
        repo = project(args.repo)
        data = json.loads((repo / "tasks.json").read_text(encoding="utf-8"))
        tasks = items(data, "tasks")
        if args.dry_run:
            say(f"[PLAN] tasks={len(tasks)} agents={len(tasks)}")
            return 0
        directory = Path(tempfile.mkdtemp(prefix="relay-run-"))
        say(f"[RUN] worktrees and logs: {directory}")
        with progress(summary, args.no_spinner):
            return 0 if pipeline(repo, data, directory, summary) else 2
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        with summary.lock:
            summary.error = " ".join(str(error).split())[:240]
            summary.states = {key: value if value[0] == "merged" else ("blocked", "")
                              for key, value in summary.states.items()}
        say(f"[RUN] blocked: {error}")
        return 2
    finally:
        if not args.dry_run:
            say(f"[SUMMARY] {summary()}")


if __name__ == "__main__":
    raise SystemExit(main())
