#!/usr/bin/env python3
"""Create a new local repository and optionally a private or public GitHub repo."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
from pathlib import Path


def _command(tool: str) -> list[str]:
    return shlex.split(os.environ.get(f"RELAY_{tool.upper()}", tool), posix=os.name != "nt")


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def run(tool: str, *args: str, capture: bool = False, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(_command(tool) + list(args), check=True, capture_output=capture, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def create(path: Path, github: str | None = None, visibility: str | None = None, timeout: int = 300) -> tuple[Path, str, str]:
    target = path.expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"refusing nonempty path: {target}")
    target.mkdir(parents=True, exist_ok=True)
    if (target / ".git").exists():
        raise ValueError(f"refusing existing Git repository: {target}")

    run("git", "-C", str(target), "init", "--initial-branch=main", timeout=timeout)
    (target / "README.md").write_text(f"# {target.name}\n", encoding="utf-8")
    run("git", "-C", str(target), "add", "README.md", timeout=timeout)
    run("git", "-C", str(target), "commit", "-m", "Initial commit", timeout=timeout)
    branch = run("git", "-C", str(target), "branch", "--show-current", capture=True, timeout=timeout).stdout.strip()
    sha = run("git", "-C", str(target), "rev-parse", "HEAD", capture=True, timeout=timeout).stdout.strip()

    if github:
        if visibility not in {"private", "public"}:
            raise ValueError("GitHub visibility must be explicit")
        run("gh", "auth", "status", timeout=timeout)
        run("gh", "repo", "create", github, f"--{visibility}", "--source", str(target), "--remote", "origin", "--push", timeout=timeout)
    return target, branch, sha


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Create a new local repository and optionally publish it to GitHub.")
    result.add_argument("--path", required=True, type=Path)
    def github_name(value: str) -> str:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
            raise argparse.ArgumentTypeError("must be OWNER/NAME")
        return value
    result.add_argument("--github", metavar="OWNER/NAME", type=github_name)
    visibility = result.add_mutually_exclusive_group()
    visibility.add_argument("--private", action="store_const", const="private", dest="visibility")
    visibility.add_argument("--public", action="store_const", const="public", dest="visibility")
    result.add_argument("--provider-timeout", type=positive, default=300)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if bool(args.github) != bool(args.visibility):
        parser().error("--github requires exactly one of --private or --public")
    try:
        path, branch, sha = create(args.path, args.github, args.visibility, args.provider_timeout)
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error)) from error
    print(f"Path:   {path}")
    print(f"Branch: {branch}")
    print(f"SHA:    {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
