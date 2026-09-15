# Relay

Relay is a bounded, resumable coordinator that turns requirements into isolated GitHub or Azure DevOps Services pull requests. It plans work, dispatches constrained coding agents, validates their commits, reviews candidates, merges approved PRs, and runs one finite post-build audit.

```text
requirements
    |
    v
plan.py -> PLAN.md + missing AGENTS.md
    |
    v
run.py -> AGENTS.md bootstrap PR (when needed)
    |
    v
Workers -> validation -> review/repair -> provider PRs -> merge
    |
    v
finite audit -> accepted bug fixes -> complete

status.py observes the active campaign without changing it.
```

Relay is four directly executable Python scripts. It uses only Python 3.11's standard library and shells out to `git`, `codex`, and either `gh` or `az`.

## Prerequisites

- Python 3.11 or newer.
- Git with an author name and email configured.
- For GitHub, the GitHub CLI authenticated with `gh auth login`.
- For Azure DevOps Services, Azure CLI 2.30+ with the `azure-devops` extension and authentication configured through its supported Microsoft Entra or PAT flow. Azure DevOps Server is not supported.
- Codex available as `codex`.
- A target repository whose integration branch and PR base are `main`.
- A supported GitHub or Azure Repos `origin` before running a campaign. `repo.py` can create it.

Set `RELAY_GIT`, `RELAY_GH`, `RELAY_AZ`, or `RELAY_CODEX` only when Relay should invoke those tools through different commands. Relay never reads or stores provider credentials.

For Azure, install the official extension with `az extension add --name azure-devops`, then authenticate with `az login` or pipe a PAT to `az devops login --organization https://dev.azure.com/ORGANIZATION`. The Azure CLI owns that credential flow; do not pass credentials to Relay.

## Quick start

Create and publish a new repository:

```powershell
python repo.py `
  --path C:\Code\Projects\Example `
  --github OWNER/Example `
  --private
```

Or create it in an existing Azure DevOps project (repository visibility is inherited from the project):

```powershell
python repo.py `
  --path C:\Code\Projects\Example `
  --azure-devops ORGANIZATION PROJECT Example
```

Write the requirements, then plan and run the campaign:

```powershell
python plan.py `
  --repo C:\Code\Projects\Example `
  --requirements C:\path\to\requirements.md `
  --workers 3 `
  --task-attempts 3 `
  --fix-loops 2

python run.py `
  --repo C:\Code\Projects\Example `
  --workers 3 `
  --task-attempts 3 `
  --fix-loops 2
```

`--task-attempts` and `--fix-loops` must match between planning and the first campaign run. Use the same `run.py --repo ...` command with no stdin or new `--plan` to resume an interrupted or provider-waiting campaign; Relay reloads the persisted limits.

For an existing repository, skip `repo.py` and run `plan.py` against its selected `HEAD`.

## Workflow

### 1. Repository creation

`repo.py` refuses to overwrite a nonempty path. It initializes `main`, creates `README.md` and Relay's generic target `AGENTS.md`, and commits both. `--github OWNER/NAME` requires exactly one of `--private` or `--public`. `--azure-devops ORGANIZATION PROJECT REPOSITORY` uses `az repos create`, adds its returned HTTPS clone URL as `origin`, and pushes the initial commit; the project must already exist. Publication failures preserve the local repository. The two provider options are mutually exclusive, and visibility flags are GitHub-only.

### 2. Planning

`plan.py` reads the requirements, tracked tree, selected base SHA, and target instructions. For a nontrivial repository it runs fixed, read-only scout scopes concurrently, then a read-only Planning PM produces a bounded task graph. Relay validates the structured result before it writes anything.

Planner attempts report `START`, `WAIT`, `DONE`, `RETRY`, and `FAILED` lifecycle events on stderr. Agent stdout and stderr remain captured separately.

On success, planning exclusively creates `PLAN.md`, then exclusively creates `AGENTS.md` if it is still missing. Existing files or other filesystem entries are preserved. When `AGENTS.md` was initially missing, scouts and the Planning PM receive the same generic rules in memory.

`PLAN.md` records each task's dependencies, allowed paths, acceptance criteria, validation commands, attempt limit, and shared fix-loop limit. Relay refuses to start a new campaign if the planned base no longer matches `HEAD`.

### 3. Target instructions bootstrap

Relay never copies this repository's development `AGENTS.md` into a target. The generic target template lives in `repo.py` and tells agents to honor their assigned role and paths, leave coordinator files alone, avoid provider operations and subagents, validate their work, and report evidence.

- Existing tracked or untracked custom `AGENTS.md` files are honored and never automatically committed.
- New repositories already contain the generated file in their initial commit.
- For an existing repository where Relay generated the file after the planned base, `run.py` publishes an isolated PR containing only the exact `AGENTS.md` before launching Workers.
- The bootstrap uses normal provider deadlines, retries, checks, required approvals, SHA-drift protection, and the configured merge method, but no Codex reviewers.
- Its commit, push, PR, checks, merge, and local reconciliation are persisted for safe resume.
- If marked generated content changes unexpectedly, Relay preserves it and stops in `needs-user`.

