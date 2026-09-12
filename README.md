# Relay

Relay is a bounded, resumable workflow for turning requirements into isolated GitHub pull requests:

```text
repo.py -> plan.py -> run.py -> status.py
```

It uses Python 3.11's standard library, top-level `codex exec` processes, Git worktrees, and the GitHub `gh` CLI. Worker is the only write-capable role; `task`, `bug`, and `repair` are its modes. Review, repair, provider, validation, and audit loops all have persisted hard limits.

Every target gets a root `AGENTS.md` when one is missing. New repositories commit Relay's generic target rules immediately; existing repositories publish the generated file through a provider-check-only bootstrap PR before Workers start. Existing files are never overwritten, and every agent prompt includes the target's instructions.

```powershell
python repo.py --path C:\Code\Projects\Example
python plan.py --repo C:\Code\Projects\Example --requirements requirements.md --workers 3 --fix-loops 2
python run.py --repo C:\Code\Projects\Example --workers 3 --fix-loops 2
python status.py --repo C:\Code\Projects\Example
```

Use `python run.py --repo <path> --cleanup` to preview permanent campaign cleanup, then repeat with `--confirm`. Run the deterministic gate with `python -m unittest -v test_workflow.py`.

`plan.py` writes `PLAN.md` and then creates a missing `AGENTS.md`; `run.py` reads the plan without a pipe and creates `tasks.md`, `bugs.md`, and `.relay/state.json`. Attempt and fix-loop options must match between planning and execution. `PLAN.md` and `AGENTS.md` remain after confirmed permanent cleanup.
