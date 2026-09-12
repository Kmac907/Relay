# Relay

Relay is a bounded, resumable workflow for turning requirements into isolated GitHub pull requests:

```text
repo.py -> plan.py -> run.py -> status.py
```

It uses Python 3.11's standard library, top-level `codex exec` processes, Git worktrees, and the GitHub `gh` CLI. Worker is the only write-capable role; `task`, `bug`, and `repair` are its modes. Review, repair, provider, validation, and audit loops all have persisted hard limits.

```powershell
python repo.py --path C:\Code\Projects\Example
python plan.py --repo C:\Code\Projects\Example --requirements requirements.md --workers 3 |
  python run.py --repo C:\Code\Projects\Example --workers 3 --fix-loops 2
python status.py --repo C:\Code\Projects\Example
```

Use `python run.py --repo <path> --cleanup` to preview permanent campaign cleanup, then repeat with `--confirm`. Run the deterministic gate with `python -m unittest -v test_workflow.py`.

Relay writes `tasks.md`, `bugs.md`, and `.relay/state.json` in the target repository and excludes them locally through `.git/info/exclude`. Campaign evidence remains until confirmed cleanup.