### 4. Build, review, and merge

`run.py` copies the validated plan into the active `tasks.md` ledger, creates `bugs.md` and `.relay/state.json`, and excludes coordinator-owned runtime files from Git.

Ready tasks run concurrently only when dependencies are satisfied and allowed paths do not overlap. Each task gets an isolated branch and Git worktree. The Worker is the only write-capable role and must commit a candidate locally. Before publication, Relay verifies ancestry, the reported SHA, changed paths, and every assigned validation command.

Relay then pushes the branch, opens or recovers one PR, and runs two independent read-only reviews followed by one triage decision. Accepted blockers enter the same bounded Worker repair loop and receive a focused verification review. Initial blockers, merge conflicts, integration failures, and provider-check repairs share one persisted fix-loop budget.

Approved candidates must retain the reviewed SHA and pass the selected provider's required checks and approvals before Relay merges them. For Azure, blocking branch-policy evaluations are authoritative: approved and not-applicable pass, queued and running wait within the persisted deadline, and rejected or broken fail. Conflicts enter the shared repair budget, and Relay never bypasses policies. Azure supports Relay's `squash` and standard no-fast-forward `merge` completion modes; `rebase` is rejected during preflight. Completed worktrees and local branches are removed.

`run.py` detects canonical GitHub and Azure HTTPS/SSH origins, including legacy `visualstudio.com` Azure URLs, then persists the provider identity. Azure PR descriptions are passed as a single line for Windows `az.cmd` compatibility; GitHub continues to receive the body file. Resume uses the persisted provider identity and normalized PR number, URL, source SHA, and state; older campaigns containing `githubRepository` continue as GitHub campaigns. Use the same `run.py --repo ...` command after provider action or interruption. Push, PR creation, policy polling, merge, reconciliation, and the no-AI-review `AGENTS.md` bootstrap all resume without intentionally duplicating completed operations. A legacy Azure bootstrap stopped specifically after exhausting `AGENTS:pr-create` is recovered once with separate bounded v2 list/create counters, reusing its existing commit, branch, push, and worktree.

### 5. Finite audit

After all planned tasks integrate, Relay updates local `main` and plans one finite audit campaign. Read-only Audit Workers inspect explicit scopes concurrently, and triage classifies their evidence once. Accepted P0/P1 findings become bounded bug-mode Worker assignments; P2 findings may enter the backlog. Fixes do not trigger recursive audit planning.

The campaign becomes `complete` when no active audit bugs remain. A blocker needing judgment becomes `needs-user`; incomplete provider checks become `waiting-provider`.

## Campaign files

| Path | Owner | Lifetime |
| --- | --- | --- |
| `AGENTS.md` | Target | Preserved permanently; generated only when missing |
| `PLAN.md` | User | Planning handoff preserved permanently |
| `tasks.md` | Coordinator | Active task ledger; removed only by confirmed cleanup |
| `bugs.md` | Coordinator | Audit/review evidence ledger; removed only by confirmed cleanup |
| `.relay/state.json` | Coordinator | Resume authority, counters, phases, PRs, worktrees, and deadlines |
| `.relay/logs/` | Coordinator | Raw agent and provider logs, separate from console output |

Only `run.py` modifies active ledgers and runtime state. Counters are persisted before processes or provider operations start, so a crash never grants a free retry.

## Observe, validate, and clean up

Show campaign state, including active roles, review budgets, provider deadlines, PRs, worktrees, bugs, and any `AGENTS.md` bootstrap:

```powershell
python status.py --repo C:\Code\Projects\Example
```

Validate a plan without creating campaign state:

```powershell
python run.py --repo C:\Code\Projects\Example --dry-run
```

Preview permanent campaign cleanup, then confirm it:

```powershell
python run.py --repo C:\Code\Projects\Example --cleanup
python run.py --repo C:\Code\Projects\Example --cleanup --confirm
```

Cleanup is allowed only for a complete, inactive campaign with no worktrees or open Relay PRs. It removes `tasks.md`, `bugs.md`, and `.relay`; it preserves `PLAN.md`, `AGENTS.md`, source, and Git history.

Run Relay's deterministic test gate with:

```powershell
python -m unittest -v test_workflow.py
```

Exit codes are `0` for completion, `1` for an operational or validation failure, `2` when user or provider action is required, and `130` when interrupted. A nonzero campaign result emits `STOPPED` on stderr with the persisted phase and coordinator-owned reason; the targeted Azure recovery emits `RECOVER`. Raw provider output remains in `.relay/logs/provider.log`.

Run any script with `--help` for all limits, deadlines, merge methods, and path options. See `PLAN.md` for the full behavioral specification.
