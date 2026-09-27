#!/usr/bin/env python3
"""Run Relay's finite parallel build, audit, bug, and validation pipeline."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from plan import validate_tasks

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
AGENT_TIMEOUT = 3600
CHECK_TIMEOUT = 1800
INTEGRATION_BRANCH = "relay/integration"


def command(name: str) -> list[str]:
    value = os.environ.get(f"RELAY_{name.upper()}", name)
    parts = shlex.split(value, posix=os.name != "nt")
    if os.name == "nt" and parts:
        parts[0] = shutil.which(parts[0]) or parts[0]
    return parts


def run_command(program: str, *args: str, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command(program) + list(args),
        cwd=cwd,
        check=True,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=CHECK_TIMEOUT,
    )


def git(repo: Path, *args: str, capture: bool = False) -> str:
    return run_command("git", "-C", str(repo), *args, capture=capture).stdout.strip() if capture else ""


def gh(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    result = run_command("gh", *args, cwd=cwd, capture=capture)
    return result.stdout.strip() if capture else ""


def emit(message: str) -> None:
    print(message, flush=True)


class Status:
    def __init__(self, no_spinner: bool, verbose: bool) -> None:
        self.no_spinner = no_spinner
        self.verbose = verbose
        self.phase = "starting"
        self.active = 0
        self.complete = 0
        self.merged = 0
        self.blocked = 0
        self.started = time.monotonic()
        self.interactive = not no_spinner and sys.stdout.isatty()
        self._spinner: asyncio.Task[None] | None = None
        self._stop = False

    def snapshot(self) -> str:
        elapsed = int(time.monotonic() - self.started)
        return f"phase={self.phase} active={self.active} complete={self.complete} merged={self.merged} blocked={self.blocked} elapsed={elapsed}s"

    def event(self, message: str) -> None:
        if self.interactive and self._spinner:
            print("\r\033[2K", end="", flush=True)
        emit(message)

    async def start(self) -> None:
        if self.no_spinner or not sys.stdout.isatty():
            self._spinner = asyncio.create_task(self._heartbeat())
        else:
            self._spinner = asyncio.create_task(self._spin())

    async def stop(self) -> None:
        self._stop = True
        if self._spinner:
            self._spinner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._spinner
            if self.interactive:
                print("\r\033[2K", end="", flush=True)
            self._spinner = None

    async def _spin(self) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        index = 0
        while not self._stop:
            print(f"\r{frames[index % len(frames)]} {self.phase.upper()} {self.snapshot()}", end="", flush=True)
            index += 1
            await asyncio.sleep(0.35)

    async def _heartbeat(self) -> None:
        while not self._stop:
            await asyncio.sleep(15)
            if not self._stop:
                emit(f"[RUN] {self.snapshot()}")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_bugs(data: object) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("bugs"), list):
        raise ValueError("bugs.json must contain a bugs array")
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, bug in enumerate(data["bugs"], 1):
        if not isinstance(bug, dict):
            raise ValueError(f"bugs[{index}] must be an object")
        for field in ("id", "description", "location", "evidence", "expected", "validation"):
            if field not in bug or not isinstance(bug[field], str) or not bug[field].strip():
                raise ValueError(f"bugs[{index}] missing valid {field}")
        if bug["id"] in ids:
            raise ValueError(f"duplicate bug id: {bug['id']}")
        ids.add(bug["id"])
        result.append(bug)
    return result


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
        raise


def add_worktree(repo: Path, branch: str, base: str, path: Path) -> None:
    try:
        run_command("git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch}")
        run_command("git", "-C", str(repo), "worktree", "add", str(path), branch)
    except subprocess.CalledProcessError:
        run_command("git", "-C", str(repo), "worktree", "add", "-b", branch, str(path), base)


def remove_worktree(repo: Path, path: Path) -> None:
    try:
        run_command("git", "-C", str(repo), "worktree", "remove", "--force", str(path))
    except (OSError, subprocess.CalledProcessError):
        pass


def branch_exists(repo: Path, branch: str) -> bool:
    try:
        run_command("git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch}")
        return True
    except subprocess.CalledProcessError:
        return False


def ensure_integration(repo: Path) -> None:
    base = "main"
    if not branch_exists(repo, INTEGRATION_BRANCH):
        run_command("git", "-C", str(repo), "branch", INTEGRATION_BRANCH, base)
    else:
        try:
            run_command("git", "-C", str(repo), "fetch", "origin", INTEGRATION_BRANCH)
            fast_forward_branch(repo, INTEGRATION_BRANCH)
        except subprocess.CalledProcessError:
            pass
    run_command("git", "-C", str(repo), "push", "--set-upstream", "origin", INTEGRATION_BRANCH)


def refresh_integration(repo: Path) -> None:
    run_command("git", "-C", str(repo), "fetch", "origin", INTEGRATION_BRANCH)
    fast_forward_branch(repo, INTEGRATION_BRANCH)


def fast_forward_branch(repo: Path, branch: str) -> None:
    local = git(repo, "rev-parse", branch, capture=True)
    remote = git(repo, "rev-parse", f"origin/{branch}", capture=True)
    run_command("git", "-C", str(repo), "merge-base", "--is-ancestor", local, remote)
    run_command("git", "-C", str(repo), "update-ref", f"refs/heads/{branch}", remote, local)


def parse_pr_number(text: str) -> str:
    match = re.search(r"/pull/(\d+)", text)
    if not match:
        raise ValueError("gh pr create returned no pull request URL")
    return match.group(1)


def task_branch(prefix: str, item_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", item_id).strip("-")
    return f"relay/{prefix}/{safe}"


def prompt(name: str, context: str) -> str:
    path = PROMPTS / name
    return path.read_text(encoding="utf-8") + "\n\n## Runtime context\n" + context


async def run_codex(worktree: Path, text: str, extra_dirs: list[Path] | None = None) -> tuple[int, str]:
    args = command("codex") + [
        "exec",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(worktree),
    ]
    for directory in extra_dirs or []:
        args.extend(["--add-dir", str(directory)])
    args.append("-")
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(text.encode()), AGENT_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "agent timed out"
    return process.returncode or 0, output.decode(errors="replace")


def worktree_clean(path: Path) -> bool:
    return not run_command("git", "-C", str(path), "status", "--porcelain", capture=True).stdout.strip()


def changed_files(path: Path, base: str) -> list[str]:
    result = run_command("git", "-C", str(path), "diff", "--name-only", f"{base}..HEAD", capture=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


async def create_pr_and_merge(
    repo: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
    merge_lock: asyncio.Lock,
) -> str:
    output = await asyncio.to_thread(
        gh,
        "pr",
        "create",
        "--base",
        base,
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
        cwd=repo,
        capture=True,
    )
    number = parse_pr_number(output)
    await asyncio.to_thread(gh, "pr", "checks", number, "--required", "--watch", cwd=repo)
    async with merge_lock:
        await asyncio.to_thread(gh, "pr", "merge", number, "--squash", "--delete-branch", cwd=repo)
        await asyncio.to_thread(refresh_integration, repo)
    return number


async def run_item(
    repo: Path,
    item: dict[str, Any],
    kind: str,
    base: str,
    status: Status,
    merge_lock: asyncio.Lock,
) -> bool:
    item_id = item["id"]
    branch = task_branch("task" if kind == "task" else "bug", item_id)
    path = Path(tempfile.mkdtemp(prefix=f"relay-{kind}-{item_id}-"))
    status.active += 1
    status.event(f"[{item_id}] started")
    try:
        await asyncio.to_thread(add_worktree, repo, branch, base, path)
        agents = (repo / "AGENTS.md").read_text(encoding="utf-8") if (repo / "AGENTS.md").is_file() else "(no AGENTS.md present)"
        context = f"""
