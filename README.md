# Relay

<p align="center">
  <img src="assets/relay-icon.png" alt="Relay" width="180">
</p>

Relay is a bounded, resumable coordinator that turns requirements into isolated GitHub or Azure DevOps pull requests. Its workflow is:

```text
vertical slice -> focused validation -> campaign validation -> one review
-> bounded blocker repair -> merge -> next slice -> one finite audit
```

The four public tools are directly executable Python 3.11+ scripts and use only the standard library. Relay shells out to `git`, `codex`, and either `gh` or `az`.

## Requirements

- Python 3.11 or newer and Git with an author configured.
- Codex available as `codex`.
- A target repository with `main` as its integration branch and PR base.
- A supported GitHub or Azure Repos `origin`.
- GitHub CLI authenticated with `gh auth login`, or Azure CLI with the `azure-devops` extension and an authenticated organization.

`RELAY_GIT`, `RELAY_GH`, `RELAY_AZ`, `RELAY_CODEX`, and `RELAY_PWSH` may select replacement executables. Relay does not read or store provider credentials.

## Create a repository

`repo.py` creates a local repository with `main`, a README, and generic target `AGENTS.md`. It refuses a nonempty destination, and a failed publication leaves the local repository intact.

```powershell
python repo.py --path C:\Code\Projects\Example --github OWNER/Example --private
python repo.py --path C:\Code\Projects\Example --azure-devops ORGANIZATION PROJECT Example
```

GitHub requires exactly one of `--private` or `--public`. Azure repository visibility comes from its project. The provider options are mutually exclusive.

Native tools are equally valid alternatives:

```powershell
gh repo create OWNER/Example --private --clone
az repos create --organization https://dev.azure.com/ORGANIZATION --project PROJECT --name Example
```

For an existing repository, skip repository creation.

## Plan and run

Create requirements in Markdown, then generate and inspect a bounded plan:

```powershell
python plan.py `
  --repo C:\Code\Projects\Example `
  --requirements C:\path\to\requirements.md `
  --workers 3 `
  --task-attempts 3 `
  --fix-loops 2

python run.py --repo C:\Code\Projects\Example --dry-run
```

`PLAN.md` records the selected base SHA, vertical-slice dependencies and paths, acceptance criteria, focused validation, mandatory campaign validation, task-attempt limit, and shared fix limit. The first run must use matching limits and an unchanged base.

```powershell
python run.py `
  --repo C:\Code\Projects\Example `
  --workers 3 `
  --task-attempts 3 `
  --fix-loops 2
```

Normal restarts use the same short command. Relay reloads persisted limits and provider identity, replays ledger intents, and continues a safe recorded phase. Read status without changing the campaign:

```powershell
python status.py --repo C:\Code\Projects\Example
```

`status.py` uses the same campaign summary as `run.py`: phase, completed work, blockers, attempt/fix budgets, and an exact `NEXT` command.

## Architecture

`plan.py` reads the requirements, tracked tree, base SHA, and target `AGENTS.md`. It may run fixed read-only scout scopes concurrently, then creates the fewest independently useful vertical slices. One read-only plan review may trigger one bounded repair and exact verification. Every structured agent result is validated before output is accepted.

`run.py` is the only writer of active campaign state and ledgers. Before any Worker starts, it validates the campaign commands at the planned base in a detached worktree. It optionally publishes a generated target `AGENTS.md` in its own PR. Ready slices then run concurrently when dependencies and path ownership allow it.

Each Worker receives one isolated branch and worktree. Relay persists the task attempt before launch, requires a clean committed descendant, validates ancestry and changed paths, runs focused validation followed by campaign validation, and records the exact command, result, and log. A failed candidate may return to the same Worker path only while both task and shared fix budgets remain. The fix is reserved and persisted before launch.

One read-only slice review runs after validation. Candidate-introduced P0/P1 blockers may be repaired; P2 or supported pre-existing findings enter the backlog; P3 findings are discarded; genuine decisions stop in `needs-user`. Review repairs move only forward through `repair-N` and `verify-N`. Validation, review, integration, merge-conflict, and provider-check repairs all consume `taskStates[id].fixAttemptsStarted`.

Only a reviewed SHA is published. Relay requires the provider-reported head to match that SHA, waits within bounded provider deadlines, and merges through GitHub or Azure DevOps policy. Completed worktrees and branches are removed after integration.

After planned slices merge, Relay runs exactly one finite audit. Audit scopes and calls are bounded. Accepted audit fixes validate and merge without starting another full review or audit. Deferred findings are written to human-readable `BACKLOG.md` with title, severity, requirement, failure, and evidence. A later `plan.py --requirements BACKLOG.md` treats it as ordinary requirements and plans its tests and commands normally.

Coordinator output is a synchronous timestamped event stream. There is no spinner, console thread, TTY rewriting, or periodic display process. Raw agent, provider, and validation logs remain under `.relay/logs/`.

## State contract

Schema 3 is the only executable campaign schema. Extra obsolete keys in schema-3 state are ignored. Earlier schemas are rejected before mutation.

To replace an unsupported campaign, extract its incomplete requirements and backlog items, archive the old campaign outside the active repository state, and generate a fresh schema-3 plan. Do not edit `state.json` directly. A schema-3 campaign stopped in a removed recovery phase is also rejected and must be replanned.

The authoritative runtime facts are:

- `tasks.md` and `bugs.md` for active ledgers;
- `.relay/state.json` for phases, counters, deadlines, provider identity, PRs, and worktrees;
- one `attemptCounters[id]` task-attempt counter;
- one `taskStates[id].fixAttemptsStarted` shared repair counter.

Only `run.py` changes those files. Reservations and counters are saved before external processes, so crashes never create free retries.

## Recovery

Recovery has three explicit intents and always previews before mutation:

```powershell
# Resume the exact safe phase after repairing an external condition
python run.py --repo C:\Code\Projects\Example --recover
python run.py --repo C:\Code\Projects\Example --recover --confirm

