#!/usr/bin/env python3
"""Create a local repository and optionally publish it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import relay_console

GENERATED_AGENTS_MARKER = "<!-- relay: generated-target-instructions v1 -->"
TARGET_AGENTS = f"""{GENERATED_AGENTS_MARKER}
# Relay target instructions

- Follow the assigned role, mode, paths, and acceptance criteria.
- Treat `PLAN.md` as user-owned and `tasks.md`, `bugs.md`, and `.relay` as coordinator-owned.
- Only Workers may modify source, and only within their assigned worktree and allowed paths.
- Do not create, push, or merge pull requests; change provider settings; spawn subagents; or edit ledgers.
- Run the assigned validation, make focused commits, and report evidence for every result.
"""
TARGET_AGENTS_SHA256 = hashlib.sha256(TARGET_AGENTS.encode()).hexdigest()


def create_exclusive(path: Path, content: str) -> bool:
    """Create a UTF-8/LF file, or preserve any existing filesystem entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stream = path.open("x", encoding="utf-8", newline="\n")
    except OSError:
        if os.path.lexists(path):
            return False
        raise
    with stream:
        stream.write(content)
    return True


def _command(tool: str) -> list[str]:
    parts = shlex.split(os.environ.get(f"RELAY_{tool.upper()}", tool), posix=os.name != "nt")
    if os.name == "nt" and parts:
        parts[0] = shutil.which(parts[0]) or parts[0]
    return parts


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def run(tool: str, *args: str, capture: bool = False, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(_command(tool) + list(args), check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def create(path: Path, github: str | None = None, visibility: str | None = None, timeout: int = 300, azure_devops: tuple[str, str, str] | None = None) -> tuple[Path, str, str]:
    if github and azure_devops:
        raise ValueError("choose either GitHub or Azure DevOps")
    if visibility and not github:
        raise ValueError("--private and --public are GitHub-only")
    if github and visibility not in {"private", "public"}:
        raise ValueError("GitHub visibility must be explicit")
    if azure_devops and (len(azure_devops) != 3 or not re.fullmatch(r"[^/\s]+", azure_devops[0]) or any(not value.strip() for value in azure_devops[1:])):
        raise ValueError("invalid Azure DevOps organization, project, or repository")
    target = path.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"refusing nonempty path: {target}")
    target.mkdir(parents=True, exist_ok=True)
    if (target / ".git").exists():
        raise ValueError(f"refusing existing Git repository: {target}")

    started = time.monotonic()
    relay_console.emit("START", operation="initialize", deadline=f"{timeout}s")
    run("git", "-C", str(target), "init", "--initial-branch=main", timeout=timeout)
    create_exclusive(target / "README.md", f"# {target.name}\n")
    create_exclusive(target / "AGENTS.md", TARGET_AGENTS)
    run("git", "-C", str(target), "add", "README.md", "AGENTS.md", timeout=timeout)
    relay_console.emit("DONE", operation="initialize", elapsed=f"{time.monotonic() - started:.1f}s")
    started = time.monotonic()
    relay_console.emit("START", operation="commit", deadline=f"{timeout}s")
    run("git", "-C", str(target), "commit", "-m", "Initial commit", timeout=timeout)
    branch = run("git", "-C", str(target), "branch", "--show-current", capture=True, timeout=timeout).stdout.strip()
    sha = run("git", "-C", str(target), "rev-parse", "HEAD", capture=True, timeout=timeout).stdout.strip()
    relay_console.emit("DONE", operation="commit", sha=sha[:12], elapsed=f"{time.monotonic() - started:.1f}s")

    if github:
        started = time.monotonic()
        relay_console.emit("START", operation="publish", deadline=f"{timeout}s")
        run("gh", "auth", "status", timeout=timeout)
        run("gh", "repo", "create", github, f"--{visibility}", "--source", str(target), "--remote", "origin", "--push", timeout=timeout)
        relay_console.emit("DONE", operation="publish", elapsed=f"{time.monotonic() - started:.1f}s")
    elif azure_devops:
        started = time.monotonic()
        relay_console.emit("START", operation="publish", deadline=f"{timeout}s")
        organization, project, repository = azure_devops
        created = run("az", "repos", "create", "--name", repository, "--organization", f"https://dev.azure.com/{organization}", "--project", project, "--output", "json", capture=True, timeout=timeout)
        data = json.loads(created.stdout)
        if not isinstance(data, dict) or not isinstance(data.get("remoteUrl"), str) or not data["remoteUrl"]:
            raise ValueError("az repos create returned no remoteUrl")
        run("git", "-C", str(target), "remote", "add", "origin", data["remoteUrl"], timeout=timeout)
        run("git", "-C", str(target), "push", "--set-upstream", "origin", "main", timeout=timeout)
        relay_console.emit("DONE", operation="publish", elapsed=f"{time.monotonic() - started:.1f}s")
    return target, branch, sha


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Create a new local repository and optionally publish it to GitHub or Azure DevOps Services.")
    result.add_argument("--path", required=True, type=Path)
    def github_name(value: str) -> str:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
            raise argparse.ArgumentTypeError("must be OWNER/NAME")
        return value
    provider = result.add_mutually_exclusive_group()
    provider.add_argument("--github", metavar="OWNER/NAME", type=github_name)
    provider.add_argument("--azure-devops", nargs=3, metavar=("ORGANIZATION", "PROJECT", "REPOSITORY"))
    visibility = result.add_mutually_exclusive_group()
    visibility.add_argument("--private", action="store_const", const="private", dest="visibility")
    visibility.add_argument("--public", action="store_const", const="public", dest="visibility")
    result.add_argument("--provider-timeout", type=positive, default=300)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.github and not args.visibility:
        parser().error("--github requires exactly one of --private or --public")
    if args.visibility and not args.github:
        parser().error("--private and --public are GitHub-only")
    if args.azure_devops and (not re.fullmatch(r"[^/\s]+", args.azure_devops[0]) or any(not value.strip() for value in args.azure_devops[1:])):
        parser().error("invalid Azure DevOps organization, project, or repository")
    try:
        path, branch, sha = create(args.path, args.github, args.visibility, args.provider_timeout, tuple(args.azure_devops) if args.azure_devops else None)
    except (ValueError, OSError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        reason = f"command exited with code {error.returncode}" if isinstance(error, subprocess.CalledProcessError) else "operation timed out" if isinstance(error, subprocess.TimeoutExpired) else str(error).splitlines()[0]
        relay_console.emit("FAILED", operation="repository", reason=reason)
        return 1
    print(f"Path:   {path}")
    print(f"Branch: {branch}")
    print(f"SHA:    {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
