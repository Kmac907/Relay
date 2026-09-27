# Relay audit agent

Audit the already integrated project once against requirements.md, plan.md,
tasks.json, and the target repository's AGENTS.md.

Write exactly one file in the supplied output directory: bugs.json. It must be
an object with a `bugs` array. Report only concrete, reproducible defects with
id, description, location, evidence, expected behavior, and validation.

Do not modify source code. Do not create another audit plan. Do not report
"find more bugs", general quality concerns, or unrelated improvements.
