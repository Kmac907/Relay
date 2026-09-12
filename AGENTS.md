# Relay development instructions

- Use Python 3.11+ and the Python standard library.
- Keep Relay as directly executable scripts, not an installed package.
- Resolve support paths relative to `__file__`.
- Never embed credentials, user paths, repository names, or remote identities.
- Validate structured agent output before changing state.
- Only `run.py` may modify active campaign ledgers and runtime state.
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
- Add deterministic tests for transitions, retries, recovery, PR handling, concurrency, and cleanup.
- Run tests with: `python -m unittest -v test_workflow.py`

Relay reads and honors the target repository's `AGENTS.md`; it does not copy this file there.
