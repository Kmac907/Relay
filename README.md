# Relay

<p align="center">
  <img src="assets/relay-icon.png" alt="Relay" width="180">
</p>

Relay is a crash-safe, parallel Ralph-style scheduler. It gives each agent one small assignment in a fresh context, validates the resulting commit independently, reviews it, and serializes integration through the provider.

```text
reviewed finite plan
        |
        v
ready queue ----> fresh Worker ----> focused + campaign validation
   ^                                      |
   |                                      v
   +---- repair/bug <---- incremental review <---- initial review
   |                                                   |
   |                                               approved
   |                                                   v
   +---- dependencies unlocked <---- serialized integration + provider checks
                                                        |
                                                        v
                                              one audit -> final validation
```

Independent assignments, reviews, and repairs run concurrently. Integration is serialized, and only the exact SHA that passed validation, review, and provider checks may merge.

Relay is a set of directly executable Python 3.11+ scripts using only the standard library. It shells out to `git`, `codex`, and either `gh` or `az`.

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

For an existing repository, skip this step.

## Plan and run

```powershell
python plan.py `
  --repo C:\Code\Projects\Example `
  --requirements C:\path\to\requirements.md `
  --workers 3

python run.py --repo C:\Code\Projects\Example --dry-run
python run.py --repo C:\Code\Projects\Example
```

The planner creates the smallest independently verifiable assignments that fit one fresh context. It prefers vertical behavior; an enabling assignment is valid only with focused validation and a named consumer in the same finite plan. The plan receives one initial review and, when needed, one scoped repair and verification before execution.

`PLAN.md` is a reviewed immutable contract containing the requirement source or snapshot, objective, task graph, paths, non-goals, acceptance criteria, validation, and prompt-template digest. `tasks.md` is its coordinator-owned mutable execution projection.

Read status without changing state:

```powershell
python status.py --repo C:\Code\Projects\Example
python status.py --repo C:\Code\Projects\Example --markdown
```

## Agents and prompts

Relay has seven agent roles:

| Role | Access | Responsibility |
| --- | --- | --- |
| Scout | Read-only | Optional fixed-scope repository discovery during planning. |
| Planner | Read-only | Produce the finite task graph and repair a rejected plan. |
| Plan Reviewer | Read-only | Review the plan and verify its one repair. |
| Worker | Worktree write | Implement task, repair, bug, or integration-repair mode. |
| Reviewer | Read-only | Perform one initial review or an incremental repair review. |
| Audit Planner | Read-only | Define one finite final audit scope set. |
| Auditor | Read-only | Inspect one audit scope and return evidence-backed findings. |

Scheduling, tests, Git/worktree operations, provider polling, integration, output validation, state changes, and cycle detection are coordinator functions, not agents.

Stable role policy lives in `prompts/*.md`. Python appends a canonical JSON handoff packet built from applicable `AGENTS.md` files, the assignment contract, Git SHAs, dependency results, bugs, validation evidence, prior results, review epochs, and relevant merged learnings. The fully rendered execution prompt and its template/context/prompt hashes are recorded under `.relay/logs/prompts/` and `.relay/state.json`. Safety boundaries are also enforced by Python; prompt text is not a security boundary.

Only relevant context is selected. Workers receive path- or dependency-related learnings and history; reviewers and auditors remain independent. Raw logs are referenced when needed rather than appended wholesale.

## Information model

| Artifact | Authority |
| --- | --- |
| `AGENTS.md` | Tracked project rules, commands, architecture, and conventions. |
| `PLAN.md` | Immutable reviewed campaign contract and requirement snapshot. |
| `tasks.md` | Mutable task execution ledger derived from the plan. |
| `bugs.md` | Active and resolved defect ledger. |
| `BACKLOG.md` | Deferred supported findings retained after cleanup. |
| Git and provider PRs | Implementation history and merged SHAs. |
| `.relay/state.json` | Runtime phases, work items, leases, evidence, review epochs, fingerprints, prompt records, dependency summaries, and scoped learnings. |
| `.relay/logs/` | Raw agent, validation, provider, and rendered-prompt evidence. |

There is no authoritative `implementation.md` or `learnings.jsonl`. `status.py --markdown` is a derived human view. Only `run.py` writes active ledgers and runtime state.

Relay accepts a tracked project-owned `AGENTS.md` as authoritative. It creates the generic target file only when one is absent. Project instructions must not contain Relay orchestration commands such as selecting work, managing PRs, or editing ledgers.

## Autonomous execution

The unified queue contains planned tasks, validation repairs, review repairs, bugs, and integration repairs. Each work item is leased to an isolated worktree and fresh Worker. Provider polling remains a coordinator operation and does not occupy an agent slot.

```text
ready -> leased -> running -> candidate -> accepted -> integrated
                           |
                           +-> repair or bug work item
```

Every candidate runs focused validation followed by campaign validation. A task receives one initial review. Later epochs verify only unresolved findings, claimed resolutions, and the repair delta; they may add only blockers introduced by that delta. Review never returns to the initial phase or widens the assignment.

Relay maximizes autonomous throughput. There are no campaign budgets, call allowances, retry limits, fix-loop limits, or attempt counters. Work continues while deterministic project status advances. Relay stops only for unsafe repository or provider drift, missing credentials or access, or a genuine human decision.

Relay persists fingerprints containing the candidate tree, validation failure evidence, open finding IDs, integration base, repair scope, and provider state. An unchanged candidate, repeated fingerprint, A-B-A cycle, or irrelevant code change with unchanged failures/findings is a genuine decision point and becomes `needs-user`. Each external agent, validation, provider command, and provider-check window has a hard deadline. Expiration starts a fresh status-driven operation; it never consumes or grants an allowance. Concurrency remains limited by `--workers`.

The interactive console continuously redraws one spinner/status heartbeat. Redirected output emits a plain `WAIT` line only when status changes or after the periodic interval. These lines are presentation only; Relay never creates persisted WAIT work items.

Workers may propose concise path-scoped learnings. Relay activates them only after the evidence candidate merges, marks them stale when supporting paths change, and injects only relevant active learnings into later Worker contexts.

## Findings, bugs, and integration

Reviewers and auditors report evidence only: severity, location, observable failure, reproduction text, an exact requirement citation, evidence, candidate provenance, and affected evidence paths. They never select actions, IDs, status, or repair scope, and Relay never executes their reproduction text. `affectedPaths` must be repository-relative, must contain the location file, and never grants write access.

`run.py` derives stable IDs and dispositions. Unsupported provenance and P3 findings are discarded; P2 findings enter `BACKLOG.md`; candidate-introduced P0/P1 findings become review repairs or `needs-user` when they exceed the existing maximum scope. Pre-existing and audit P0/P1 findings become bug work only when their normalized requirement exactly matches supplied requirement or acceptance text; otherwise they enter the backlog. Coordinator disposition and reason are persisted with every finding and bug. Backlog and discarded findings do not block a candidate; `needs-user` takes priority over repair, and incremental approval requires every prior blocker to be explicitly resolved with no new blocker.

Bugs carry stable IDs, provenance, requirement, reproduction, evidence, paths, validation, and prior fingerprints. Their fixes use the same validation and incremental-review pipeline as tasks.

Approved candidates enter one integration lock. Relay refreshes `main`, reconciles the candidate, validates any changed SHA, incrementally reviews an integration-resolution diff, pushes the exact reviewed SHA, verifies the provider-reported head and checks, merges, records proof, and then unlocks dependents.

After planned work integrates, Relay runs one finite audit. Audit bugs use the normal queue; fixes never start another audit. Final campaign validation runs after the audit queue settles. A concrete final-validation failure becomes a bug, not a recursive audit.

Completion requires all planned tasks satisfied or integrated, all blocking findings and included audit bugs resolved, deferred findings recorded, no active candidate or PR, final campaign validation passing, and all worktrees safely removed.

## Recovery

Normal restarts reload schema-4 state, reconcile persisted operations and live provider state, and continue the safe recorded phase. Recovery is for a genuine decision or explicit blocker deferral and previews every mutation.

Malformed JSON and schema or validator failures start a fresh read-only correction context with the original context and schema frozen. Relay supplies exact JSON paths, stable error codes, and the rejected response as delimited untrusted data. Rejected responses are secret-redacted, included up to 64 KiB with a full hash and truncation flag, and saved under `.relay/logs/rejected/`. Corrections do not rerun implementation or validation, create repair work, or advance review epochs.

Agent-process and provider timeouts are recorded, reconciled against current status, and relaunched when safe. Transient provider failures continue locally; credential/access failures and unsafe drift become `needs-user`. Human reviewer policy becomes `waiting-provider` without occupying a worker.

```powershell
python run.py --repo C:\Code\Projects\Example --recover
python run.py --repo C:\Code\Projects\Example --recover --confirm

python run.py --repo C:\Code\Projects\Example --recover --defer-blocker BUG-0001
python run.py --repo C:\Code\Projects\Example --recover --defer-blocker BUG-0001 --confirm
```

Operation identity and status are persisted before external processes. Unexpected dirt, SHA drift, path drift, provider-head drift, missing authority, and unknown phases stop without guessing; interrupted safe operations resume from recorded evidence.

## Cleanup

```powershell
python run.py --repo C:\Code\Projects\Example --cleanup
python run.py --repo C:\Code\Projects\Example --cleanup --confirm
```

Cleanup is allowed only for a complete inactive campaign. It validates resolved paths and merge proof, then removes `tasks.md`, `bugs.md`, Relay plan files, and `.relay`. It preserves `BACKLOG.md`, `AGENTS.md`, source, and Git history.

## Development

```powershell
python -m unittest -v test_workflow.py
python -m py_compile run.py plan.py status.py repo.py relay_console.py
python repo.py --help
python plan.py --help
python run.py --help
python status.py --help
```

Exit codes are `0` for completion, `1` for invalid startup state or an unsafe coordinator failure, `2` when user or provider action is required, and `130` when interrupted.
