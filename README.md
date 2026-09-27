# Relay

<p align="center">
  <img src="assets/relay-icon.png" alt="Relay icon" width="180">
</p>

One plan, parallel implementation, one audit, parallel bug fixes, final validation.

```text
requirements.md
      |
      v
planning -------> plan.md + tasks.json
      |
      v
build ----------> TASK worktrees -> task PRs -> relay/integration
      |
      v
audit ----------> bugs.json -> BUG worktrees -> bug PRs
      |
      v
final validation -> project PR -> main -> cleanup
```

Requires Python 3.11+, Git, Codex CLI, and authenticated GitHub CLI (`gh`).
Windows validation commands use PowerShell 7 (`pwsh`); elsewhere they use `sh`.
GitHub must permit merge commits. Branch protection and required checks are
respected; required human approval still blocks that PR.

## Usage

Create a project using the existing repository tool, or use an existing GitHub
repository with `origin/main`:

```powershell
python repo.py --path C:\Code\Projects\Example --github OWNER/Example --private
python plan.py --repo C:\Code\Projects\Example --requirements C:\path\requirements.md
python run.py --repo C:\Code\Projects\Example --dry-run
python run.py --repo C:\Code\Projects\Example
```

`plan.py` calls one read-only planner and saves `plan.md` and `tasks.json`.
The project's `requirements.md` is the authoritative source for both scripts.
`tasks.json` contains only tasks. By default, planning reads the project's
`requirements.md`; `--requirements PATH` imports that file to the project as
`requirements.md`, replacing its previous contents. Common JSON command shapes
are normalized locally. Invalid output stops with the original response retained.

`run.py` starts one agent per independent task simultaneously. The planner
defines shared interfaces and combines dependent or overlapping work. Each
agent has a fresh context and its own branch and worktree. Workers implement;
Relay runs validation and handles commits, pushes, PRs, and merges through `gh`.
CI waits run concurrently; merges are serialized. All workers finish even if
another worker fails. Failed work stops progression to the next phase.

The integrated code is audited once. Each reported bug gets one worker and
worktree. After bug PRs merge, final validation runs the unique task and bug
commands against the integrated code. Passing validation leads to a project PR
and merge into `main`. Relay does not update your original working directory.

## Instructions and prompts

Every project has an authoritative `AGENTS.md`. An existing file is preserved;
Relay creates a minimal file if missing and supplies it to each agent.
Project conventions and commands belong there. Agent role prompts live in:

```text
prompts/planning.md
prompts/task.md
prompts/audit.md
prompts/bug.md
prompts/merge.md
```

Planner output contains `plan` text and a `tasks` array. Each task contains
`id`, `title`, `description` (including acceptance criteria), and `validation`
command strings. Audit output contains a `bugs` array; bugs contain `id`,
`description`, `evidence`, and `validation`. The prompts include examples.

## Status and failures

Console events identify each phase, worker, validation command, and PR.
The live summary shows the phase, active agents, validation, conflict resolution,
waiting work, merged/total items, blocked items, and total elapsed time. Active
item IDs and their current operations follow the counts. A final summary remains
visible after exit. These are in-memory display values, not scheduling state.
Interactive terminals show a spinner; `--no-spinner` disables it.
The printed temporary directory contains full agent output, validation logs,
and worktrees. Failures print the command, output excerpt, and log location.

When GitHub reports a merge conflict, an agent receives Git's conflicting files
and the task context in an isolated worktree. It chooses and stages the resolution;
Relay supplies no file-specific resolution rules. Relay validates, commits,
pushes without force, and attempts to merge the resolved result. The handoff also
applies to existing PRs and the final project PR. Other workers continue while
the agent resolves the conflict. An unresolved conflict, failed validation, or
another failed merge stops with work retained; it does not call the agent again.

The orchestration is acyclic: implementation, optional conflict resolution,
and integration. Audit runs once per campaign; subsequent invocations reuse
the existing `bugs.json` without regenerating findings or their IDs. Failed validation never
schedules another agent. There are no
repair counters, budgets, PM agents, review passes, workflow database, or resume
engine. This prevents Relay from spawning an endless review/repair cycle. It
does not guarantee an underlying agent terminates or that every bug is solvable.
Tests provide repeatable acceptance checks; they do not prove arbitrary code
correct. Choose meaningful regression and integration checks in the plan.

On failure, worktrees and branches are retained for inspection. Running again
continues the existing `relay/integration` branch. Open task and bug PRs are
reused, and PRs already merged into integration are skipped without starting
another agent. Existing PRs pass through GitHub checks and merging; the combined
result still passes final validation using the saved audit's checks. Open project
PRs are reused too. A project PR already merged at the current integration commit
ends the run before starting any agents; the summary says `ALREADY MERGED; NOT
REVALIDATED`. This does not claim that later findings are fixed. Changed integration
after a completed project requires a new campaign. Closed, unmerged PRs require
user action. Unpublished work remains available for manual recovery.
On success, worktrees are removed after the project PR merges. Cleanup removes
untracked Python bytecode from `__pycache__` and restores only content-equivalent
`AGENTS.md` checkout formatting. Actual edits and other untracked files are
preserved; the final summary reports `cleanup=partial` and the retained count.
Logs, artifacts, and branches remain available. These artifacts and branch names
belong to the current planned project; rerunning resumes it rather than starting
a new project or requesting another audit.
Do not edit or remove `bugs.json` while resuming a campaign: bug PR IDs refer to
that saved audit. Starting another campaign in the same repository is not
automated; deleting branches alone does not remove their old GitHub PR identities.

`RELAY_GIT`, `RELAY_GH`, and `RELAY_CODEX` can name replacement executable paths.
Agents use Codex's read-only or workspace-write sandbox as appropriate.

## Verification

```powershell
python -B -m unittest -v test_workflow.py
```

The tests exercise real Git repositories, concurrent worktrees, real shell
validation, task and bug integration, and every terminal failure phase.
Agent responses and GitHub are simulated; the tests do not spend model tokens
or create remote PRs.
