  # Relay — Complete Implementation Plan

  Relay is a standalone repository containing four directly executable Python scripts:

  repo.py → plan.py → run.py → status.py

  Relay creates repositories, converts project requirements into tasks.md, always creates bugs.md, executes independent work concurrently through top-level Codex
  processes, creates GitHub pull requests, performs bounded reviews and repairs, runs a bounded audit, and recovers safely after interruption.

  Relay is separate from Brace. Relay is the temporary workflow used to build Brace; Brace remains the eventual full CLI product.

  ## Primary requirements

  Relay must:

  - Live in its own Git repository.
  - Use Python 3.11’s standard library.
  - Reuse the unfinished existing repo.py.
  - Accept text or Markdown requirements.
  - Convert requirements into tasks.md.
  - Always create bugs.md.
  - Pipe plan.py output directly into run.py.
  - Use multiple top-level codex exec processes.
  - Never use Codex subagents.
  - Use isolated Git worktrees.
  - Push code through GitHub pull requests using gh.
  - Separate semantic agent decisions from mechanical coordinator decisions.
  - Expose every agent role explicitly.
  - Show concise live progress without interleaving raw agent output.
 - Bound attempts, reviews, repairs, and audits.
  - Use one monotonic review state machine with no transition back to initial review.
  - Count every post-review candidate change against one shared repair budget.
  - Cap every review-session model call, including malformed-output replacements.
  - Apply hard deadlines to agents, validation commands, provider operations, and provider checks.
  - Give every retrying or polling loop a durable attempt limit or deadline and a terminal state.
 - Resume after interruption.
  - Clean ephemeral resources automatically.
  - Retain campaign evidence unless explicitly deleted.

  # Repository structure

  C:\Code\Projects\Relay\
  ├── AGENTS.md
  ├── README.md
  ├── repo.py
  ├── plan.py
  ├── run.py
  ├── status.py
  ├── test_workflow.py
  └── .gitignore

  Relay is not an installable package. It has no:

  - pyproject.toml
  - Package directory
  - Service
  - Daemon
  - Database
  - Web interface
  - Third-party Python dependency
  - Runtime dependency on multi-agent-prompt.txt
  - Provider abstraction before another provider is needed

  # Existing repo.py

  An unfinished repository-creation script already exists at:

  C:\Code\Projects\Tools\scripts\repo.py

  This file is the starting point for Relay’s repo.py.

  Its current behavior has not yet been verified because the local command runner failed when attempting to read it. No assumptions should be made about which features
  it already supports.

  Before changing it:

  1. Read the entire file.
  2. Check the Tools repository’s Git status.
  3. Find every caller, test, and documentation reference.
  4. Run its existing help and safe tests.
  5. Record current supported arguments.
  6. Identify working behavior.
  7. Identify incomplete behavior.
  8. Identify unsafe or destructive behavior.
  9. Copy the working implementation into Relay.
  10. Complete only verified gaps.
  11. Keep the original Tools copy unchanged until Relay passes verification.

  Relay must not import the Tools copy at runtime.

  Removing or redirecting the Tools copy is a separate, explicitly approved cleanup task after Relay works.

  # Relay AGENTS.md

  Relay ships with its own operational instructions:

  # Relay development instructions

  - Use Python 3.11+ and the Python standard library.
  - Keep Relay as directly executable scripts, not an installed package.
  - Resolve support paths relative to __file__.
  - Never embed credentials, user paths, repository names, or remote identities.
  - Validate structured agent output before changing state.
  - Only run.py may modify active campaign ledgers and runtime state.
  - Use isolated Git worktrees and GitHub pull requests for all implementation.
  - Validate resolved paths before worktree or campaign cleanup.
  - Keep attempts, reviews, repairs, and audits mechanically bounded.
  - Persist counters before launching a process; crashes never grant a free retry.
  - Never transition a review session back to initial review or triage.
  - Count blocker, merge-conflict, integration, and provider-check repairs against one shared fix-loop budget.
  - Apply hard deadlines to agent processes, validation, provider operations, and provider checks.
  - Plan one finite audit campaign; fixes never trigger recursive audit planning.
  - Never ask an agent to continue finding arbitrary issues.
  - Keep raw agent logs separate from coordinator output.
  - Add deterministic tests for transitions, retries, recovery, PR handling,
    concurrency, and cleanup.
  - Run tests with: python -m unittest -v test_workflow.py

  Relay reads and honors the target repository’s own AGENTS.md.

  Relay does not copy its AGENTS.md into target repositories.

  # Target-repository files

  A Relay campaign creates:

  <target-repository>\
  ├── tasks.md
  ├── bugs.md
  └── .relay\
      ├── state.json
      └── logs\

  The canonical name is tasks.md, plural. There is no separate runtime plan.md; the output from plan.py becomes tasks.md.

  Relay adds these paths to .git\info\exclude:

  tasks.md
  bugs.md
  .relay/

  This keeps coordination files out of product PRs without changing the repository’s tracked .gitignore.

  Responsibilities:

  - tasks.md: authoritative implementation plan.
  - bugs.md: authoritative bug ledger.
  - .relay\state.json: mechanical execution and recovery state.
  - .relay\logs: separate raw output for every agent call.

  # Agent architecture

  Relay has a deterministic Python coordinator and multiple short-lived agent types.

  The Python coordinator is not an AI agent. It owns process scheduling, validation, Git, GitHub, counters, state transitions, and cleanup.

  ## Agent summary

   Agent type                  Started by    Access                     Purpose
  ━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Repository Scout            plan.py       Read-only                  Inspect an assigned repository area
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Planning Project Manager    plan.py       Read-only                  Convert requirements and scout evidence into tasks
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Worker                      run.py        Assigned worktree write    Implement tasks and bugs or repair an existing candidate, selected by assignment mode
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Contract Reviewer           run.py        Read-only                  Check candidate against acceptance criteria
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Risk Reviewer               run.py        Read-only                  Independently check regressions and candidate-introduced risks
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Triage Project Manager      run.py        Read-only                  Accept, downgrade, reject, or backlog review findings
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Verification Reviewer       run.py        Read-only                  Verify only the accepted blocker and repair delta
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Audit Planner               run.py        Read-only                  Define a finite audit scope from requirements
  ──────────────────────────  ────────────  ─────────────────────────  ────────────────────────────────────────────────────────────────
   Audit Worker                run.py        Read-only                  Execute one bounded audit assignment

  These are logical roles with distinct prompts and output schemas. They do not require different models. Worker is the only write-capable agent type; task, bug, and repair are assignment modes, not separate agents.

  ## Non-agent responsibilities

  The following are deterministic Python operations:

  - Creating repositories
  - Scheduling work
  - Computing ready tasks
  - Detecting declared path conflicts
  - Creating worktrees
  - Validating candidate SHAs
  - Running validation commands
  - Pushing branches
  - Creating PRs
  - Polling GitHub checks
  - Merging PRs
  - Updating ledgers
  - Counting attempts and fix loops
  - Determining terminal states
  - Cleaning worktrees

  No “integration agent” or “coordinator agent” is needed for work Python can perform deterministically.

  # Planning agent flow

  ## Repository Scout

  For a nontrivial existing repository, plan.py partitions relevant files into bounded inspection assignments.

  Several scouts may run concurrently.

  Each scout receives:

  - One directory or subsystem
  - Requirements relevant to that scope
  - Target AGENTS.md
  - Read-only access
  - A structured evidence schema

  Each scout returns:

  {
    "scope": "src/brace/build.py and related tests",
    "implemented": [],
    "missing": [],
    "conflicts": [],
    "relevantPaths": [],
    "validationCommands": [],
    "evidence": []
  }

  Scouts:

  - Do not propose the final task graph.
  - Do not edit files.
  - Do not communicate with other scouts.
  - Do not widen beyond their assigned scope.

  Small or empty repositories skip scouts and go directly to the Planning PM.

  plan.py fixes the scout assignment list before launching any scout. Scouts cannot create more scouts or expand the list. Let S be that fixed count:

  planningCallLimit = S scouts
                    + 1 Planning PM
                    + formatRetryAllowance

  Every scout and PM launch consumes this in-process call budget and the agent timeout. Because plan.py is read-only and produces no campaign until it succeeds, restarting plan.py is an explicit user action rather than an automated retry loop.

  ## Planning Project Manager

  The Planning PM receives:

  - Project requirements
  - Repository instructions
  - Repository tree
  - Scout evidence
  - Current Git base SHA
  - Relevant existing implementation
  - Required task schema

  It must:

  1. Reconcile requirements with existing implementation.
  2. Mark already-satisfied requirements as such.
  3. Create bounded tasks only for missing work.
  4. Give every task explicit acceptance criteria.
  5. Give every task validation commands.
  6. Identify expected changed paths.
  7. Add genuine dependencies.
  8. Avoid historical execution-order dependencies.
  9. Separate independent work.
  10. Return structured task records.
  11. Exit.

  The Planning PM does not write tasks.md. plan.py validates the result and renders Markdown.

  The Planning PM cannot create more scout assignments, request another planning pass, or exceed planningCallLimit. Invalid structured output may use only the remaining formatRetryAllowance.

  # Build agent flow

  ## Worker

  Worker is Relay's only write-capable agent. The coordinator supplies exactly one assignment with one of three modes:

  - task: implement one ready planned task in a new task worktree and branch.
  - bug: implement one accepted audit bug in a new bug worktree and branch.
  - repair: modify an existing candidate branch only for accepted blockers, repair-delta regressions, merge conflicts, provider-check failures, or required base updates.

  All modes use the same Worker prompt template, permissions, result schema, and candidate validation. Mode changes the supplied scope and which durable budget is consumed; it does not create another agent type.

  It may:

  - Modify its assigned worktree.
  - Run tests.
  - Create a local candidate commit.
  - Return a structured result.

  It may not:

  - Work on another assignment.
  - Edit tasks.md, bugs.md, or .relay.
  - Push its branch.
  - Create or merge a PR.
  - Change acceptance criteria.
  - Change dependencies.
  - Widen scope without returning a blocker.

  Result:

  {
    "mode": "task",
    "assignmentId": "TASK-0001",
    "status": "candidate",
    "candidateSha": "93c61a4",
    "changedPaths": [
      "src/brace/build.py",
      "tests/test_build.py"
    ],
    "validation": [
      {
        "command": "python -m unittest tests.test_build",
        "exitCode": 0
      }
    ],
    "summary": "Persisted review counters across restart"
  }

  ## Contract Reviewer

  The Contract Reviewer checks:

  - Acceptance criteria
  - Required behavior
  - Required tests
  - Candidate diff
  - Candidate-introduced regressions directly related to the task

  It cannot search the entire repository for unrelated issues.

  ## Risk Reviewer

  The Risk Reviewer independently checks:

  - Candidate correctness
  - Regression risk
  - Security or data-loss risk introduced by the candidate
  - Error paths changed by the candidate
  - Missing tests for candidate behavior

  It cannot review unrelated pre-existing code.

  The Contract and Risk reviewers:

  - Run independently.
  - Do not see each other’s findings.
  - Run once for the initial candidate.
  - Return evidence-backed structured findings.
  - Cannot directly trigger repairs.

  ## Triage Project Manager

  The Triage PM receives:

  - Task contract
  - Candidate SHA
  - Both initial review results
  - Validation evidence
  - Existing bug ledger
  - Current fix-loop count

  For every finding, it returns one decision:

  accept-blocker
  backlog
  discard
  needs-user

  Rules:

  - Accepted P0/P1 findings block integration.
  - P2 findings go to backlog.
  - P3 findings are discarded.
  - Findings without reproduction or evidence are discarded.
  - Pre-existing problems not caused by the candidate do not block it.
  - Triage completes once per review session after the two initial reviewer assignments; verification results never trigger another triage.
  - Reviewers are not recursively asked for more opinions.

  The PM makes the semantic decision. Python enforces the resulting transition.

  ### Worker repair mode

  A Worker in repair mode receives:

  - Original task contract
  - Accepted blocker IDs
  - Evidence and reproduction
  - Current candidate SHA
  - Remaining fix-loop count

  It may change only what is needed to resolve the assigned post-review failure. Every candidate change after initial review starts consumes the same durable FixLoopCount, including:

  - Accepted P0/P1 blocker repairs
  - Repair-delta regressions
  - Merge-conflict repairs
  - Required provider-check repairs
  - Candidate updates required after another PR merges

  It does not address:

  - Backlog findings
  - Discarded findings
  - Unrelated cleanup
  - Speculative improvements

  It commits a replacement candidate to the same task branch.

  ## Verification Reviewer

  The Verification Reviewer receives:

  - Accepted blocker
  - Previous failing candidate
  - Repaired candidate
  - Exact repair diff
  - Reproduction command

  It returns only:

  - resolved
  - unresolved
  - invalid-result

  It cannot reopen the full repository or original candidate review.

  A new P0/P1 failure is admissible only when it is directly introduced by the repair delta. It remains in the same review session and consumes the same remaining repair budget. A finding outside the accepted blocker or repair delta is rejected mechanically and cannot create a new review session, task, or blocking bug.

  # Audit agent flow

  ## Audit Planner

  After all planned tasks integrate, the Audit Planner receives:

  - Original requirements
  - Completed task contracts
  - Integrated PRs
  - Validation evidence
  - Current bugs.md

  It returns a finite list of audit assignments.

  Audit planning runs exactly once per campaign. Its call counter is persisted before launch. Invalid output may use only the campaign's bounded format-retry allowance; exhaustion transitions to needs-user.

  Each audit assignment must define:

  - Scope
  - Relevant requirements
  - Paths
  - Commands or evidence to inspect
  - Explicit completion condition

  It cannot return an instruction such as “find any remaining bugs.”

  ## Audit Worker

  Each Audit Worker receives exactly one audit assignment.

  Audit workers may run concurrently when scopes do not overlap.

  Each finding must provide:

  - Severity
  - Exact location
  - Observable failure
  - Reproduction or failing check
  - Requirement violated
  - Evidence

  Audit workers are read-only.

  Their findings go to the Triage PM once.

  Each declared audit scope runs exactly once. Completing a bug fix never generates another audit plan or reruns a completed audit scope. After fixes, Relay runs only the declared deterministic validation and the candidate's delta-limited verification.

  ### Worker bug mode

  An accepted P0/P1 audit bug becomes an isolated bug assignment handled by a Worker in bug mode. It receives:

  - One bug record
  - Evidence
  - Reproduction
  - Allowed paths
  - Acceptance criteria
  - Required validation

  It works in its own branch and worktree and produces a candidate commit using the same Worker result schema.

  Bug candidates use the same:

  - Contract review
  - Risk review
  - Triage
  - Repair
  - Verification
  - PR
  - Provider-check
  - Merge

  protocol as planned tasks.

  P2 bugs remain in backlog. P3 findings are discarded.

  # Agent concurrency

  --workers is the maximum number of simultaneous codex exec processes.

  It is not a detected or guaranteed Codex account limit.

  During planning, scout processes share the planning process budget. The Planning PM runs after scout results are collected.

  During execution, the pool may contain any mixture:

  slot 1: TASK-0001 worker mode=task
  slot 2: TASK-0002 contract reviewer
  slot 3: BUG-0001 worker mode=bug

  There is no fixed allocation of one Worker and two reviewers.

  When a slot becomes free, Relay launches the highest-priority runnable job.

  A job waits only when:

  - A genuine dependency is incomplete.
  - Declared paths conflict with active work.
  - Its base SHA is invalid.
  - It requires user input.
  - Every configured process slot is occupied.
  - Codex or GitHub is throttling requests.
  - A provider check is pending.

  Waiting for GitHub does not occupy a Codex process.

  Every wait is bounded. An agent, validation command, provider operation, or provider check that reaches its deadline transitions to a counted failure, waiting-provider, or needs-user state while unrelated work continues.

  Default hard deadlines:

  - Agent execution: 60 minutes
  - Validation command: 30 minutes
  - GitHub operation: 5 minutes
  - GitHub check wait: 60 minutes

  These values are command-line overrides, but none may be disabled. If no process is active, no external wait remains, unfinished work exists, and no legal transition is available, Relay exits needs-user instead of spinning.

  # repo.py

  The final script creates repositories that Relay can plan and build.

  ## Local creation

  python C:\Code\Projects\Relay\repo.py `
    --path C:\Code\Projects\NewProject

  Required behavior:

  1. Resolve the requested path.
  2. Refuse a nonempty directory.
  3. Refuse an existing Git repository.
  4. Create the directory.
  5. Initialize main.
  6. Create a minimal README.md.
  7. Create an initial commit.
  8. Print the path, branch, and SHA.

  ## GitHub creation

  python C:\Code\Projects\Relay\repo.py `
    --path C:\Code\Projects\NewProject `
    --github owner/new-project `
    --private

  Visibility must be explicit:

  --private
  --public

  It uses gh repo create, adds origin, and pushes main.

  It preserves the local repository if GitHub creation fails.

  # plan.py

  Command:

  python C:\Code\Projects\Relay\plan.py `
    --repo C:\Code\Projects\Brace `
    --requirements C:\Code\brace-plan.txt `
    --workers 3

  plan.py is read-only with respect to the target.

  It runs scouts when useful, runs the Planning PM, validates structured output, and emits only canonical tasks.md content to stdout.

  Progress goes to stderr:

  Relay Planner
  Repository:   C:\Code\Projects\Brace
  Requirements: C:\Code\brace-plan.txt
  Workers:      3 configured

  15:10:01  SCOUT      slot=1 scope=src/brace
  15:10:01  SCOUT      slot=2 scope=tests
  15:10:01  SCOUT      slot=3 scope=docs
  15:11:02  SYNTHESIZE role=planning-pm
  15:11:58  VALIDATE   tasks=24
  15:11:59  OUTPUT     ready=7 blocked=17

  Stdout contains only:

  # Tasks

  <!-- relay: planned-base=0123456789abcdef requirements=abc123 -->

  ## TASK-0001 — Preserve review counters across restart

  - Status: ready
  - Priority: P1
  - Dependencies: none
  - Allowed paths:
    - `src/brace/build.py`
    - `tests/test_build.py`
  - Acceptance criteria:
    - Counter survives coordinator restart.
    - Counter survives replacement candidate commits.
  - Validation:
    - `python -m unittest tests.test_build`
  - Attempt: 0/3
  - Fix loop: 0/2
  - Branch: pending
  - Pull request: pending
  - Candidate: pending

  # Plan-to-run pipeline

  python C:\Code\Projects\Relay\plan.py `
    --repo C:\Code\Projects\Brace `
    --requirements C:\Code\brace-plan.txt `
    --workers 3 |
  python C:\Code\Projects\Relay\run.py `
    --repo C:\Code\Projects\Brace `
    --workers 3 `
    --fix-loops 2 `
    --merge-method squash

  To inspect the plan first:

  python C:\Code\Projects\Relay\plan.py `
    --repo C:\Code\Projects\Brace `
    --requirements C:\Code\brace-plan.txt `
    > C:\Code\proposed-tasks.md

  Then:

  Get-Content -Raw C:\Code\proposed-tasks.md |
  python C:\Code\Projects\Relay\run.py `
    --repo C:\Code\Projects\Brace `
    --workers 3 `
    --fix-loops 2

  A redirected plan is user-owned and is never deleted by Relay.

  # run.py initialization

  For a new campaign, run.py:

  1. Reads all stdin before modifying the target.
  2. Validates the Markdown plan.
  3. Verifies its base SHA.
  4. Verifies task IDs and dependencies.
  5. Creates .relay.
  6. Adds Relay paths to .git\info\exclude.
  7. Writes stdin to tasks.md.
  8. Always creates bugs.md.
  9. Creates .relay\state.json.
  10. Runs GitHub preflight.
  11. Starts ready work.

  No worker starts before all three exist:

  tasks.md
  bugs.md
  .relay\state.json

  Existing non-Relay tasks.md or bugs.md files are never overwritten.

  # Runtime state

  Example .relay\state.json:

  {
    "schemaVersion": 1,
    "campaignId": "20260911-142301",
    "repository": "C:\\Code\\Projects\\Brace",
    "phase": "build",
    "baseSha": "0123456789abcdef",
    "workerLimit": 3,
    "taskAttemptLimit": 3,
    "fixLoopLimit": 2,
    "formatRetryAllowance": 2,
    "agentTimeoutSeconds": 3600,
    "validationTimeoutSeconds": 1800,
    "providerTimeoutSeconds": 300,
    "providerCheckTimeoutSeconds": 3600,
    "providerAttemptLimit": 3,
    "mergeMethod": "squash",
    "activeProcesses": {},
    "worktrees": {},
    "candidateShas": {},
    "attemptCounters": {},
    "reviewSessions": {
      "TASK-0001": {
        "reviewSessionId": "TASK-0001-REVIEW-1",
        "initialCandidateSha": "93c61a4",
        "phase": "verify-1",
        "initialReviewAssignmentsStarted": 2,
        "initialReviewAssignmentsCompleted": 2,
        "triageCompleted": true,
        "acceptedBlockerIds": ["BUG-0001"],
        "repairAttemptsStarted": 1,
        "reviewCallsStarted": 5,
        "reviewCallLimit": 9
      }
    },
    "pullRequests": {},
    "providerAttemptCounters": {},
    "auditPlanStarted": true,
    "auditPlanCompleted": true,
    "auditCallsStarted": 4,
    "auditCallLimit": 7,
    "auditScopes": {},
    "pendingLedgerOperation": null
  }

  The Markdown ledgers hold task and bug meaning. JSON holds runtime mechanics.

  Only run.py modifies active campaign state.

  Every attempt and call counter is atomically persisted before its process starts. A crash may consume an attempt without receiving a result, but it can never grant a free retry. Replacement commits, replacement processes, restarts, and reviewer changes retain the same reviewSessionId and counters.

  Every retrying and polling loop has exactly one of:

  - A durable attempt limit followed by failed, waiting-provider, or needs-user
  - A wall-clock deadline followed by waiting-provider or needs-user

  No agent may add tasks, audit scopes, review sessions, or repair budgets after planning. Review findings attach to the candidate's existing review session; they cannot recursively create fresh bug-fix assignments.

  # Execution state machine

  ready
    ↓
  implementing
    ↓
  candidate-validation
    ↓
  push-and-open-pr
    ↓
  initial-review
    │ exactly one Contract Reviewer assignment and one Risk Reviewer assignment
    ↓
  triage
    │ exactly one completed PM decision
    ├── no accepted P0/P1 ───────────────────────────────→ approved
    │
    └── accepted P0/P1
              ↓
          repair-1
              ↓
          verify-1
              ├── resolved ──────────────────────────────→ approved
              └── unresolved
                       ↓
                   repair-2
                       ↓
                   verify-2
                       ├── resolved ──────────────────────→ approved
                       └── unresolved ────────────────────→ needs-user

  The only legal review-session phases are:

  - initial-review
  - triage
  - repair-N
  - verify-N
  - approved
  - needs-user

  Forbidden transitions include:

  - verify-N to initial-review
  - verify-N to triage
  - repair-N to initial-review
  - approved to any review phase
  - needs-user to any repair or review phase without an explicit new user-authorized campaign

  A replacement candidate never creates a new review session. Before initial review begins, a candidate validation or integration-readiness failure consumes a normal task attempt. After initial review begins, every code-changing repair consumes the single FixLoopCount.

  Python independently verifies:

  - Reported commit equals worktree HEAD.
  - Candidate descends from the expected base.
  - Changed paths stay within scope.
  - Required validation passes.
  - PR head matches the reviewed SHA.
  - Required GitHub checks pass.
  - Review and repair counters have not exceeded limits.
  - The requested transition is legal from the persisted phase.
  - The review session and all its counters match the original reviewSessionId.

  # GitHub pull requests

  ## Preflight

  Before launching workers:

  git remote get-url origin
  gh auth status
  gh repo view

  Relay stops if:

  - origin is missing.
  - The remote is not GitHub.
  - gh authentication fails.
  - The GitHub repository cannot be queried.

  ## Candidate publication

  Only run.py pushes:

  git push --set-upstream origin relay/TASK-0001

  Only run.py creates PRs:

  gh pr create `
    --base main `
    --head relay/TASK-0001 `
    --title "TASK-0001: Preserve review counters" `
    --body-file <generated-pr-body>

  The PR number and URL are recorded in tasks.md or bugs.md.

  ## Checks and merge

  Relay polls GitHub checks without occupying a Codex process.

  A PR merges only when:

  - The PR head matches the reviewed SHA.
  - Local validation passes.
  - Internal bounded review passes.
  - No accepted P0/P1 blocker remains.
  - Required GitHub checks pass.
  - The PR is mergeable.

  Default:

  gh pr merge <number> --squash --delete-branch

  Supported merge methods:

  squash
  merge
  rebase

  Required human approval produces waiting-provider; it does not cause more AI reviews.

  # Finite review policy

  Each initial candidate creates exactly one durable review session. The session can move only forward through the execution state machine.

  For every review session:

  1. Persist the reviewSessionId before launching any reviewer.
  2. Start one Contract Reviewer assignment and one independent Risk Reviewer assignment exactly once.
  3. Allow only bounded replacement calls for malformed structured output; replacements receive the identical scope and are not new reviews.
  4. Run one completed Triage PM decision.
  5. Require severity, exact location, observable failure, reproduction or failing check, and candidate-introduction evidence for every finding.
  6. Reject unsupported or pre-existing findings as blockers.
  7. Record accepted findings in bugs.md.
  8. Send P2 findings to backlog without blocking.
  9. Discard P3 findings.
  10. Approve immediately when no P0/P1 blocker is accepted.
  11. Repair accepted P0/P1 blockers as one batch.
  12. Count every post-review candidate change against the same FixLoopCount.
  13. Run one delta-limited Verification Reviewer after each repair.
  14. Treat a repair-delta P0/P1 as unresolved work in the same session, never as a new task or review session.
  15. Never rerun initial review or triage after a repair.
  16. Never reset counters after a commit, process, reviewer, restart, merge conflict, or provider failure.
  17. Transition directly to needs-user when FixLoopCount or the total review-call budget is exhausted.
  18. Continue unrelated runnable work, then exit code 2 when only needs-user or waiting-provider work remains.

  No model output can request a state transition that Python does not allow.

  ## Hard review-call budget

  FixLoopCount does not by itself bound malformed output, crashes, or replacement processes. Relay therefore derives one total call limit for the entire review session:

  reviewCallLimit = 2 initial reviewers
                  + 1 triage PM
                  + (2 × FixLoopCount)
                  + formatRetryAllowance

  With FixLoopCount=2 and formatRetryAllowance=2:

  reviewCallLimit = 2 + 1 + 4 + 2 = 9

  The two calls per repair round are one Worker in repair mode and one Verification Reviewer. Every attempted launch consumes this budget, including a timed-out process, crash, empty result, and malformed result. The counter is persisted before launch.

  ## Audit termination

  Relay creates one audit plan per campaign. Let S be the number of scopes in that validated plan:

  auditCallLimit = 1 audit planner
                 + S audit workers
                 + 1 audit triage PM
                 + formatRetryAllowance

  Each scope is one logical assignment. Any malformed-output replacement receives the identical scope and consumes the shared auditCallLimit. Each scope is triaged once as part of the single audit triage decision. Accepted bugs use the same finite candidate state machine. Bug fixes never create a new audit plan, rerun completed audit scopes, expand S, or ask for another unrestricted review. A new audit requires an explicit new user command and campaign.

  ## Global liveness invariant

  At campaign initialization, the task set is finite. At audit initialization, the scope set is finite. No agent can append runnable work except an explicit user-approved new campaign. Every process has a deadline, every retry consumes a persisted counter, every provider wait has a deadline, and every state has either a legal forward transition or a terminal outcome. Therefore run.py must never poll or dispatch indefinitely without consuming a finite budget or approaching a deadline.

  # Console output

  The main terminal includes role names:

  Relay
  Repository:   C:\Code\Projects\Brace
  Workers:      3 configured
  Fix loops:    2
  Review calls: 9 maximum per candidate
  Agent timeout: 60m
  Check timeout: 60m
  Merge method: squash

  15:11:14  RECEIVED   plan tasks=24
  15:11:14  CREATED    tasks.md
  15:11:14  CREATED    bugs.md
  15:11:16  GITHUB     authenticated
  15:11:18  READY      tasks=7 blocked=17

  15:11:19  START      slot=1 role=worker mode=task task=TASK-0001
  15:11:19  START      slot=2 role=worker mode=task task=TASK-0002
  15:11:20  START      slot=3 role=worker mode=task task=TASK-0003

  15:12:43  CANDIDATE  task=TASK-0002 sha=b4f82d1 tests=passed
  15:12:45  PUSHED     task=TASK-0002
  15:12:48  PR         task=TASK-0002 number=73
  15:12:49  START      slot=2 role=contract-reviewer task=TASK-0002

  15:13:03  START      slot=1 role=risk-reviewer task=TASK-0002
  15:13:41  TRIAGE     role=project-manager task=TASK-0002 blockers=0 backlog=1
  15:13:41  BUG        BUG-0001 severity=P2 status=backlog
  15:13:43  CHECKS     pr=73 status=passed
  15:13:46  MERGED     task=TASK-0002 pr=73

  15:14:02  REPAIR     role=worker mode=repair task=TASK-0003 fix=1/2 calls=4/9
  15:14:34  VERIFY     role=verification-reviewer task=TASK-0003 fix=1/2 calls=5/9

  When otherwise quiet:

  15:15:06  ACTIVE  processes=3/3 ready=5 review=2 checks=1 completed=8 nearest-deadline=18m

  Raw output remains isolated:

  .relay\logs\TASK-0001-worker-task-attempt-1.log
  .relay\logs\TASK-0001-contract-review.log
  .relay\logs\TASK-0001-risk-review.log
  .relay\logs\TASK-0001-triage.log
  .relay\logs\TASK-0001-repair-1.log
  .relay\logs\TASK-0001-verification-1.log
  .relay\logs\AUDIT-0001.log
  .relay\logs\provider.log

  # status.py

  status.py is read-only and reports active roles:

  Relay campaign: 20260911-142301
  Phase:          build
  Elapsed:        18m 14s

  Processes
    Configured:          3
    Active:              3
    Workers (task):      1
    Contract reviewers:  1
    Risk reviewers:      0
    Workers (repair):    1

  Review sessions
    TASK-0002: approved calls=3/9
    TASK-0003: verify-1 calls=5/9 fixes=1/2

  Tasks
    Total:       24
    Ready:        5
    Active:       1
    Reviewing:    1
    Repairing:    1
    Integrated:  12
    Blocked:      3

  Bugs
    P0 active:    0
    P1 active:    1
    P2 backlog:   3
    Resolved:     6

  Pull requests
    Open:         4
    Checks:       1 pending
    Mergeable:    3

  # Cleanup

  ## Automatic cleanup

  After successful integration, Relay removes:

  - Completed worktree
  - Git worktree registration
  - Temporary prompt and result files
  - Dead process record
  - Merged local branch
  - Remote branch when PR deletion succeeds

  At campaign completion, it removes the coordinator lock.

  It retains:

  tasks.md
  bugs.md
  .relay\state.json
  .relay\logs\

  ## Explicit permanent cleanup

  Preview:

  python C:\Code\Projects\Relay\run.py `
    --repo C:\Code\Projects\Brace `
    --cleanup

  Confirm:

  python C:\Code\Projects\Relay\run.py `
    --repo C:\Code\Projects\Brace `
    --cleanup `
    --confirm

  Permanent cleanup is allowed only when:

  - Campaign state is complete.
  - No process is active.
  - No Relay PR is open.
  - No Relay worktree exists.
  - State identifies the same resolved repository.
  - Markdown files contain the matching Relay ownership marker.
  - Every deletion target resolves within the target repository.

  It removes:

  - tasks.md
  - bugs.md
  - .relay
  - Relay-owned .git\info\exclude entries

  It never removes:

  - Requirements files
  - Redirected proposed plans
  - Source files
  - Target repository
  - Open PR branches
  - Unmerged branches
  - Non-Relay ledgers

  # Interruption and resume

  On Ctrl+C:

  1. Stop launching work.
  2. Mark active assignments interrupted.
  3. Stop child processes.
  4. Preserve worktrees, branches, PRs, and logs.
  5. Save runtime state.
  6. Exit.

  Resume:

  python C:\Code\Projects\Relay\run.py `
    --repo C:\Code\Projects\Brace

  Relay reconciles local and GitHub state before launching fresh contexts. It must not duplicate pushes, PRs, reviews, or merges.

  Exit codes:

  0    Completed
  1    Operational or validation failure
  2    User decision required
  130  Interrupted

  # Implementation sequence

  1. Create C:\Code\Projects\Relay as its own Git repository.
  2. Add AGENTS.md.
  3. Inspect the unfinished Tools repo.py.
  4. Inspect its callers, tests, documentation, and Git state.
  5. Copy its working code into Relay.
  6. Complete only verified repository-creation gaps.
  7. Add repository-creation regression tests.
  8. Define agent input and output schemas.
  9. Define the fixed tasks.md format.
  10. Define the fixed bugs.md format.
  11. Implement repository scouts in plan.py.
  12. Implement the Planning PM.
  13. Implement structured planning validation.
  14. Emit canonical Markdown exclusively on stdout.
  15. Send planning progress exclusively to stderr.
  16. Implement piped-plan ingestion in run.py.
  17. Implement mandatory ledger creation.
  18. Implement Relay ownership markers and local exclusion.
  19. Implement runtime state, locking, and recoverable ledger writes.
  20. Implement the task scheduler.
  21. Implement the Worker role with task, bug, and repair modes using one prompt and result schema.
  22. Implement Contract Reviewers.
  23. Implement Risk Reviewers.
  24. Implement Triage PM calls.
  25. Implement Verification Reviewers.
  26. Implement one durable monotonic review session per candidate.
  27. Implement pre-launch attempt and review-call accounting.
  28. Implement the derived hard reviewCallLimit.
  29. Implement non-disableable agent, validation, provider, and check deadlines.
  30. Implement candidate validation for every Worker mode.
  31. Implement safe worktree management.
  32. Implement GitHub preflight.
  33. Implement branch push and PR creation.
  34. Implement GitHub check polling and merging.
  35. Route Worker repair mode through the shared FixLoopCount.
  36. Implement one-shot Audit Planning.
  37. Implement single-run parallel Audit Workers.
  38. Route accepted audit bugs to Worker bug mode using the same finite candidate state machine.
  39. Implement finite task, planning, review, repair, provider, and audit limits.
  40. Implement concise role-aware console output with Worker modes, deadlines, and budgets.
  41. Implement isolated raw logging.
  42. Implement interruption and recovery.
  43. Implement status.py.
  44. Implement automatic ephemeral cleanup.
  45. Implement previewed permanent cleanup.
  46. Run deterministic and adversarial termination tests.
  47. Smoke-test against a disposable GitHub repository.
  48. Dogfood Relay against Brace from a selected clean base SHA.
  49. Decide separately whether to remove the original Tools repo.py.

  # Required verification

  test_workflow.py must prove:

  ## Repository creation

  - Existing valid repo.py behavior is preserved.
  - Nonempty targets are never overwritten.
  - Existing repositories are never replaced.
  - GitHub failure preserves local work.
  - Relay has no runtime dependency on Tools.

  ## Planning

  - Scouts are read-only and scope-bounded.
  - Independent scouts overlap in execution.
  - Planning PM receives all scout evidence.
  - plan.py never modifies the target.
  - Progress never enters stdout.
  - Stdout contains only valid tasks.md.
  - Planning attempts terminate at their configured limit.
  - Scout assignments are fixed before launch and cannot recursively expand.
  - planningCallsStarted never exceeds scout count + 1 + formatRetryAllowance.
  - Hung scouts and Planning PM calls stop at agentTimeoutSeconds.
  - plan.py | run.py works as a real pipe.

  ## Agent dispatch

  - Every logical agent type receives the correct prompt and schema.
  - Read-only roles cannot modify worktrees.
  - Worker is the only write-capable role and can modify only its assigned worktree.
  - Worker task, bug, and repair modes use the same prompt template and result schema.
  - Worker mode is coordinator-supplied and cannot be changed by agent output.
  - Worker task and bug modes consume the normal implementation-attempt budget.
  - Worker repair mode consumes the existing review session's FixLoopCount and reviewCallLimit.
  - Agents cannot modify shared ledgers.
  - Role output is rejected when its candidate or assignment ID is wrong.

  ## Throughput

  - Independent task implementations overlap.
  - Peak concurrency never exceeds --workers.
  - Completed slots refill immediately.
  - Review and repair jobs share the process pool.
  - GitHub waits do not consume Codex process slots.
  - Declared path conflicts prevent unsafe overlap.
  - Historical order does not serialize independent tasks.

  ## Reviews

  - Exactly one Contract Reviewer assignment and one Risk Reviewer assignment exist per review session.
  - Reviewer results remain independent.
  - Triage completes once per review session.
  - Unsupported findings are discarded.
  - P2/P3 findings do not trigger repairs.
  - Verification is restricted to blocker and repair delta.
  - Review counters survive commit and process replacement.
  - Review counters survive restart.
  - An always-blocking reviewer reaches needs-user after exactly FixLoopCount.
  - A replacement candidate cannot create a new review session.
  - No legal transition returns to initial-review or triage.
  - Merge-conflict and provider-check repairs consume the same FixLoopCount.
  - Every agent launch is counted and persisted before process creation.
  - Malformed and empty results consume reviewCallLimit.
  - A reviewer that invents a different P1 on every call still terminates.
  - Crashes immediately before and after every launch still terminate.
  - One hundred repeated restarts cannot exceed reviewCallLimit or FixLoopCount.
  - reviewCallsStarted never exceeds 2 + 1 + (2 × FixLoopCount) + formatRetryAllowance.
  - Every terminal review session ends approved or needs-user.

  ## Liveness

  - A nonterminating agent is stopped at agentTimeoutSeconds.
  - A nonterminating validation command is stopped at validationTimeoutSeconds.
  - A hung provider operation is stopped at providerTimeoutSeconds.
  - Permanently pending checks transition to waiting-provider at providerCheckTimeoutSeconds.
  - Provider attempts never exceed providerAttemptLimit.
  - Timed-out calls consume their applicable durable budget.
  - An incomplete campaign with no active process and no legal transition exits needs-user instead of spinning.

  ## Audit termination

  - Audit planning starts no more than once per campaign.
  - Each declared audit scope starts no more than once.
  - Audit findings are triaged once.
  - auditCallsStarted never exceeds 1 + audit scope count + 1 + formatRetryAllowance.
  - Bug fixes do not create another audit plan.
  - Bug fixes do not rerun completed audit scopes.
  - An auditor that requests more audit work cannot expand the persisted audit plan.
  - Review findings attach to their current review session and cannot recursively create a new runnable bug-fix assignment.

  ## GitHub

  - Valid candidates push once.
  - PRs create once.
  - PR IDs enter the correct ledger.
  - Pending checks do not block unrelated work.
  - Failed checks prevent merge.
  - Head SHA drift prevents merge.
  - Provider-required approval produces waiting-provider.
  - Successful reviews and checks permit merge.
  - Restart does not duplicate pushes, PRs, or merges.

  ## Recovery and cleanup

  - Every campaign creates tasks.md and bugs.md.
  - Interrupted ledger writes recover.
  - Ctrl+C produces resumable state.
  - Raw outputs remain in separate logs.
  - Coordinator heartbeat remains visible.
  - Automatic cleanup removes only completed ephemeral resources.
  - Cleanup preview removes nothing.
  - Permanent cleanup rejects incomplete campaigns.
  - Permanent cleanup rejects open PRs and worktrees.
  - Ownership mismatches prevent deletion.
  - Cleanup cannot escape the target repository.
  - Requirements and user-created plan files are never deleted.

  # Empirical acceptance

  A real disposable-repository smoke test must record:

  - Configured process limit
  - Observed peak concurrent Codex processes
  - Codex throttling
  - GitHub throttling
  - Slot utilization
  - Tasks completed per hour
  - PRs opened and merged
  - Reviews per candidate
  - Repairs per candidate
  - Review-call budget consumed per candidate
  - Agent and validation timeouts
  - Ready-but-unscheduled time
  - Dependency wait time
  - File-conflict wait time
  - Provider-check wait time
  - Cleanup results

  Until this test succeeds, Relay may be described as:

  - Pipeline-driven
  - Parallel by construction
  - Worktree-isolated
  - GitHub-integrated
  - Observable
  - Resumable
  - Mechanically bounded

  It must not yet be described as empirically faster.

  # Completeness check

   Requested requirement                  Covered in
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Own repository                         Repository structure
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Name Relay                             Entire plan
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Python scripts, not package            Repository structure and non-goals
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Existing unfinished repo.py            Existing repo.py and implementation sequence
  ─────────────────────────────────────  ──────────────────────────────────────────────
   repo.py, plan.py, run.py, status.py    Script sections
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Requirements become task plan          plan.py
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Pipe plan into runner                  Plan-to-run pipeline
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Always create tasks.md                 run.py initialization
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Always create bugs.md                  Target state and initialization
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Explicit agent types                   Agent architecture
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Multiple top-level sessions            Agent concurrency
  ─────────────────────────────────────  ──────────────────────────────────────────────
   No subagent ceiling dependency         Agent concurrency
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Git worktrees                          Parallel execution
  ─────────────────────────────────────  ──────────────────────────────────────────────
   GitHub PRs through gh                  GitHub pull requests
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Finite review loop                     Finite review policy
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Monotonic review transitions           Execution state machine
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Hard total review-call limit           Hard review-call budget
  ─────────────────────────────────────  ──────────────────────────────────────────────
   One Worker for all code changes         Worker modes and finite review policy
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Hung-agent and provider termination    Agent concurrency and liveness verification
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Nonrecursive one-shot audit            Audit agent flow and audit termination
  ─────────────────────────────────────  ──────────────────────────────────────────────
   P0–P3 triage                           Triage PM and review policy
  ─────────────────────────────────────  ──────────────────────────────────────────────
   No endless second opinions             Reviewer and triage contracts
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Visible but non-interleaved output     Console output
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Status command                         status.py
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Resume after interruption              Interruption and resume
  ─────────────────────────────────────  ──────────────────────────────────────────────
   AGENTS.md included                     Relay AGENTS.md
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Automatic ephemeral cleanup            Cleanup
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Explicit durable-state cleanup         Permanent cleanup
  ─────────────────────────────────────  ──────────────────────────────────────────────
   Empirical proof before speed claims    Empirical acceptance

  This is now the complete consolidated plan.