REPOSITORY={repo}
WORKTREE={path}
BRANCH={branch}
AGENTS_MD:
{agents}
ITEM_JSON:
{json.dumps(item, indent=2)}
"""
        template = "task.md" if kind == "task" else "bug.md"
        code, output = await run_codex(path, prompt(template, context))
        if status.verbose and output:
            for line in output.splitlines()[-80:]:
                status.event(f"[{item_id}] {line}")
        if code:
            raise RuntimeError(f"agent exited {code}")
        if not await asyncio.to_thread(worktree_clean, path):
            raise RuntimeError("worktree is dirty after agent exit")
        files = await asyncio.to_thread(changed_files, path, base)
        if not files:
            raise RuntimeError("agent produced no committed changes")
        sha = await asyncio.to_thread(git, path, "rev-parse", "HEAD", capture=True)
        status.event(f"[{item_id}] committed {sha[:12]}")
        await asyncio.to_thread(run_command, "git", "-C", str(path), "push", "--set-upstream", "origin", branch)
        title = f"{item_id}: {item.get('title', item.get('description', 'change'))}"
        body = f"Automated Relay {kind}.\n\n{json.dumps(item, indent=2)}"
        number = await create_pr_and_merge(repo, branch, INTEGRATION_BRANCH, title, body, merge_lock)
        status.event(f"[{item_id}] PR #{number} merged")
        status.complete += 1
        status.merged += 1
        return True
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        status.blocked += 1
        status.event(f"[{item_id}] blocked reason={str(error).splitlines()[0]}")
        return False
    finally:
        status.active -= 1
        await asyncio.to_thread(remove_worktree, repo, path)


async def run_items(repo: Path, items: list[dict[str, Any]], kind: str, status: Status) -> bool:
    if not items:
        return True
    status.phase = "build" if kind == "task" else "bugs"
    status.event(f"[{status.phase.upper()}] starting {kind}s={len(items)} agents={len(items)}")
    merge_lock = asyncio.Lock()
    results = await asyncio.gather(*(run_item(repo, item, kind, INTEGRATION_BRANCH, status, merge_lock) for item in items))
    return all(results)


async def audit(repo: Path, status: Status) -> list[dict[str, Any]] | None:
    status.phase = "audit"
    status.event("[AUDIT] starting")
    path = Path(tempfile.mkdtemp(prefix="relay-audit-"))
    output = Path(tempfile.mkdtemp(prefix="relay-audit-output-"))
    try:
        await asyncio.to_thread(add_worktree, repo, "relay/audit", INTEGRATION_BRANCH, path)
        agents = (repo / "AGENTS.md").read_text(encoding="utf-8") if (repo / "AGENTS.md").is_file() else "(no AGENTS.md present)"
        context = f"""
