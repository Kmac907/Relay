Resolve the reported merge conflicts in this isolated worktree.
Git has already started merging the supplied base into the PR's head.
Read the conflicted files, both sides, requirements, plan, and AGENTS.md.
Choose the resolution that preserves the intended behavior of both changes;
do not blindly accept one side. Keep changes focused on integration.

Resolve and stage the conflicted files using git add or git rm. Leave the merge pending:
do not commit, abort the merge, reset, rebase, push, operate on another worktree,
edit AGENTS.md or planning artifacts, or change provider settings.
Relay runs the supplied validation, commits, pushes, and completes the PR.
If the conflict needs a user decision, explain it and leave the work intact.
Do not start a review/repair loop or search for unrelated improvements.
Return a concise explanation of the resolution or blocker.
