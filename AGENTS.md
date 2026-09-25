# Relay development instructions

- Use Python 3.11+ and the Python standard library.
- Keep Relay as directly executable scripts, not an installed package.
- Resolve support paths relative to `__file__`.
- Never embed credentials, user paths, repository names, or remote identities.
- Validate structured agent output before changing state.
- Only `run.py` may modify active campaign ledgers and runtime state.
- Use isolated Git worktrees and GitHub pull requests for all implementation.
- Validate resolved paths before worktree or campaign cleanup.
- Maximize autonomous throughput; stop only for unsafe repository/provider drift, missing credentials/access, or a genuine human decision.
- Do not add campaign budgets, call allowances, retry limits, fix-loop limits, or attempt counters.
- Persist operation identity and status before launching a process so crashes resume from evidence.
- Never transition a review session back to initial review or triage.
- Continue reviews and repairs only while deterministic project status advances; repeated fingerprints, A-B-A cycles, and irrelevant changes require a human decision.
- Apply hard deadlines to agent processes, validation, provider operations, and provider checks.
- Treat deadline expiration as a fresh status-driven operation, never as a consumed allowance.
- Plan one finite audit campaign; fixes never trigger recursive audit planning.
- Never ask an agent to continue finding arbitrary issues.
- Keep raw agent logs separate from coordinator output.
- Add deterministic tests for transitions, status-driven continuation, recovery, PR handling, concurrency, and cleanup.
- Run tests with: `python -m unittest -v test_workflow.py`

Relay reads and honors the target repository's `AGENTS.md`. When one is missing, Relay generates separate generic target rules; it never copies this development file there.
