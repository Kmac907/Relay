# Relay autonomy restoration continuation

## Authority

Resume this branch and finish the implementation. Do not merge partial work.

Relay must maximize autonomous throughput. It may stop only for:

- unsafe repository or provider drift;
- missing credentials or access;
- a genuine human decision.

There are no campaign budgets, call allowances, retry limits, fix-loop limits, or attempt counters. Hard deadlines bound each external process, validation command, and provider command, but expiration starts a fresh status-driven operation; it does not consume or require a grant. Concurrency limits remain. One audit uses one finite, persisted scope set and never recursively plans another audit.

Reviews and repairs advance from deterministic project status. A candidate may continue while its tree, failing evidence, open findings, integration base, repair scope, or provider state advances. An unchanged fingerprint, an A-B-A cycle, irrelevant changes, unsafe drift, missing access, or a decision that cannot be inferred must stop the affected work as `needs-user`. Review never returns to initial review.

The live console keeps the interactive spinner/status heartbeat. Non-interactive output may emit changed or periodic status snapshots; those snapshots are presentation, not persisted WAIT work items.

## Completed before this branch

- The four later counter/recovery changes were exactly reverted and merged in PR #52.
- The reviewer output-schema fixes remain.
- The complete suite passed 158 tests on that merged baseline.
- This branch starts from that merged baseline.

## Work in progress on this branch

`plan.py` and `run.py` contain an incomplete first pass that:

- removes planning campaign ceilings and structured-output allowances;
- changes malformed structured output to fresh correction contexts without a limit;
- starts replacing numeric process reservations with UUID identities;
- starts replacing provider attempt counters with status-driven provider commands;
- starts replacing repair counts with review-epoch work-item identities.

The branch is intentionally not ready to merge. Before editing, inspect the full diff and run:

```powershell
python -m py_compile plan.py run.py
rg -n -i "budget|allowance|attempt|retry|counter|fix.?loop|campaign-active|campaign-agent|format-retr|grant-agent|grant-active" -g "*.py" -g "*.md" .
```

Known incomplete areas include old references to `ProtocolExhaustedError`, `worker_attempt_available`, `protocol_failed`, `fix_attempts_started`, `reserve_fix`, resource grants, provider attempt counters, audit call limits, active-runtime accounting, and tests that assert the removed behavior.

## Implementation plan

1. Finish the runtime model.
   - Remove all allowance, attempt, provider, repair, review-call, audit-call, and campaign-resource counters from new state and control flow.
   - On reconciliation, discard those obsolete fields from an existing schema-4 state without granting or decrementing anything.
   - Keep review epochs only as immutable phase identities (`repair-N` / `verify-N`), never as a limit or allowance.
   - Derive the next review epoch from persisted phase/evidence, not a retry counter.

2. Make loops status-driven.
   - Worker validation repairs continue only when candidate/failure evidence advances.
   - Review repairs continue only when candidate/open-finding evidence advances.
   - Integration repair consumes a persisted candidate if present, validates it, incrementally reviews the delta, and re-evaluates live integration state.
   - Provider and Git operations repeat transient failures with a hard deadline per subprocess; credentials/access and unsafe non-transient failures become `needs-user`.
   - Structured-output correction uses a fresh read-only context and persists rejected evidence, with no allowance or counter.

3. Keep audit finite by data, not a budget.
   - Persist one audit plan and its finite scope IDs.
   - Run each incomplete scope to completion.
   - Audit fixes use the normal queue and never create another audit plan.

4. Remove obsolete CLI and contract fields.
   - Remove campaign time/call grants and format-retry options.
   - New plans contain no resource ceilings.
   - Existing schema-4 plan extras may be ignored so active work can resume; do not add a migration command or converter.

5. Align every durable instruction and document.
   - Update `README.md`, `AGENTS.md`, help text, generated target rules, prompt wording, and any other Markdown.
   - State the three stop conditions exactly.
   - Describe heartbeat/spinner behavior accurately.
   - Remove language about budgets, allowances, attempts, retry counters, fix-loop budgets, resource grants, and exhausted calls.

6. Replace tests instead of weakening them.
   - Delete assertions for removed counters and grants.
   - Add deterministic tests for progress-driven continuation, repeated/cyclic evidence stopping as `needs-user`, crash recovery without a consumed allowance, unlimited schema correction using fresh contexts, transient provider continuation, access failure stopping, one finite audit, and spinner/non-TTY status behavior.
   - Prevent tests for perpetual loops from hanging by advancing their mocked project/provider status.

7. Verify and publish through the normal isolated-worktree PR flow.

```powershell
python -m py_compile plan.py run.py repo.py status.py relay_console.py
python -m unittest -v test_workflow.py
python plan.py --help
python run.py --help
python status.py --help
git diff --check
```

Only after all gates pass: commit the final implementation, push this branch, open one PR, wait for checks, merge, and clean the worktree/branch. Then resume the paused target campaign from live Git/provider/project status; do not edit its active state by hand.

## Continuation prompt

> Resume `fix/restore-throughput` from `CONTINUATION.md`. Treat the Authority section as the acceptance contract. Inspect the entire current diff before editing. Finish the smallest status-driven implementation that removes every campaign budget, allowance, retry/fix-loop limit, and attempt counter while retaining per-process deadlines, concurrency, deterministic progress/cycle safety, one finite audit, and the live heartbeat/spinner. Align README, all docs, AGENTS.md, generated rules, help, prompts, and tests. Do not merge until the complete Relay gate passes. After the Relay PR merges, resume the paused target campaign from live repository/provider status without hand-editing campaign state.
