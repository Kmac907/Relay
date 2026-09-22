# Relay Architecture

Relay coordinates a bounded campaign of complete vertical slices. Only `run.py` writes active campaign state and ledgers; agents return structured evidence and Workers commit within an assigned worktree.

## End-to-end workflow

```mermaid
flowchart TD
    A[Requirements] --> B[Planning PM: few complete vertical slices]
    B --> C[One plan reviewer]
    C -->|findings| D[Planning repair]
    D --> E[Exact plan verifier]
    C -->|approved| F[Baseline campaign validation]
    E -->|resolved| F
    F --> G[Worker: complete slice]
    G --> H[Focused validation]
    H --> I[Campaign validation]
    I --> J[One slice reviewer]
    J -->|repair P0/P1| K[Bounded repair Worker]
    K --> H
    I -->|repaired candidate| L[Exact repair verifier]
    L -->|resolved| M[Publish or update PR]
    J -->|approved| M
    M --> N[Provider checks and approvals]
    N --> O[Merge reviewed SHA]
    O --> P{More slices?}
    P -->|yes| G
    P -->|no| Q[One finite audit]
    Q --> R[Publish backlog and complete]
```

Review happens after both validation levels and before a new PR is published. An existing schema-2 PR is retained and updated after migration. The provider head must equal the final reviewed SHA before merge.

## Scope and ownership

```mermaid
flowchart LR
    S[Slice paths] --> MAX[Maximum repair scope]
    D[Completed transitive dependency paths] --> MAX
    MAX --> EXACT[Exact paths required by accepted finding]
    P[Parallel, future, or unrelated paths] -. never automatic .-> EXACT
    EXACT --> W[Repair Worker prompt]
    EXACT --> V[Candidate path validation]
```

Each slice owns its production entrypoint, direct collaborators, contracts, and tests. Dependencies express runtime prerequisites. Literal files, literal directories, segment wildcards, and `**` use one normalized path policy across scheduling, validation, recovery, review repair, audit, deleted files, and backlog tests.

Tests may fake external processes, networks, clocks, and providers. They do not replace an internal production component whose integration the slice is meant to prove.

## Inline bug lifecycle

```mermaid
stateDiagram-v2
    [*] --> ReviewFinding
    ReviewFinding --> active: repair / candidate P0-P1
    ReviewFinding --> backlog: P2 or supported pre-existing
    ReviewFinding --> discarded: P3 or unsupported
    ReviewFinding --> needs_user: concrete human decision
    active --> Repair
    Repair --> Validate
    Validate --> ExactVerify
    ExactVerify --> resolved: finding fixed
    ExactVerify --> Repair: budget remains
    ExactVerify --> needs_user: budget decision required
```

Active, backlog, and needs-user findings are journaled to `bugs.md` before state advances. Discarded findings remain in the review session. Duplicate source assignment and finding IDs update the existing bug. Backlog entries retain a deferral reason; needs-user entries retain a decision reason.

## Final audit

```mermaid
flowchart TD
    AP[One audit planner] --> AW[One read-only Worker per finite scope]
    AW -->|repair with in-scope paths| BUG[Active audit bug]
    AW -->|P2| BACKLOG[Backlog]
    AW -->|P3| DROP[Discard]
    AW -->|decision or outside scope| USER[needs-user]
    BUG --> BW[Bug Worker]
    BW --> SV[Audit-scope validation]
    SV --> CV[Campaign validation]
    CV --> EV[Exact finding verifier]
    EV --> PR[PR, provider checks, merge]
```

Audit Workers provide dispositions directly; there is no triage call. Audit fixes do not receive a new full slice review and never create another audit plan.

## Persistence and recovery

Review sessions move forward through:

```text
slice-review -> approved | scope-resolution | repair-1 | needs-user
scope-resolution -> repair-N | needs-user
repair-N -> verify-N | repair-(N+1) | needs-user
verify-N -> approved | repair-(N+1) | needs-user
approved -> provider processing
```

The session persists the initial candidate, validated review result, accepted bug IDs, exact repair paths, counters, previous/current/pending SHAs, repair number, and final reviewed SHA. Call reservations and ledger intents are saved before external processes or ledger replacement, so a restart replays the same durable candidate or journal rather than granting a free attempt.

## Schema-2 migration

`run.py --recover` recognizes schema 2 before normal unsupported-schema rejection. Preview is read-only. Confirmation validates worktree, candidate, PR head, and ledger ownership; journals the migration; maps saved structured results to one slice review result; preserves counters, validation and audit evidence, provider deadlines, branches, worktrees, PRs, and SHAs; writes schema 3; and exits without launching an agent.

Approved and integrated sessions map directly. Untouched review maps to `slice-review`; numbered repair and verification phases keep their number; an out-of-scope finding maps to `scope-resolution`. Candidate-introduced P0/P1 findings become repair bugs, P2 and supported pre-existing findings become backlog, and P3 is discarded. Raw agent logs are never migration input. The next ordinary invocation resumes the persisted phase.
