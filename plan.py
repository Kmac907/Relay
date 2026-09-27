#!/usr/bin/env python3
"""Plan once: requirements.md -> plan.md + tasks.json."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

PROMPTS = Path(__file__).resolve().parent / "prompts"
PRINT_LOCK = threading.Lock()


def say(message: str) -> None:
    with PRINT_LOCK:
        print("\r\033[2K" if sys.stdout.isatty() else "", end="")
        print(message, flush=True)


@contextmanager
def progress(label: str, disabled: bool = False):
    stop = threading.Event()
    started = time.monotonic()
    def animate():
        for frame in itertools.cycle("|/-\\"):
            if stop.wait(0.2):
                return
            text = label() if callable(label) else f"{label} elapsed={int(time.monotonic() - started)}s"
            text = text[:max(0, shutil.get_terminal_size().columns - 3)]
            with PRINT_LOCK:
                print(f"\r{frame} {text}", end="", flush=True)
    thread = None
    if sys.stdout.isatty() and not disabled:
        thread = threading.Thread(target=animate, daemon=True)
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread:
            thread.join()
            with PRINT_LOCK:
                print("\r\033[2K", end="", flush=True)


def execute(cwd: Path, *args: str, input: str | None = None, log: Path | None = None) -> str:
    executable = shutil.which(os.environ.get(f"RELAY_{args[0].upper()}", args[0]))
    if not executable:
        raise RuntimeError(f"executable not found: {args[0]}")
    result = subprocess.run([executable, *args[1:]], cwd=cwd, input=input,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if log:
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{subprocess.list2cmdline(list(args))} exited {result.returncode}\n"
                           f"{(result.stdout + result.stderr)[-4000:]}" + (f"\nLog: {log}" if log else ""))
    return result.stdout.strip()


def agent(repo: Path, role: str, context: dict, output: Path, *, write: bool = False) -> str:
    instructions = (repo / "AGENTS.md").read_text(encoding="utf-8")
    prompt = (PROMPTS / f"{role}.md").read_text(encoding="utf-8")
    prompt += "\nProject AGENTS.md:\n" + instructions + "\nInputs:\n" + json.dumps(context, indent=2)
    execute(repo, "codex", "exec", "--ephemeral", "--sandbox",
            "workspace-write" if write else "read-only", "--cd", str(repo),
            "--output-last-message", str(output), "-", input=prompt, log=output.with_suffix(".log"))
    if not output.is_file():
        raise ValueError(f"agent returned no final response; see {output.with_suffix('.log')}")
    return output.read_text(encoding="utf-8")


def decode(text: str):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def commands(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result = [item.get("command") if isinstance(item, dict) else item for item in values]
    if not result or any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError(f"validation must contain command strings; received {value!r}")
    return result


def items(data, key: str) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError(f"expected an object with a {key} array")
    result = data[key]
    if key == "tasks" and not result:
        raise ValueError("tasks must not be empty")
    seen = set()
    for item in result:
        if not isinstance(item, dict):
            raise ValueError(f"invalid {key} entry: {item!r}")
        identity = item.get("id", "")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity) or identity.casefold() in seen:
            raise ValueError(f"invalid or duplicate id: {identity!r}")
        seen.add(identity.casefold())
        for field in ("description", "evidence") if key == "bugs" else ("title", "description"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"{identity}: missing {field}")
        if item.get("dependencies"):
            raise ValueError(f"{identity}: tasks must be independently executable; combine dependent work during planning")
        item["validation"] = commands(item.get("validation"))
    return result


def project(path: Path) -> Path:
    repo = path.expanduser().resolve()
    if Path(execute(repo, "git", "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("--repo must name the repository root")
    if not (repo / "AGENTS.md").is_file():
        from repo import TARGET_AGENTS
        (repo / "AGENTS.md").write_text(TARGET_AGENTS, encoding="utf-8")
    return repo


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, help="import this file as the project's requirements.md")
    parser.add_argument("--no-spinner", action="store_true")
    args = parser.parse_args(argv)
    try:
        repo = project(args.repo)
        requirements_path = repo / "requirements.md"
        if args.requirements and args.requirements.resolve() != requirements_path:
            shutil.copyfile(args.requirements, requirements_path)
        requirements = requirements_path.read_text(encoding="utf-8")
        output = Path(tempfile.mkdtemp(prefix="relay-plan-"))
        say(f"[PLAN] running; output={output}")
        with progress("planning", args.no_spinner):
            raw = agent(repo, "planning", {"requirements": requirements}, output / "response.json")
        result = decode(raw)
        tasks = items(result, "tasks")
        if not isinstance(result.get("plan"), str) or not result["plan"].strip():
            raise ValueError("planner response is missing plan text")
        (repo / "plan.md").write_text(result["plan"] + "\n", encoding="utf-8")
        (repo / "tasks.json").write_text(json.dumps({"tasks": tasks}, indent=2) + "\n", encoding="utf-8")
        say(f"[PLAN] complete tasks={len(tasks)}")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        say(f"[PLAN] blocked: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
