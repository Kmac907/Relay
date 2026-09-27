Read the requirements, repository, and authoritative AGENTS.md. Plan once.
Return JSON only, with this shape:
{"plan":"# Plan\n...","tasks":[{"id":"TASK-001","title":"...","description":"Scope, acceptance criteria, shared interfaces, and files to own","validation":["a runnable command"]}]}

All tasks start together from the same Git revision. Split independent work
into as many useful parallel tasks as the project supports. Define shared
interfaces in the plan. Combine work that requires another unfinished task
or competes for the same files. Do not invent dependencies or workflow tasks.
Include tests in the implementation tasks, including an integration test
command that can run against the assembled project during final validation.
Commands run through PowerShell on Windows and sh elsewhere; specify setup
where required. Commands must signal failure with a nonzero exit status.
Keep the requested scope. Do not edit files or review/repair your plan in a loop.