REPOSITORY={repo}
WORKTREE={path}
OUTPUT_DIR={output}
AGENTS_MD:
{agents}
requirements.md:
{(repo / 'requirements.md').read_text(encoding='utf-8')}
plan.md:
{(repo / 'plan.md').read_text(encoding='utf-8')}
tasks.json:
{(repo / 'tasks.json').read_text(encoding='utf-8')}
"""
        code, text = await run_codex(path, prompt("audit.md", context), [output])
        if status.verbose and text:
            for line in text.splitlines()[-80:]:
                status.event(f"[AUDIT] {line}")
        if code:
            raise RuntimeError(f"audit agent exited {code}")
        candidate = output / "bugs.json"
        if not candidate.is_file():
            raise RuntimeError("audit agent did not create bugs.json")
        bugs = validate_bugs(load_json(candidate))
        atomic_write(repo / "bugs.json", json.dumps({"bugs": bugs}, indent=2) + "\n")
        status.event(f"[AUDIT] complete bugs={len(bugs)}")
        return bugs
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        status.blocked += 1
        status.event(f"[AUDIT] blocked reason={str(error).splitlines()[0]}")
        return None
    finally:
        await asyncio.to_thread(remove_worktree, repo, path)
        shutil.rmtree(output, ignore_errors=True)


async def final_validation(repo: Path, status: Status, tasks: list[dict[str, Any]], bugs: list[dict[str, Any]]) -> bool:
    status.phase = "final"
    status.event("[FINAL] validation started")
    path = Path(tempfile.mkdtemp(prefix="relay-final-"))
    commands: list[str] = []
    for item in [*tasks, *bugs]:
        for check in item.get("validation", []):
            if check not in commands:
                commands.append(check)
    try:
        await asyncio.to_thread(add_worktree, repo, "relay/final", INTEGRATION_BRANCH, path)
        for check in commands:
            process = await asyncio.create_subprocess_shell(
                check,
                cwd=path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await asyncio.wait_for(process.communicate(), CHECK_TIMEOUT)
            if process.returncode:
                text = output.decode(errors="replace")[-4000:]
                raise RuntimeError(f"{check} exited {process.returncode}: {text}")
        status.event("[FINAL] validation passed")
        return True
    except (OSError, RuntimeError, subprocess.TimeoutExpired, asyncio.TimeoutError) as error:
        status.blocked += 1
        status.event(f"[FINAL] blocked reason={str(error).splitlines()[0]}")
        return False
    finally:
        await asyncio.to_thread(remove_worktree, repo, path)


async def project_pr(repo: Path, status: Status) -> bool:
    status.phase = "project-pr"
    status.event("[FINAL] project PR creating")
    try:
        output = await asyncio.to_thread(
            gh,
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            INTEGRATION_BRANCH,
            "--title",
            "Relay project integration",
            "--body",
            "Automated Relay project PR after final validation.",
            cwd=repo,
            capture=True,
        )
        number = parse_pr_number(output)
        await asyncio.to_thread(gh, "pr", "checks", number, "--required", "--watch", cwd=repo)
        await asyncio.to_thread(gh, "pr", "merge", number, "--squash", "--delete-branch", cwd=repo)
        status.event(f"[FINAL] project PR #{number} merged")
        return True
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        status.blocked += 1
        status.event(f"[FINAL] project PR blocked reason={str(error).splitlines()[0]}")
        return False


def cleanup(repo: Path) -> None:
    temp_root = Path(tempfile.gettempdir()).resolve()
    result = run_command("git", "-C", str(repo), "worktree", "list", "--porcelain", capture=True).stdout
    current: Path | None = None
    for line in result.splitlines() + [""]:
        if line.startswith("worktree "):
            current = Path(line[9:]).resolve()
        elif not line and current and current != repo.resolve() and current.parent == temp_root and current.name.startswith("relay-"):
            remove_worktree(repo, current)
            current = None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", required=True, type=Path)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--no-spinner", action="store_true")
    result.add_argument("--verbose", action="store_true")
    result.add_argument("--cleanup", action="store_true")
    return result


async def main_async(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        emit(f"[RUN] blocked reason=not a Git repository: {repo}")
        return 2
    if args.cleanup:
        cleanup(repo)
        emit("[CLEANUP] complete")
        return 0
    for required in ("plan.md", "tasks.json"):
        if not (repo / required).is_file():
            emit(f"[RUN] blocked reason=missing {required}")
            return 2
    try:
        tasks_data = load_json(repo / "tasks.json")
        validate_tasks(tasks_data)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        emit(f"[RUN] blocked reason=invalid tasks.json: {str(error).splitlines()[0]}")
        return 2
    tasks = tasks_data["tasks"]
    status = Status(args.no_spinner, args.verbose)
    await status.start()
    try:
        status.phase = "prepare"
        if args.dry_run:
            status.event(f"[DRY-RUN] tasks={len(tasks)} agents={len(tasks)}")
            return 0
        await asyncio.to_thread(ensure_integration, repo)
        if not await run_items(repo, tasks, "task", status):
            return 2
        bugs = await audit(repo, status)
        if bugs is None:
            return 2
        if not await run_items(repo, bugs, "bug", status):
            return 2
        if not await final_validation(repo, status, tasks, bugs):
            return 2
        if not await project_pr(repo, status):
            return 2
        status.phase = "cleanup"
        cleanup(repo)
        status.event("[CLEANUP] complete")
        status.event("[RUN] complete")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        status.blocked += 1
        status.event(f"[RUN] blocked reason={str(error).splitlines()[0]}")
        return 2
    finally:
        await status.stop()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
