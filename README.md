# Relay

Relay is a simple parallel Ralph pipeline. It plans once, builds tasks in
parallel, integrates task PRs, audits the integrated result once, fixes the
reported bugs in parallel, validates once, and opens one project PR.

```text
requirements.md
      |
      v
planning -------> plan.md + tasks.json
      |
      v
build ----------> task worktrees -> task PRs -> relay/integration
      |
      v
audit ----------> bugs.json -> bug worktrees -> bug PRs
      |
      v
final validation -> project PR -> main -> cleanup
```

There is no PM loop, dependency scheduler, workflow database, retry counter,
repair budget, review epoch, fingerprint ledger, or recursive audit. The phase
pipeline is finite. A failed task, bug fix, or final validation is blocked with
evidence instead of being retried forever.

## Requirements

- Python 3.11 or newer.
- Git with an author configured.
- Codex available as `codex`.
- GitHub CLI authenticated with `gh auth login`.
- A target repository with `main` as its final branch.

Relay shells out to `git`, `codex`, and `gh`. `RELAY_GIT`, `RELAY_GH`, and
`RELAY_CODEX` may select replacement executables. Relay does not read or store
provider credentials.

## Prompts

Prompt templates are versioned in Relay and resolved relative to the scripts:

```text
prompts/planning.md
prompts/task.md
prompts/audit.md
prompts/bug.md
```

They are never copied into target repositories. Runtime context is appended in
memory for each agent.

## Project instructions

Each target project should have one authoritative `AGENTS.md`.

- `repo.py` creates a minimal one for newly created repositories.
- Existing `AGENTS.md` files are preserved and never overwritten.
- `plan.py` and `run.py` read it and provide it to every agent.
- Project architecture, commands, conventions, and testing rules belong there.
- Relay orchestration rules belong in Relay prompts, not the target file.

## Create a repository

`repo.py` creates a local repository with `main`, a README, and generic target
`AGENTS.md`. It refuses a nonempty destination.

```powershell
python repo.py --path C:\Code\Projects\Example --github OWNER/Example --private
python repo.py --path C:\Code\Projects\Example --azure-devops ORGANIZATION PROJECT Example
```

For an existing repository, skip this step.

## Plan

```powershell
python plan.py `
  --repo C:\Code\Projects\Example `
  --requirements C:\path\to\requirements.md
```

The planner creates:

- `plan.md`: human-readable plan;
- `tasks.json`: independently implementable task list.

The planner performs one agent pass and validates the JSON. It does not run a
plan-review or plan-repair loop.

## Run

```powershell
python run.py --repo C:\Code\Projects\Example
python run.py --repo C:\Code\Projects\Example --dry-run
python run.py --repo C:\Code\Projects\Example --verbose
python run.py --repo C:\Code\Projects\Example --no-spinner
```

The number of agents is derived from the current work:

- one build agent per task;
- one bug agent per bug.

There is no `--workers` option or internal worker pool. Independent work is
launched concurrently. GitHub, the provider, the OS, CI, and available disk
capacity provide the practical limit.

Each agent receives an isolated Git worktree and a fresh context. Task and bug
branches are deterministic:

```text
relay/task/TASK-001
relay/bug/BUG-001
relay/integration
```

Task and bug PRs target `relay/integration`. The final project PR targets
`main`.

## Status output

Relay prints event lines as work progresses:

```text
[BUILD] starting tasks=8 agents=8
[TASK-001] started
[TASK-001] committed abc1234
[TASK-001] PR #41 merged
[AUDIT] complete bugs=3
[BUG-001] PR #52 merged
[FINAL] validation passed
[FINAL] project PR #60 merged
```

Interactive terminals also show a lightweight spinner with phase, active
agents, completed work, merged PRs, blocked work, and elapsed time. It is
disabled automatically when output is redirected or explicitly with
`--no-spinner`. `--verbose` includes prefixed agent output.

## Infinite-loop prevention

The pipeline is acyclic:

```text
planning -> build -> integration -> audit -> bug fixes -> final validation -> main
```

The audit runs once. Bug fixes never start another audit. Final validation
never searches for new bugs or creates repair work. Failed work is blocked with
the exact command and evidence. Relay uses no attempt counters, retry limits,
repair budgets, or recursive review loops.

## Cleanup

Normal completion removes temporary worktrees after the project PR merges.

```powershell
python run.py --repo C:\Code\Projects\Example --cleanup
```

Cleanup removes only validated temporary Relay worktrees. It preserves source,
Git history, `AGENTS.md`, `requirements.md`, `plan.md`, `tasks.json`, and
`bugs.json`.

## Development

```powershell
python -m unittest -v test_workflow.py
python -m py_compile plan.py run.py repo.py
python repo.py --help
python plan.py --help
python run.py --help
```
