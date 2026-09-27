Audit the integrated project once against the supplied requirements, plan,
tasks, and authoritative AGENTS.md. Read source and tests; do not edit code.
Return JSON only:
{"bugs":[{"id":"BUG-001","description":"Concrete defect, location, expected behavior and repair scope","evidence":"Reproduction or code evidence","validation":["command verifying the fix"]}]}
Return {"bugs":[]} when no supported defects are found.
Report concrete defects only. Group overlapping repairs into one bug so all
bug workers can start concurrently. Do not create an audit plan, open-ended
review work, speculative improvements, or another audit. Each defect needs
a regression check with an observable pass/fail result, not reviewer approval.
