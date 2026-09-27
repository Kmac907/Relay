#!/usr/bin/env python3
"""Create plan.md and tasks.json from requirements.md."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPT = ROOT / "prompts" / "planning.md"
AGENT_TIMEOUT = 3600


def command(name: str) -> list[str]:
    value = os.environ.get(f"RELAY_{name.upper()}", name)
    parts = shlex.split(value, posix=os.name != "nt")
    if os.name == "nt" and parts:
        parts[0] = shutil.which(parts[0]) or parts[0]
    return parts


def emit(message: str) -> None:
    print(message, flush=True)


class Spinner:
    def __init__(self, label: str, disabled: bool) -> None:
        self.label = label
        self.disabled = disabled or not sys.stdout.isatty()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.disabled:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join()
            print("\r\033[2K", end="", flush=True)

    def _run(self) -> None:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        index = 0
        started = time.monotonic()
        while not self.stop_event.is_set():
            elapsed = int(time.monotonic() - started)
            print(f"\r{frames[index % len(frames)]} PLAN {self.label} elapsed={elapsed}s", end="", flush=True)
            index += 1
            self.stop_event.wait(0.35)


def validate_tasks(data: object) -> int:
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list) or not data["tasks"]:
        raise ValueError("tasks.json must contain a non-empty tasks array")
    ids: set[str] = set()
    for index, task in enumerate(data["tasks"], 1):
        if not isinstance(task, dict):
            raise ValueError(f"tasks[{index}] must be an object")
        for field in ("id", "title", "description", "acceptanceCriteria", "validation"):
            if field not in task:
                raise ValueError(f"tasks[{index}] missing {field}")
        task_id = task["id"]
        if not isinstance(task_id, str) or not task_id.strip() or task_id in ids:
            raise ValueError(f"tasks[{index}] has a duplicate or invalid id")
        if not isinstance(task["acceptanceCriteria"], list) or not task["acceptanceCriteria"]:
            raise ValueError(f"tasks[{index}].acceptanceCriteria must be non-empty")
        validation = task["validation"]
        if isinstance(validation, str):
            validation = [validation]
        elif isinstance(validation, dict) and isinstance(validation.get("command"), str):
            validation = [validation["command"]]
        elif isinstance(validation, list):
            normalized: list[str] = []
            for item in validation:
                if isinstance(item, str):
                    normalized.append(item)
                elif isinstance(item, dict) and isinstance(item.get("command"), str):
                    normalized.append(item["command"])
                else:
                    raise ValueError(f"tasks[{index}].validation contains unsupported value: {item!r}")
            validation = normalized
        else:
            raise ValueError(f"tasks[{index}].validation must contain command strings, got: {validation!r}")
        if not validation or not all(item.strip() for item in validation):
            raise ValueError(f"tasks[{index}].validation must contain at least one command string")
        task["validation"] = validation
        ids.add(task_id)
    return len(data["tasks"])


def atomic_write(path: Path, content: str) -> None:
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", required=True, type=Path)
    result.add_argument("--requirements", required=True, type=Path)
    result.add_argument("--no-spinner", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    requirements = args.requirements.expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        parser().error(f"not a Git repository: {repo}")
    if not requirements.is_file():
        parser().error(f"requirements file not found: {requirements}")
    if not PROMPT.is_file():
        parser().error(f"prompt not found: {PROMPT}")

    started = time.monotonic()
    emit("[PLAN] starting")
    before_status = subprocess.run(
        command("git") + ["-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    agents = (repo / "AGENTS.md").read_text(encoding="utf-8") if (repo / "AGENTS.md").is_file() else "(no AGENTS.md present)"
    requirement_text = requirements.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="relay-plan-") as output:
        output_dir = Path(output)
        prompt = PROMPT.read_text(encoding="utf-8") + f"""

## Runtime context
OUTPUT_DIR={output_dir}
REPOSITORY={repo}

<AGENTS_MD>
{agents}
</AGENTS_MD>

<REQUIREMENTS_MD>
{requirement_text}
</REQUIREMENTS_MD>
"""
        emit("[PLAN] planning agent running")
        spinner = Spinner("planning agent running", args.no_spinner)
        spinner.start()
        try:
            result = subprocess.run(
                command("codex") + ["exec", "--ephemeral", "--dangerously-bypass-approvals-and-sandbox", "--cd", str(repo), "-"],
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=AGENT_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            spinner.stop()
            emit(f"[PLAN] blocked reason={error}")
            return 2
        spinner.stop()
        after_status = subprocess.run(
            command("git") + ["-C", str(repo), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        if after_status != before_status:
            emit("[PLAN] blocked reason=planning agent modified the target repository")
            return 2
        if result.returncode:
            emit(f"[PLAN] blocked agent_exit={result.returncode}")
            if result.stderr:
                print(result.stderr[-4000:], file=sys.stderr)
            return 2
        plan_path = output_dir / "plan.md"
        tasks_path = output_dir / "tasks.json"
        if not plan_path.is_file() or not tasks_path.is_file():
            emit("[PLAN] blocked reason=agent did not create plan.md and tasks.json")
            return 2
        try:
            tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
            count = validate_tasks(tasks)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            emit(f"[PLAN] blocked reason=invalid tasks.json: {error}")
            return 2
        emit("[PLAN] validating tasks.json")
        atomic_write(repo / "plan.md", plan_path.read_text(encoding="utf-8"))
        atomic_write(repo / "tasks.json", json.dumps(tasks, indent=2) + "\n")
    emit(f"[PLAN] complete tasks={count} elapsed={time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