# Grant one persisted additional Worker assignment
python run.py --repo C:\Code\Projects\Example --recover --grant-attempt TASK-0001
python run.py --repo C:\Code\Projects\Example --recover --grant-attempt TASK-0001 --confirm

# Move all selected blockers for an assignment to BACKLOG.md
python run.py --repo C:\Code\Projects\Example --recover --defer-blocker BUG-0001
python run.py --repo C:\Code\Projects\Example --recover --defer-blocker BUG-0001 --confirm
```

Preview is read-only. Confirmation journals the action, applies it, and exits without launching a Worker; follow the printed `NEXT` command.

`resume` revalidates ledger ownership, the resolved worktree path, cleanliness, candidate ancestry and SHA, changed paths, recorded phase, and provider state. Interrupted validation reruns its recorded command without refunding its counter. Provider recovery queries the live PR and source SHA; confirmed resume may grant one provider retry after credentials or authorization are repaired. `defer` restores the initial reviewed candidate and moves every selected blocker for that assignment to the backlog.

Unexpected dirt, SHA drift, scope drift, provider-head drift, active processes, or an unknown phase stops in `needs-user`. Relay preserves evidence and does not guess. `waiting-provider` means checks or approvals are still pending.

## Providers

| | GitHub | Azure DevOps Services |
| --- | --- | --- |
| Origin | Canonical HTTPS or SSH | Canonical Azure HTTPS or SSH, including `visualstudio.com` |
| PR text | Multiline body file and idempotent `pr edit` | Multiline Markdown description and idempotent `repos pr update` |
| Merge | `squash`, `merge`, or `rebase` | `squash` or no-fast-forward `merge`; `rebase` fails preflight |
| Gate | Required checks and approvals | Blocking branch policies; queued/running waits, rejected/broken fails |

Relay never bypasses provider policy. Push, PR creation, metadata refresh, checks, merge, and reconciliation have hard deadlines and persisted retry counters.

## Cleanup and next campaign

After completion, preview and confirm cleanup:

```powershell
python run.py --repo C:\Code\Projects\Example --cleanup
python run.py --repo C:\Code\Projects\Example --cleanup --confirm
```

Cleanup is permanent and only allowed for a complete inactive campaign with no worktrees or open Relay PRs. It validates resolved paths and merge proof before removing `tasks.md`, `bugs.md`, Relay-format plan files, and `.relay`. It preserves `BACKLOG.md`, `AGENTS.md`, source, and Git history.

Start the next campaign directly from the backlog:

```powershell
python plan.py --repo C:\Code\Projects\Example --requirements C:\Code\Projects\Example\BACKLOG.md
python run.py --repo C:\Code\Projects\Example
```

## Files and development

| File | Responsibility |
| --- | --- |
| `repo.py` | Create and optionally publish a repository. |
| `plan.py` | Inspect, plan, review, and validate before writing `PLAN.md`. |
| `run.py` | Coordinate the campaign and exclusively write active state and ledgers. |
| `status.py` | Print the shared read-only campaign summary. |
| `relay_console.py` | Emit synchronous timestamped coordinator events. |
| `test_workflow.py` | Deterministic workflow, recovery, provider, concurrency, and cleanup checks. |

Run the complete local gate:

```powershell
python -m unittest -v test_workflow.py
python -m py_compile run.py plan.py status.py repo.py relay_console.py
python repo.py --help
python plan.py --help
python run.py --help
python status.py --help
```

Exit codes are `0` for completion, `1` for operational or validation failure, `2` when user or provider action is required, and `130` when interrupted.
