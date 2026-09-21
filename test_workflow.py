import io
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import plan
import relay_console
import repo
import run
import status

VALIDATION_ENV = {"RELAY_PWSH": "powershell.exe"} if os.name == "nt" else {}


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class ConsoleTests(unittest.TestCase):
    def test_timed_status_recalculates_for_tty_and_redirected_waits(self):
        clock = [5.0]
        stream = TTYBuffer()
        console = relay_console.Console(stream, interval=3600, monotonic=lambda: clock[0], width=lambda: 80)
        console.update("working", started=10.0, timeout=30)
        clock[0] = 17.0
        console.update("working", started=10.0, timeout=30)
        console.close()
        self.assertIn("working | elapsed 0s / 30s", stream.getvalue())
        self.assertIn("working | elapsed 7s / 30s", stream.getvalue())

        stream, clock = io.StringIO(), [10.0]
        console = relay_console.Console(stream, interval=3600, wait_interval=5, monotonic=lambda: clock[0])
        console.update("working", started=clock[0], timeout=30)
        clock[0] = 15.0
        console.update("working", started=10.0, timeout=30)
        console.close()
        self.assertEqual(stream.getvalue().count("WAIT"), 2)
        self.assertIn("working | elapsed 5s / 30s", stream.getvalue())

    def test_tty_rewrites_one_truncated_line_without_wait_events(self):
        stream = TTYBuffer()
        console = relay_console.Console(stream, interval=3600, width=lambda: 20)
        console.update("assignment worker with a long description")
        console.update("assignment validate")
        console.close()
        output = stream.getvalue()
        self.assertIn("\r", output)
        self.assertNotIn("WAIT", output)
        self.assertTrue(all(len(part) <= 19 for part in output.split("\r") if part.strip()))

    def test_redirected_waits_are_plain_and_rate_limited(self):
        stream, clock = io.StringIO(), [0.0]
        console = relay_console.Console(stream, interval=3600, monotonic=lambda: clock[0])
        console.update("TASK-0001 worker")
        console.update("TASK-0001 worker")
        clock[0] = 299
        console.update("TASK-0001 worker")
        clock[0] = 300
        console.update("TASK-0001 worker")
        console.close()
        output = stream.getvalue()
        self.assertEqual(output.count("WAIT"), 2)
        self.assertNotIn("\r", output)
        self.assertNotRegex(output, r"WAIT\s+[|/\\-]\s")

    def test_concurrent_events_clear_and_redraw_without_interleaving(self):
        stream = TTYBuffer()
        console = relay_console.Console(stream, interval=3600, width=lambda: 80)
        console.update("TASK-0001 worker")
        threads = [threading.Thread(target=console.emit, args=("DONE",), kwargs={"operation": f"step-{number}"}) for number in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        console.close()
        output = stream.getvalue()
        for number in range(8):
            self.assertEqual(output.count(f"operation=step-{number}"), 1)
        self.assertEqual(output.count("DONE"), 8)

    def test_close_cleans_live_line_after_failures_and_interrupts(self):
        for failure in (RuntimeError("failure"), KeyboardInterrupt()):
            stream = TTYBuffer()
            console = relay_console.Console(stream, interval=3600)
            try:
                console.update("working")
                raise failure
            except (RuntimeError, KeyboardInterrupt):
                pass
            finally:
                console.close()
            self.assertRegex(stream.getvalue(), r"\r +\r$")


class ContractTests(unittest.TestCase):
    def task(self, task_id="TASK-0001", dependencies=None):
        return {
            "id": task_id, "title": "Do the thing", "status": "ready", "priority": "P1",
            "dependencies": dependencies or [], "allowedPaths": ["src"],
            "acceptanceCriteria": ["It works."], "validationCommands": ["python -m unittest"],
        }

    def backlog_bug(self, bug_id="BUG-0001"):
        return {
            "id": bug_id, "title": "Repair defect", "severity": "P1", "status": "backlog",
            "source": "TASK-0009", "sourceFindingId": "finding-1", "location": "src/app.py:12",
            "failure": "input fails", "reproduction": "python -m unittest tests.test_app",
            "requirement": "input works", "evidence": "exit 1", "deferralReason": "next campaign",
            "allowedPaths": ["src/app.py"],
        }

    def backlog_task(self, source_ref="origin/BUG-0001"):
        task = self.task()
        task.update(
            allowedPaths=["src/app.py", "tests/test_app.py"], sourceRef=source_ref,
            testPaths=["tests/test_app.py"],
            regressionValidationCommands=["python -m unittest tests.test_app"],
            validationCommands=["python -m unittest tests.test_app"],
        )
        return task

    def test_plan_round_trip(self):
        text = plan.render_tasks([self.task()], "0123456789abcdef", "abc123")
        metadata, tasks = run.parse_tasks(text)
        self.assertEqual(metadata["baseSha"], "0123456789abcdef")
        self.assertEqual(tasks[0]["id"], "TASK-0001")

    def test_campaign_validation_round_trip_and_digest(self):
        commands = ["python -m unittest", "python -m compileall ."]
        text = plan.render_tasks([self.task()], "0123456789abcdef", "abc123", campaign_validation_commands=commands)
        metadata, tasks = run.parse_tasks(text)
        self.assertEqual(metadata["campaignValidationCommands"], commands)
        self.assertFalse(metadata["legacyCampaignValidation"])
        self.assertNotEqual(plan.plan_digest(tasks, commands), plan.plan_digest(tasks, commands[::-1]))
        with self.assertRaises(ValueError):
            plan.render_tasks(tasks, metadata["baseSha"], metadata["requirementsHash"], campaign_validation_commands=[])

    def test_seed_metadata_round_trip_and_digest(self):
        task = self.task()
        task["seed"] = {"originalBaseSha": "0123456789abcdef", "candidateSha": "fedcba9876543210", "archiveRef": "relay/archive/campaign/TASK-0001", "summary": "preserved work"}
        text = plan.render_tasks([task], "1111111111111111", "abc123", campaign_validation_commands=["full build"])
        _, parsed = run.parse_tasks(text)
        self.assertEqual(parsed[0]["seed"], task["seed"])
        self.assertNotEqual(plan.plan_digest([task], ["full build"]), plan.plan_digest([{key: value for key, value in task.items() if key != "seed"}], ["full build"]))

    def test_backlog_parser_preserves_owned_qualified_entries(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            parsed = plan.parse_backlog(run.render_backlog("origin", root, [self.backlog_bug()]), root)
            self.assertEqual(parsed["campaignId"], "origin")
            self.assertEqual(parsed["entries"][0]["sourceRef"], "origin/BUG-0001")
            self.assertEqual(parsed["entries"][0]["allowedPaths"], ["src/app.py"])
            self.assertIsNone(plan.parse_backlog("# User requirements\n", root))

    def test_backlog_parser_rejects_malformed_or_unsafe_contracts(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            valid = run.render_backlog("origin", root, [self.backlog_bug()])
            cases = {
                "marker": valid.replace("<!-- relay: backlog", "<!-- not-relay: backlog"),
                "repository": valid.replace(hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12], "0" * 12),
                "missing": valid.replace("- Evidence: exit 1\n", ""),
                "unsafe": valid.replace("`src/app.py`", "`../app.py`"),
                "empty": run.render_backlog("origin", root, []),
                "duplicate": valid + valid[valid.index("## BUG-0001"):],
            }
            for label, text in cases.items():
                with self.subTest(label=label), self.assertRaises(ValueError):
                    plan.parse_backlog(text, root)

    def test_backlog_plan_requires_exact_mapping_and_regression_contract(self):
        entries = [{**self.backlog_bug(), "sourceRef": "origin/BUG-0001"}]
        valid = {"campaignValidationCommands": ["python -m unittest"], "tasks": [self.backlog_task()]}
        self.assertEqual(plan.validate_plan(valid, entries), valid)
        variants = []
        for change in (
            {"sourceRef": "origin/BUG-9999"}, {"testPaths": []},
            {"allowedPaths": ["src/app.py"]}, {"regressionValidationCommands": ["missing command"]},
        ):
            variants.append(valid | {"tasks": [self.backlog_task() | change]})
        variants += [valid | {"tasks": []}, valid | {"tasks": [self.backlog_task(), self.backlog_task("origin/BUG-0001")] }]
        for value in variants:
            with self.assertRaises(ValueError):
                plan.validate_plan(value, entries)

    def test_provenance_render_parse_and_digest_round_trip(self):
        task = self.backlog_task()
        commands = ["python -m unittest"]
        text = plan.render_tasks([task], "0123456", "abc123", campaign_validation_commands=commands)
        _, parsed = run.parse_tasks(text)
        for key in ("sourceRef", "testPaths", "regressionValidationCommands"):
            self.assertEqual(parsed[0][key], task[key])
        self.assertNotEqual(plan.plan_digest([task], commands), plan.plan_digest([{key: value for key, value in task.items() if key != "sourceRef"}], commands))

    def test_planning_result_requires_campaign_validation(self):
        with self.assertRaisesRegex(ValueError, "campaign validation"):
            plan.validate_plan({"campaignValidationCommands": [], "tasks": [self.task()]})
        value = {"campaignValidationCommands": ["python -m unittest"], "tasks": [self.task()]}
        self.assertEqual(plan.validate_plan(value), value)

    def test_bug_ledger_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            bug = {"id": "BUG-0001", "title": "Broken", "severity": "P1", "status": "active", "source": "audit", "sourceFindingId": "AUDIT-F1", "location": "x.py:1", "failure": "fails", "reproduction": "python x.py", "requirement": "works", "evidence": "exit 1"}
            text = run.render_bugs("campaign", Path(root), [bug])
            metadata, bugs = run.parse_bugs(text)
            self.assertEqual(metadata["campaignId"], "campaign")
            self.assertEqual(bugs[0]["reproduction"], "python x.py")
            self.assertEqual(bugs[0]["sourceFindingId"], "AUDIT-F1")

    def test_finding_line_ranges_are_not_part_of_allowed_path(self):
        with tempfile.TemporaryDirectory() as root:
            bug = {"id": "BUG-0001", "title": "Broken", "severity": "P1", "status": "active", "source": "TASK-0001", "location": "src/run.ps1:27-34,60-70", "failure": "fails", "reproduction": "test", "requirement": "works", "evidence": "failure"}
            _, bugs = run.parse_bugs(run.render_bugs("campaign", Path(root), [bug]))
            self.assertEqual(bugs[0]["allowedPaths"], ["src/run.ps1"])

    def test_invalid_dependency_rejected(self):
        text = plan.render_tasks([self.task(dependencies=["TASK-9999"])], "0123456", "abc123")
        with self.assertRaises(ValueError):
            run.parse_tasks(text)

    def test_cyclic_dependency_and_path_escape_rejected(self):
        first, second = self.task("TASK-0001", ["TASK-0002"]), self.task("TASK-0002", ["TASK-0001"])
        with self.assertRaises(ValueError):
            run.parse_tasks(plan.render_tasks([first, second], "0123456", "abc123"))
        escaped = self.task(); escaped["allowedPaths"] = ["../outside"]
        with self.assertRaises(ValueError):
            run.parse_tasks(plan.render_tasks([escaped], "0123456", "abc123"))

    def test_backward_review_transitions_rejected(self):
        forbidden = [("verify-1", "initial-review"), ("verify-1", "triage"), ("repair-1", "initial-review"), ("approved", "initial-review"), ("needs-user", "repair-2")]
        for source, target in forbidden:
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                run.transition_review({"phase": source}, target, 2)

    def test_forward_review_transitions(self):
        session = {"phase": "initial-review"}
        for phase in ("triage", "repair-1", "verify-1", "repair-2", "verify-2", "approved"):
            run.transition_review(session, phase, 2)
        self.assertEqual(session["phase"], "approved")

    def test_pre_review_repair_advances_the_shared_fix_sequence(self):
        session = {"phase": "triage", "repairAttemptsStarted": 1}
        with self.assertRaises(ValueError):
            run.transition_review(session, "repair-1", 2)
        run.transition_review(session, "repair-2", 2)
        self.assertEqual(session["phase"], "repair-2")

    def test_worker_cannot_change_mode(self):
        value = {"mode": "repair", "assignmentId": "TASK-0001", "status": "candidate", "candidateSha": "abc", "validation": [], "summary": ""}
        with self.assertRaises(ValueError):
            run.validate_agent_result("worker", value, "TASK-0001", "task")
        value["mode"], value["status"] = "task", "completed"
        with self.assertRaises(ValueError):
            run.validate_agent_result("worker", value, "TASK-0001", "task")

    def test_review_budget_formula(self):
        self.assertEqual(run.review_call_limit(2, 2), 9)
        self.assertNotIn("repair-1", run.legal_review_targets("triage", 0))

    def test_positive_deadlines_cannot_be_disabled(self):
        with self.assertRaises(SystemExit):
            plan.parser().parse_args(["--repo", ".", "--requirements", "PLAN.md", "--agent-timeout", "0"])
        with self.assertRaises(SystemExit):
            run.parser().parse_args(["--repo", ".", "--agent-timeout", "0"])
        for option in ("--workers", "--task-attempts", "--validation-timeout", "--provider-timeout", "--provider-check-timeout", "--provider-attempts"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                run.parser().parse_args(["--repo", ".", option, "0"])
        with self.assertRaises(SystemExit):
            repo.parser().parse_args(["--path", "x", "--provider-timeout", "0"])

    def test_tool_commands_resolve_windows_shims(self):
        builders = ((plan, plan.command), (run, run.tool_command), (repo, repo._command))
        cases = (
            ("codex", "nt", r"C:\Tools\codex.CMD", [r"C:\Tools\codex.CMD", "--flag"], True),
            ("gh", "nt", r"C:\Tools\gh.CMD", [r"C:\Tools\gh.CMD", "--flag"], True),
            ("az", "nt", r"C:\Tools\az.CMD", [r"C:\Tools\az.CMD", "--flag"], True),
            ("git", "nt", r"C:\Tools\git.CMD", [r"C:\Tools\git.CMD", "--flag"], True),
            ("missing", "nt", None, ["missing", "--flag"], True),
            ("codex", "posix", r"C:\Tools\codex.CMD", ["codex", "--flag"], False),
        )
        for module, builder in builders:
            for tool, platform, resolved, expected, looked_up in cases:
                with self.subTest(builder=builder.__name__, tool=tool, platform=platform):
                    with patch.object(module.os, "name", platform), patch.object(module.shutil, "which", return_value=resolved) as which, patch.dict(os.environ, {f"RELAY_{tool.upper()}": f"{tool} --flag"}):
                        self.assertEqual(builder(tool), expected)
                    self.assertEqual(which.called, looked_up)


class RepositoryTests(unittest.TestCase):
    def completed(self, stdout=""):
        return subprocess.CompletedProcess([], 0, stdout, "")

    def test_creates_local_repository(self):
        with tempfile.TemporaryDirectory() as root, patch("repo.run") as command:
            command.side_effect = [self.completed(), self.completed(), self.completed(), self.completed("main\n"), self.completed("abcdef\n")]
            path, branch, sha = repo.create(Path(root) / "demo")
            self.assertEqual((path / "README.md").read_text(encoding="utf-8"), "# demo\n")
            self.assertEqual((path / "AGENTS.md").read_bytes(), repo.TARGET_AGENTS.encode())
            self.assertEqual((branch, sha), ("main", "abcdef"))
            self.assertEqual(command.call_args_list[0].args[:4], ("git", "-C", str(path), "init"))
            self.assertEqual(command.call_args_list[1].args[-2:], ("README.md", "AGENTS.md"))

    def test_exclusive_create_preserves_existing_entry_and_uses_lf(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "AGENTS.md"
            self.assertTrue(repo.create_exclusive(path, "one\ntwo\n"))
            self.assertEqual(path.read_bytes(), b"one\ntwo\n")
            self.assertFalse(repo.create_exclusive(path, "replacement\n"))
            self.assertEqual(path.read_bytes(), b"one\ntwo\n")
            directory = Path(root) / "occupied"; directory.mkdir()
            self.assertFalse(repo.create_exclusive(directory, "replacement\n"))

    def test_refuses_nonempty_path_without_running_tools(self):
        with tempfile.TemporaryDirectory() as root, patch("repo.run") as command:
            target = Path(root) / "demo"
            target.mkdir()
            (target / "mine.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(ValueError):
                repo.create(target)
            command.assert_not_called()
            self.assertEqual((target / "mine.txt").read_text(encoding="utf-8"), "keep")

    def test_github_failure_preserves_local_repository(self):
        with tempfile.TemporaryDirectory() as root, patch("repo.run") as command:
            command.side_effect = [self.completed(), self.completed(), self.completed(), self.completed("main\n"), self.completed("abcdef\n"), self.completed(), subprocess.CalledProcessError(1, "gh")]
            target = Path(root) / "demo"
            with self.assertRaises(subprocess.CalledProcessError):
                repo.create(target, "owner/demo", "private")
            self.assertTrue((target / "README.md").is_file())

    def test_creates_azure_repository_adds_remote_and_pushes(self):
        with tempfile.TemporaryDirectory() as root, patch("repo.run") as command:
            created = json.dumps({"remoteUrl": "https://dev.azure.com/org/project/_git/demo"})
            command.side_effect = [self.completed(), self.completed(), self.completed(), self.completed("main\n"), self.completed("abcdef\n"), self.completed(created), self.completed(), self.completed()]
            target = Path(root) / "demo"
            repo.create(target, timeout=10, azure_devops=("org", "project", "demo"))
            self.assertEqual(command.call_args_list[5].args, ("az", "repos", "create", "--name", "demo", "--organization", "https://dev.azure.com/org", "--project", "project", "--output", "json"))
            self.assertEqual(command.call_args_list[6].args[-2:], ("origin", "https://dev.azure.com/org/project/_git/demo"))
            self.assertEqual(command.call_args_list[7].args[-3:], ("--set-upstream", "origin", "main"))

    def test_azure_publish_failure_preserves_local_repository(self):
        with tempfile.TemporaryDirectory() as root, patch("repo.run") as command:
            created = json.dumps({"remoteUrl": "https://dev.azure.com/org/project/_git/demo"})
            command.side_effect = [self.completed(), self.completed(), self.completed(), self.completed("main\n"), self.completed("abcdef\n"), self.completed(created), self.completed(), subprocess.CalledProcessError(1, "git")]
            target = Path(root) / "demo"
            with self.assertRaises(subprocess.CalledProcessError):
                repo.create(target, azure_devops=("org", "project", "demo"))
            self.assertTrue((target / "README.md").is_file())

    def test_visibility_is_required_for_github(self):
        with self.assertRaises(SystemExit):
            repo.parser().parse_args(["--path", "x", "--github", "owner/x", "--private", "--public"])
        with self.assertRaises(SystemExit):
            repo.parser().parse_args(["--path", "x", "--github", "owner/x", "--azure-devops", "org", "project", "repo"])
        with self.assertRaises(SystemExit):
            repo.main(["--path", "x", "--azure-devops", "org", "project", "repo", "--private"])

    def test_refuses_existing_git_repository(self):
        with tempfile.TemporaryDirectory() as root, patch("repo.run") as command:
            target = Path(root) / "demo"; (target / ".git").mkdir(parents=True)
            with self.assertRaises(ValueError):
                repo.create(target)
            command.assert_not_called()

    def test_real_git_creates_disposable_local_repository(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Relay Test", "GIT_AUTHOR_EMAIL": "relay@example.invalid", "GIT_COMMITTER_NAME": "Relay Test", "GIT_COMMITTER_EMAIL": "relay@example.invalid"}):
            target, branch, sha = repo.create(Path(root) / "demo")
            self.assertTrue((target / ".git").is_dir())
            self.assertEqual(branch, "main")
            self.assertEqual(git_output(target, "rev-parse", "HEAD").strip(), sha)
            self.assertEqual(git_output(target, "ls-tree", "--name-only", "HEAD").splitlines(), ["AGENTS.md", "README.md"])

    def test_no_runtime_dependency_on_tools_copy(self):
        for script in (repo, plan, run, status):
            self.assertNotIn("Projects\\Tools", Path(script.__file__).read_text(encoding="utf-8"))


class PlanningTests(unittest.TestCase):
    def test_fixed_scout_assignments(self):
        files = [f"area{i}/file{n}.py" for i in range(4) for n in range(4)]
        self.assertEqual(plan.scout_scopes(files, 3), ["area0, area3", "area1", "area2"])

    def test_planning_call_budget_is_hard(self):
        budget = plan.CallBudget(2)
        self.assertEqual([budget.consume(), budget.consume()], [1, 2])
        with self.assertRaises(RuntimeError):
            budget.consume()

    def test_small_repository_skips_scouts(self):
        self.assertEqual(plan.scout_scopes(["README.md"], 3), [])

    def test_scout_cannot_widen_fixed_scope(self):
        value = {"scope": "elsewhere", "implemented": [], "missing": [], "conflicts": [], "relevantPaths": [], "validationCommands": [], "evidence": []}
        with self.assertRaisesRegex(ValueError, "scout scope mismatch"):
            plan.validate_scout(value, "src")
        value["scope"], value["evidence"] = "src", [1]
        with self.assertRaisesRegex(ValueError, "scout invalid field: evidence"):
            plan.validate_scout(value, "src")
        value["evidence"], value["relevantPaths"] = [], ["src/file.py"]
        self.assertIs(plan.validate_scout(value, "src"), value)
        value["scope"], value["relevantPaths"] = "src", ["tests/test_other.py"]
        with self.assertRaisesRegex(ValueError, "scout out-of-scope path"):
            plan.validate_scout(value, "src")

    def test_scout_schema_fixes_assigned_scope(self):
        self.assertEqual(plan.scout_schema("src, tests")["properties"]["scope"]["enum"], ["src, tests"])

    def test_validated_attempt_events_and_budget_are_bounded(self):
        valid = {"scope": "src", "implemented": [], "missing": [], "conflicts": [], "relevantPaths": [], "validationCommands": [], "evidence": []}
        invalid = valid | {"scope": "elsewhere"}
        events = []
        budget = plan.CallBudget(2)
        with patch("plan.invoke_agent", side_effect=[invalid, valid]), patch("plan.progress", side_effect=lambda event, detail: events.append((event, detail))):
            result = plan.invoke_validated(Path("."), "prompt", plan.scout_schema("src"), lambda value: plan.validate_scout(value, "src"), 10, budget, 1, "role=scout slot=2")
        self.assertIs(result, valid)
        self.assertEqual([event for event, _ in events], ["START", "RETRY", "START", "DONE"])
        self.assertIn("attempt=1/2 call=1/2 timeout=10s", events[0][1])
        self.assertIn("attempt=2/2 call=2/2 timeout=10s", events[2][1])
        self.assertEqual(budget.started, 2)

    def test_backlog_planner_stops_when_no_test_contract_is_returned(self):
        entry = {**ContractTests().backlog_bug(), "sourceRef": "origin/BUG-0001"}
        invalid = {"campaignValidationCommands": ["python -m unittest"], "tasks": [ContractTests().task() | {"sourceRef": "origin/BUG-0001", "testPaths": [], "regressionValidationCommands": []}]}
        budget = plan.CallBudget(2)
        with patch("plan.invoke_agent", return_value=invalid), patch("plan.progress"), self.assertRaisesRegex(RuntimeError, "valid structured output"):
            plan.invoke_validated(Path("."), "prompt", plan.planning_schema(True), lambda value: plan.validate_plan(value, [entry]), 10, budget, 1)
        self.assertEqual(budget.started, 2)

    def test_plan_review_accepts_after_one_review_and_one_audit(self):
        tasks = [ContractTests().task()]
        digest = plan.plan_digest(tasks)
        results = [
            {"assignmentId": "PLAN", "candidateSha": digest, "findings": []},
            {"assignmentId": "PLAN", "candidateSha": digest, "findings": []},
        ]
        with patch("plan.invoke_validated", side_effect=results) as invoke:
            self.assertIs(plan.reviewed_plan(Path("."), "requirements", "instructions", [], "base", tasks, 10, plan.CallBudget(7), 2), tasks)
        self.assertEqual(invoke.call_count, 2)
        self.assertIn("Role: contract-reviewer", invoke.call_args_list[0].args[1])
        self.assertIn("Role: risk-reviewer", invoke.call_args_list[1].args[1])

    def test_plan_findings_get_one_repair_and_scoped_verification(self):
        tasks = [ContractTests().task()]
        revised = [tasks[0] | {"validationCommands": ["fixed command"]}]
        finding = {"id": "plan-command", "severity": "P1", "location": "TASK-0001 Validation", "failure": "command is invalid", "reproduction": "bad command", "requirement": "validation must run", "evidence": "unsupported syntax", "candidateIntroduced": True}
        results = [
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "findings": [finding]},
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "findings": []},
            revised,
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(revised), "status": "resolved"},
        ]
        with patch("plan.invoke_validated", side_effect=results) as invoke:
            self.assertEqual(plan.reviewed_plan(Path("."), "requirements", "instructions", [], "base", tasks, 10, plan.CallBudget(7), 2), revised)
        self.assertEqual(invoke.call_count, 4)
        self.assertIn("Repair only the supplied findings", invoke.call_args_list[2].args[1])
        self.assertIn("Verify only that every supplied finding", invoke.call_args_list[3].args[1])

    def test_unresolved_plan_repair_stops_without_another_loop(self):
        tasks = [ContractTests().task()]
        finding = {"id": "plan-command", "severity": "P1", "location": "TASK-0001 Validation", "failure": "command is invalid", "reproduction": "bad command", "requirement": "validation must run", "evidence": "unsupported syntax", "candidateIntroduced": True}
        results = [
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "findings": [finding]},
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "findings": []},
            tasks,
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "status": "unresolved"},
        ]
        with patch("plan.invoke_validated", side_effect=results) as invoke, self.assertRaisesRegex(RuntimeError, "verification unresolved"):
            plan.reviewed_plan(Path("."), "requirements", "instructions", [], "base", tasks, 10, plan.CallBudget(7), 2)
        self.assertEqual(invoke.call_count, 4)

    def test_delayed_agent_keeps_wait_live_while_concurrent_agent_completes(self):
        result = {"scope": "src", "implemented": [], "missing": [], "conflicts": [], "relevantPaths": [], "validationCommands": [], "evidence": []}
        events, lock = [], threading.Lock()

        def fake_run(invocation, *, input, **kwargs):
            time.sleep(.05 if input == "slow" else .002)
            Path(invocation[invocation.index("--output-last-message") + 1]).write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(invocation, 0, "raw stdout", "raw stderr")

        def record(event, detail):
            with lock:
                events.append((event, detail))

        budget = plan.CallBudget(2)
        with tempfile.TemporaryDirectory() as root, patch("plan.subprocess.run", side_effect=fake_run), patch("plan.progress", side_effect=record):
            with plan.ThreadPoolExecutor(max_workers=2) as pool:
                slow = pool.submit(plan.invoke_validated, Path(root), "slow", plan.scout_schema("src"), lambda value: plan.validate_scout(value, "src"), 10, budget, 0, "role=scout slot=1")
                fast = pool.submit(plan.invoke_validated, Path(root), "fast", plan.scout_schema("src"), lambda value: plan.validate_scout(value, "src"), 10, budget, 0, "role=scout slot=2")
                self.assertEqual((slow.result(), fast.result()), (result, result))
        self.assertFalse(any(event == "WAIT" for event, _ in events))
        self.assertTrue(any(event == "DONE" and "slot=2" in detail for event, detail in events))

    def test_hung_planning_call_consumes_budget(self):
        with tempfile.TemporaryDirectory() as root, patch("plan.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", .01)):
            budget = plan.CallBudget(1)
            with self.assertRaises(subprocess.TimeoutExpired):
                plan.invoke_agent(Path(root), "prompt", {"type": "object"}, .01, budget)
            self.assertEqual(budget.started, 1)

    def test_only_gitless_scouts_skip_codex_repo_check(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot, repository = Path(root) / "snapshot", Path(root) / "repository"
            snapshot.mkdir(); (repository / ".git").mkdir(parents=True)
            subdirectory = repository / "subdirectory"; subdirectory.mkdir()
            for directory, skipped in ((snapshot, True), (subdirectory, False)):
                with self.subTest(directory=directory.name), patch("plan.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", .01)) as command, self.assertRaises(subprocess.TimeoutExpired):
                    plan.invoke_agent(directory, "prompt", {"type": "object"}, .01, plan.CallBudget(1))
                self.assertEqual("--skip-git-repo-check" in command.call_args.args[0], skipped)

    def test_real_plan_file_to_dry_run_needs_no_pipe(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = make_git_repository(root)
            requirements = root / "requirements.md"
            requirements.write_text("Add one file.", encoding="utf-8")
            fake = root / "fake_codex.py"
            fake.write_text(FAKE_CODEX, encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {"RELAY_CODEX": f"{sys.executable} {fake}"}
            planned = subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements), "--workers", "2"], capture_output=True, text=True, env=environment, check=True)
            plan_path = target / "PLAN.md"
            self.assertTrue(Path(planned.stdout.strip()).samefile(plan_path))
            self.assertTrue(plan_path.read_text(encoding="utf-8").startswith("# Tasks\n"))
            self.assertNotIn(b"\r\n", plan_path.read_bytes())
            self.assertEqual((target / "AGENTS.md").read_bytes(), repo.TARGET_AGENTS.encode())
            self.assertIn("Relay Planner", planned.stderr)
            self.assertIn("SUMMARY    campaign-validation=1", planned.stderr)
            self.assertIn("SUMMARY    - TASK-0001 ready: Add one file", planned.stderr)
            self.assertIn("NEXT       Review the generated plan, then execute it with:", planned.stderr)
            self.assertIn("--workers 2 --task-attempts 3 --fix-loops 2", planned.stderr)
            dry = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--dry-run"], capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertFalse((target / ".relay").exists())

    def test_nontrivial_scopes_overlap_and_pm_receives_all_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            for area in ("area1", "area2", "area3"):
                for index in range(4):
                    path = target / area / f"{index}.txt"; path.parent.mkdir(exist_ok=True); path.write_text("x", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "."], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "areas"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(target), "push"], check=True, capture_output=True)
            requirements = root / "requirements.md"; requirements.write_text("Inspect the repository.", encoding="utf-8")
            fake, events = root / "fake_codex.py", root / "scout"
            fake.write_text(FAKE_CODEX, encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {"RELAY_CODEX": f"{sys.executable} {fake}", "FAKE_SCOUT_EVENTS": str(events), "FAKE_REQUIRE_EVIDENCE": "1"}
            before = git_output(target, "status", "--porcelain=v1", "--untracked-files=all")
            completed = subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements), "--workers", "2"], capture_output=True, text=True, env=environment, check=True)
            starts = [float(path.read_text()) for path in root.glob("scout.*.start")]
            ends = [float(path.read_text()) for path in root.glob("scout.*.end")]
            self.assertEqual((len(starts), len(ends)), (2, 2))
            self.assertLess(max(starts), min(ends))
            self.assertTrue(Path(completed.stdout.strip()).samefile(target / "PLAN.md"))
            self.assertTrue((target / "PLAN.md").read_text(encoding="utf-8").startswith("# Tasks"))
            self.assertEqual(git_output(target, "status", "--porcelain=v1", "--untracked-files=all"), "?? AGENTS.md\n?? PLAN.md\n")

    def test_existing_agents_is_supplied_and_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            agents = target / "AGENTS.md"; agents.write_bytes(b"custom\r\n")
            _, _, instructions = plan.inspect_repository(target)
            self.assertEqual(instructions, "custom\n")
            self.assertEqual(agents.read_bytes(), b"custom\r\n")

    def test_missing_agents_uses_default_in_memory(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            self.assertEqual(plan.inspect_repository(target)[2], repo.TARGET_AGENTS)
            self.assertFalse((target / "AGENTS.md").exists())

    def test_scout_snapshot_exposes_only_assigned_tracked_area(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); repo_path = root / "repo"; repo_path.mkdir()
            for relative in ("src/a.py", "tests/test_a.py"):
                path = repo_path / relative; path.parent.mkdir(); path.write_text(relative, encoding="utf-8")
            snapshot = plan.create_scout_snapshot(repo_path, ["src/a.py", "tests/test_a.py"], "src", root / "view")
            self.assertTrue((snapshot / "src" / "a.py").is_file())
            self.assertFalse((snapshot / "tests").exists())

    def test_configured_limits_are_rendered(self):
        text = plan.render_tasks([ContractTests().task()], "0123456", "abc123", task_attempts=5, fix_loops=1)
        metadata, _ = run.parse_tasks(text)
        self.assertEqual((metadata["taskAttemptLimit"], metadata["fixLoopLimit"]), (5, 1))

    def test_run_rejects_plan_limit_mismatch_before_creating_campaign(self):
        with tempfile.TemporaryDirectory() as root:
            args = run.parser().parse_args(["--repo", root, "--task-attempts", "3", "--fix-loops", "2"])
            text = plan.render_tasks([ContractTests().task()], "0123456", "abc123", task_attempts=4, fix_loops=1)
            with self.assertRaises(RuntimeError):
                run.initialize_campaign(Path(root), text, args)
            self.assertFalse((Path(root) / ".relay").exists())


class DeterministicCoreTests(unittest.TestCase):
    def state_store(self, root, fix_loops=2, format_retries=2):
        args = run.parser().parse_args(["--repo", str(root), "--fix-loops", str(fix_loops), "--format-retries", str(format_retries)])
        campaign_commands = ["python -c \"pass\""]
        state = run.initial_state(Path(root), {"baseSha": "0123456", "requirementsHash": "abc123", "campaignValidationCommands": campaign_commands, "legacyCampaignValidation": False}, args)
        state["campaignId"] = "test"
        path = Path(root) / ".relay" / "state.json"
        Path(root, "tasks.md").write_text(plan.render_tasks([ContractTests().task()], "0123456", "abc123", args.task_attempts, args.fix_loops, campaign_commands), encoding="utf-8")
        Path(root, "bugs.md").write_text(run.render_bugs("test", Path(root)), encoding="utf-8")
        return run.StateStore(path, state)

    def azure_store(self, root, merge_method="squash"):
        store = self.state_store(root)
        store.state.update(provider="azure-devops", azureOrganization="my org", azureProject="My Project", azureRepository="My Repo", mergeMethod=merge_method)
        return store

    def azure_pr(self, status="active", merge_status="succeeded", sha="abc"):
        return {"pullRequestId": 7, "status": status, "mergeStatus": merge_status, "lastMergeSourceCommit": {"commitId": sha}}

    def provider_metadata(self, store, assignment, sha, pr):
        title, _body, digest = run.canonical_pr_metadata(store.state, assignment, sha)
        task_state = store.state["taskStates"].setdefault(assignment["id"], {})
        task_state.update(candidateSha=sha, pushedSha=sha, pr=pr, publicationProof={"candidateSha": sha, "providerRecord": pr}, prMetadata={"hash": digest, "candidateSha": sha, "sourceRef": assignment.get("sourceRef"), "title": title})
        store.state["pullRequests"][assignment["id"]] = pr

    def test_validation_uses_explicit_platform_shells(self):
        command = "$items = @('one', 'two'); $items | ForEach-Object { $_ }"
        with patch.object(run.os, "name", "nt"), patch("run.tool_command", return_value=[r"C:\Tools\pwsh.exe"]):
            self.assertEqual(run.validation_command(command), [r"C:\Tools\pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command])
        with patch.object(run.os, "name", "posix"):
            self.assertEqual(run.validation_command(command), ["/bin/sh", "-c", command])

    def test_validation_subprocesses_get_unique_existing_temp_directories(self):
        seen = []
        def execute(command, **kwargs):
            paths = {kwargs["env"][name] for name in ("TEMP", "TMP", "TMPDIR")}
            self.assertEqual(len(paths), 1)
            temporary = Path(paths.pop())
            self.assertTrue(temporary.is_dir())
            seen.append(temporary)
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch("run.bounded_run", side_effect=execute):
            run.run_validation_command(["shell", "first"], timeout=1)
            run.run_validation_command(["shell", "second"], timeout=1)
        self.assertEqual(len(set(seen)), 2)
        self.assertTrue(all(not path.exists() for path in seen))

    def test_assignment_python_environments_are_scoped_shared_and_do_not_mutate_parent(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "implementing"}
            worktree, other_worktree = root / "active", root / "other"
            user_site, outside = root / "user-site", root / "outside"
            leaked = root / "relay-worktrees" / "other" / "TASK-9999"
            for path in (worktree, other_worktree, user_site, outside, leaked):
                path.mkdir(parents=True)
            original = os.environ.copy()
            captured = {}

            def agent(command, **kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                if "contract-reviewer" in output.name:
                    captured["reviewer"] = kwargs["env"]
                    result = {"assignmentId": assignment["id"], "candidateSha": "abc", "findings": []}
                else:
                    captured["worker"] = kwargs["env"]
                    result = {"mode": "task", "assignmentId": assignment["id"], "status": "candidate", "candidateSha": "abc", "validation": [], "summary": ""}
                output.write_text(json.dumps(result), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")

            def validation(_command, **kwargs):
                captured["validation"] = kwargs["env"]
                return subprocess.CompletedProcess([], 0, "", "")

            operator_path = os.pathsep.join((str(outside), str(user_site), str(leaked), str(outside), str(worktree / "src")))
            with patch("run.tempfile.gettempdir", return_value=str(root)), patch.object(run, "ORIGINAL_PYTHON_USER_SITE", str(user_site)), patch.dict(os.environ, {"PYTHONPATH": operator_path}, clear=False):
                with patch("run.bounded_run", side_effect=agent):
                    run.invoke_agent(store, threading.Semaphore(1), worktree, assignment["id"], "worker", "prompt", mode="task")
                    run.ensure_review_session(store, assignment["id"], "abc")
                    run.invoke_agent(store, threading.Semaphore(1), worktree, assignment["id"], "contract-reviewer", "prompt", review=True)
                with patch("run.validation_command", return_value=["shell"]), patch("run.run_validation_command", side_effect=validation):
                    run.run_validations(store, assignment, worktree, commands=["test"])
                other = run.assignment_environment(store, "TASK-0002", other_worktree)
                self.assertEqual(captured["worker"]["PYTHONUSERBASE"], captured["validation"]["PYTHONUSERBASE"])
                self.assertEqual(captured["worker"], captured["reviewer"])
                self.assertNotEqual(captured["worker"]["PYTHONUSERBASE"], other["PYTHONUSERBASE"])
                self.assertEqual(captured["worker"]["PYTHONPATH"].split(os.pathsep), [str(worktree.resolve() / "src"), str(user_site), str(outside)])
                self.assertEqual(other["PYTHONPATH"].split(os.pathsep)[0], str(other_worktree.resolve() / "src"))
                run.cleanup_assignment_environment(store, assignment["id"])
                self.assertFalse(Path(captured["worker"]["PYTHONUSERBASE"]).exists())
            self.assertEqual(os.environ, original)

    def test_original_user_site_is_importable_without_executing_editable_pth(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = self.state_store(root)
            user_site, editable = root / "user-site", root / "editable"
            user_site.mkdir(); editable.mkdir()
            (user_site / "operator_tool.py").write_text("VALUE = 7\n", encoding="utf-8")
            (editable / "sibling_assignment.py").write_text("LEAKED = True\n", encoding="utf-8")
            (user_site / "sibling-editable.pth").write_text(str(editable) + "\n", encoding="utf-8")
            with patch("run.tempfile.gettempdir", return_value=str(root)), patch.object(run, "ORIGINAL_PYTHON_USER_SITE", str(user_site)), patch.dict(os.environ, {"PYTHONPATH": ""}, clear=False):
                environment = run.assignment_environment(store, "TASK-0001", root)
                package = root / "src" / "active_assignment"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("VALUE = 9\n", encoding="utf-8")
                completed = run.run_validation_command([
                    sys.executable, "-c",
                    "import importlib.util, active_assignment, operator_tool; assert active_assignment.VALUE == 9; assert operator_tool.VALUE == 7; assert importlib.util.find_spec('sibling_assignment') is None",
                ], timeout=10, cwd=root, env=environment)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_all_coordinator_validation_paths_use_isolated_runner(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation"}
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch("run.validation_command", side_effect=lambda command: ["shell", command]), patch("run.run_validation_command", return_value=completed) as isolated:
                for category in ("task", "campaign", "audit"):
                    run.run_validations(store, assignment, Path(root), category, [category])
                baseline = store.state["baselineValidation"]
                run.run_validations(store, {"id": "BASELINE", "validationCommands": ["baseline"]}, Path(root), "baseline", ["baseline"], baseline)
            self.assertEqual(isolated.call_count, 4)
            for call in isolated.call_args_list:
                self.assertEqual(call.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0], str(Path(call.kwargs["cwd"]).resolve() / "src"))
            with patch("run.require_validation_shell"), patch("run.git", return_value=subprocess.CompletedProcess([], 0, "", "")), patch("run.run_validation_command", return_value=completed) as recovery:
                self.assertEqual(run._check_legacy_baseline(store, [{"command": "legacy"}]), [])
            recovery.assert_called_once()
            self.assertEqual(recovery.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0], str(Path(recovery.call_args.kwargs["cwd"]).resolve() / "src"))

    def test_unlaunchable_validation_shell_fails_preflight(self):
        with patch("run.validation_command", return_value=["missing-shell"]), patch("run.os.path.isfile", return_value=False), patch("run.shutil.which", return_value=None), patch("run.bounded_run") as launch, self.assertRaisesRegex(RuntimeError, "validation shell is unavailable"):
            run.require_validation_shell()
        launch.assert_not_called()

    def test_validation_failure_and_timeout_are_logged_after_counter_is_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation"}
            completed = subprocess.CompletedProcess([], 7, "standard output\n", "standard error\n")
            def failed(*args, **kwargs):
                persisted = json.loads(store.path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["validationCommandsStarted"][assignment["id"]], 1)
                self.assertEqual(persisted["taskStates"][assignment["id"]]["operation"], "validate")
                self.assertEqual(persisted["taskStates"][assignment["id"]]["validationCommand"], assignment["validationCommands"][0])
                return completed
            with patch("run.validation_command", return_value=["explicit-shell", assignment["validationCommands"][0]]), patch("run.bounded_run", side_effect=failed) as command, self.assertRaisesRegex(RuntimeError, r"command 1 exited with code 7; log:"):
                run.run_validations(store, assignment, Path(root))
            self.assertNotIn("shell", command.call_args.kwargs)
            log = Path(root) / ".relay" / "logs" / "TASK-0001-validation-1.log"
            self.assertIn("standard output\n\n--- stderr ---\nstandard error", log.read_text(encoding="utf-8"))

            timeout = subprocess.TimeoutExpired("validation", 1, output="before timeout\n", stderr="timeout error\n")
            with patch("run.validation_command", return_value=["explicit-shell", "command"]), patch("run.bounded_run", side_effect=timeout), self.assertRaisesRegex(RuntimeError, r"command 1 timed out after 1800s; log:"):
                run.run_validations(store, assignment, Path(root))
            self.assertEqual(store.state["validationCommandsStarted"][assignment["id"]], 2)
            timeout_log = Path(root) / ".relay" / "logs" / "TASK-0001-validation-2.log"
            self.assertIn("before timeout\n\n--- stderr ---\ntimeout error", timeout_log.read_text(encoding="utf-8"))

    def test_candidate_runs_focused_then_campaign_validation(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            store.state["campaignValidationCommands"] = ["python -m unittest"]
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation"}
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            calls = []
            with patch("run.run_validations", side_effect=lambda *args, **kwargs: calls.append((args[3], args[4] if len(args) > 4 else None))):
                self.assertEqual(run.validate_candidate(store, assignment, target, {"candidateSha": base}), base)
            self.assertEqual(calls, [("task", None), ("campaign", ["python -m unittest"])])

    def test_backlog_candidate_must_change_a_declared_test_path(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            (target / "src").mkdir(); (target / "tests").mkdir()
            (target / "src" / "app.py").write_text("old\n", encoding="utf-8")
            (target / "tests" / "test_app.py").write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "src/app.py", "tests/test_app.py"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "base"], check=True, capture_output=True)
            base = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().backlog_task()
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            (target / "src" / "app.py").write_text("fixed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "src/app.py"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "source only"], check=True, capture_output=True)
            source_only = git_output(target, "rev-parse", "HEAD").strip()
            with self.assertRaisesRegex(ValueError, "declared test path"):
                run.candidate_integrity(store, assignment, target, {"candidateSha": source_only})
            (target / "tests" / "test_app.py").write_text("regression\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "tests/test_app.py"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "regression"], check=True, capture_output=True)
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            self.assertEqual(run.candidate_integrity(store, assignment, target, {"candidateSha": candidate}), candidate)

    def test_repeated_candidate_validation_failure_trips_after_second_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskAttemptLimit"] = 4
            calls = []
            def worker(*args, **kwargs):
                store.state["attemptCounters"][assignment["id"]] = store.state["attemptCounters"].get(assignment["id"], 0) + 1
                calls.append(1)
                return {"candidateSha": "same", "summary": "still failing"}
            def validate(*args):
                store.state["taskStates"][assignment["id"]].update(
                    validationCandidateSha="same",
                    validationFailure={"category": "campaign", "commandHash": "hash", "outcome": "exit:1"},
                )
                raise RuntimeError("campaign validation command 1 exited with code 1; log: changing.log")
            with patch("run.create_worktree", return_value=(Path(root), "branch")), patch("run.invoke_with_replacements", side_effect=worker), patch("run.validate_candidate", side_effect=validate):
                self.assertFalse(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            self.assertEqual((len(calls), store.state["attemptCounters"][assignment["id"]]), (2, 2))
            self.assertTrue(store.state["taskStates"][assignment["id"]]["validationCircuitBroken"])

    def test_baseline_failure_is_bounded_and_consumes_no_worker_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            store = self.state_store(target)
            base = git_output(target, "rev-parse", "HEAD").strip()
            commands = ["raise SystemExit(7)"]
            store.state.update(baseSha=base, repositoryRoot=str(target), repositoryPrefix="", campaignValidationCommands=commands)
            store.state["baselineValidation"] = {
                "baseSha": base, "commandsHash": run.commands_hash(commands), "phase": "pending",
                "commandsStarted": 0, "currentCommand": None, "startedAt": None, "deadline": None,
                "completedAt": None, "error": None, "log": None,
            }
            with patch("run.validation_command", side_effect=lambda command: [sys.executable, "-c", command]):
                self.assertFalse(run.run_baseline_validation(store))
            self.assertEqual(store.state["baselineValidation"]["phase"], "blocked")
            self.assertEqual(store.state["attemptCounters"], {})
            self.assertNotIn("BASELINE", store.state["worktrees"])

    def test_baseline_success_is_cached_for_matching_base_and_commands(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            store = self.state_store(target)
            base = git_output(target, "rev-parse", "HEAD").strip()
            commands = ["import pathlib; assert pathlib.Path('README.md').is_file()"]
            store.state.update(baseSha=base, repositoryRoot=str(target), repositoryPrefix="", campaignValidationCommands=commands)
            store.state["baselineValidation"] = {
                "baseSha": base, "commandsHash": run.commands_hash(commands), "phase": "pending",
                "commandsStarted": 0, "currentCommand": None, "startedAt": None, "deadline": None,
                "completedAt": None, "error": None, "log": None,
            }
            with patch("run.validation_command", side_effect=lambda command: [sys.executable, "-c", command]):
                self.assertTrue(run.run_baseline_validation(store))
            self.assertEqual(store.state["baselineValidation"]["phase"], "passed")
            self.assertEqual(store.state["baselineValidation"]["commandsStarted"], 1)
            with patch("run.git", side_effect=AssertionError("cached baseline reran")):
                self.assertTrue(run.run_baseline_validation(store))

    def test_terminal_baseline_replays_once_then_requires_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state.update(phase="needs-user")
            store.state["baselineValidation"].update(phase="blocked", error="failed", currentCommand="full build")
            store.save()
            run.reconcile(store)
            self.assertEqual(store.state["baselineValidation"]["phase"], "pending")
            self.assertTrue(store.state["baselineValidation"]["automaticReplayUsed"])
            store.state.update(phase="needs-user")
            store.state["baselineValidation"].update(phase="blocked", error="failed again", currentCommand="full build")
            store.save()
            run.reconcile(store)
            self.assertEqual(store.state["baselineValidation"]["phase"], "blocked")
            actions = run.plan_recovery(store, run.load_tasks(store), [], [])
            self.assertEqual(actions[0]["action"], "replay-baseline")
            run.apply_recovery(store, run.load_tasks(store), actions)
            self.assertEqual((store.state["phase"], store.state["baselineValidation"]["phase"]), ("build", "pending"))

    def test_backlog_snapshot_is_stable_and_user_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            bugs = [
                {"id": "BUG-0002", "title": "Two", "severity": "P3", "status": "backlog", "source": "audit", "sourceFindingId": "two", "location": "b.py:2", "failure": "two fails", "reproduction": "python b.py", "requirement": "two works", "evidence": "two", "allowedPaths": ["b.py"], "deferralReason": "later"},
                {"id": "BUG-0001", "title": "One", "severity": "P1", "status": "resolved", "source": "audit", "sourceFindingId": "one", "location": "a.py:1", "failure": "one fails", "reproduction": "python a.py", "requirement": "one works", "evidence": "one", "allowedPaths": ["a.py"]},
            ]
            path = run.publish_backlog(store, bugs)
            first = path.read_bytes()
            self.assertLess(first.index(b"BUG-0002"), len(first))
            self.assertNotIn(b"BUG-0001", first)
            run.publish_backlog(store, bugs)
            self.assertEqual(path.read_bytes(), first)
            path.write_text("# Mine\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "user-owned"):
                run.publish_backlog(store, bugs)
            self.assertEqual(path.read_text(encoding="utf-8"), "# Mine\n")

    def test_empty_new_backlog_does_not_delete_existing_owned_backlog(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            path = Path(root) / "BACKLOG.md"
            original = run.render_backlog("older", Path(root), [ContractTests().backlog_bug()])
            path.write_text(original, encoding="utf-8")
            self.assertIsNone(run.publish_backlog(store, []))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_campaign_summary_orders_tasks_and_backlog(self):
        tasks = [ContractTests().task("TASK-0002"), ContractTests().task("TASK-0001")]
        state = {
            "repository": str(Path("repo").resolve()), "phase": "complete",
            "campaignValidationCommands": ["test"], "baselineValidation": {"phase": "passed", "baseSha": "abc"},
            "taskStates": {
                "TASK-0001": {"phase": "integrated", "candidateSha": "one", "workerSummary": "implemented one"},
                "TASK-0002": {"phase": "integrated", "candidateSha": "two", "workerSummary": "implemented two"},
            },
        }
        bug = {"id": "BUG-0002", "status": "backlog", "failure": "formatting remains", "deferralReason": "outside scope"}
        lines, next_line = run.campaign_summary(state, tasks, [bug])
        self.assertLess(next(index for index, line in enumerate(lines) if "TASK-0001" in line), next(index for index, line in enumerate(lines) if "TASK-0002" in line))
        self.assertIn("backlog=1", lines[0])
        self.assertIn("BACKLOG.md", next_line)
        self.assertIn("then confirm with:", next_line)
        self.assertIn("--cleanup --confirm", next_line)

    def test_campaign_validation_recovery_next_command_confirms_attempt_grant(self):
        task = ContractTests().task()
        state = {
            "repository": str(Path("repo").resolve()), "phase": "needs-user",
            "campaignValidationCommands": ["build"],
            "taskStates": {task["id"]: {
                "phase": "needs-user",
                "validationFailure": {"category": "campaign", "command": "build", "commandHash": "hash", "outcome": "exit:1"},
                "validationLog": "validation.log",
            }},
        }
        _lines, next_line = run.campaign_summary(state, [task], [])
        self.assertIn("--recover --grant-attempt TASK-0001 --confirm", next_line)

    def test_schema_two_legacy_campaign_refuses_execution_without_baseline(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state.pop("campaignValidationCommands")
            store.state.pop("baselineValidation")
            store.save()
            legacy = plan.render_tasks([ContractTests().task()], "0123456", "abc123")
            Path(root, "tasks.md").write_text(legacy, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "legacy campaigns require --recover"):
                run.reconcile(store)
            with patch("run.provider_preflight") as provider, self.assertRaisesRegex(RuntimeError, "legacy campaigns require --recover"):
                run.execute_campaign(store, [])
            provider.assert_not_called()

    def test_initial_validation_failure_uses_next_task_attempt_and_keeps_last_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["taskAttemptLimit"] = 2
            assignment = ContractTests().task()
            prompts = []
            def worker(*args, **kwargs):
                prompts.append(args[5])
                store.state["attemptCounters"][assignment["id"]] = store.state["attemptCounters"].get(assignment["id"], 0) + 1
                return {"status": "candidate", "candidateSha": "candidate"}
            with patch("run.create_worktree", return_value=(Path(root), "branch")), patch("run.invoke_with_replacements", side_effect=worker), patch("run.validate_candidate", side_effect=[RuntimeError("validation command 1 exited with code 1; log: first.log"), RuntimeError("validation command 1 exited with code 2; log: second.log")]):
                self.assertFalse(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            self.assertEqual(store.state["attemptCounters"][assignment["id"]], 2)
            self.assertIn("first.log", prompts[1])
            self.assertEqual(store.state["taskStates"][assignment["id"]]["error"], "validation command 1 exited with code 2; log: second.log")

    def test_validation_circuit_routes_to_automatic_repair_before_initial_review(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["baselineValidation"]["phase"] = "passed"
            assignment = ContractTests().task()
            attempts = []
            def worker(*args, **kwargs):
                store.state["attemptCounters"][assignment["id"]] = store.state["attemptCounters"].get(assignment["id"], 0) + 1
                attempts.append(1)
                return {"candidateSha": "same"}
            def validate(*args):
                store.state["taskStates"][assignment["id"]].update(
                    validationCandidateSha="same",
                    validationFailure={"category": "campaign", "command": "build", "commandHash": "hash", "outcome": "exit:1"},
                )
                raise RuntimeError("campaign validation command 1 exited with code 1; log: failed.log")
            def approved(*args):
                store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "repaired"}
                return True
            with patch("run.create_worktree", return_value=(Path(root), "branch")), patch("run.invoke_with_replacements", side_effect=worker), patch("run.validate_candidate", side_effect=validate), patch("run.repair_failed_validation", return_value="repaired") as repair, patch("run.publish_candidate", return_value={"number": 1}), patch("run.run_review", side_effect=approved), patch("run.merge_assignment", return_value=True), patch("run.cleanup_worktree"):
                self.assertTrue(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            self.assertEqual((len(attempts), store.state["attemptCounters"][assignment["id"]]), (2, 2))
            repair.assert_called_once()

    def test_validation_repair_covers_focused_campaign_and_audit_assignments(self):
        for assignment_id, category in (("TASK-0001", "task"), ("TASK-0001", "campaign"), ("BUG-0001", "task")):
            with self.subTest(assignment=assignment_id, category=category), tempfile.TemporaryDirectory() as root:
                target = make_git_repository(Path(root))
                base = git_output(target, "rev-parse", "HEAD").strip()
                (target / "src").mkdir()
                (target / "src" / "value.txt").write_text("bad\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(target), "add", "src/value.txt"], check=True)
                subprocess.run(["git", "-C", str(target), "commit", "-m", "candidate"], check=True, capture_output=True)
                candidate = git_output(target, "rev-parse", "HEAD").strip()
                store = self.state_store(target)
                run.exclude_relay_files(target)
                assignment = ContractTests().task(assignment_id)
                store.state["baselineValidation"]["phase"] = "passed"
                store.state["worktrees"][assignment_id] = {"baseSha": base}
                store.state["taskStates"][assignment_id] = {
                    "phase": "candidate-validation", "validationCandidateSha": candidate,
                    "terminalValidationCandidateSha": candidate, "validationRepairAttemptsStarted": 0,
                    "validationRepairCallsStarted": 0, "validationLog": "failed.log",
                    "validationFailure": {"category": category, "command": "failed command", "commandHash": "hash", "outcome": "exit:1"},
                }
                calls = []
                def agent(*args, **kwargs):
                    number, process_id = run._consume_agent_call(store, assignment_id, "worker", "repair", False, False, True)
                    persisted = json.loads(store.path.read_text(encoding="utf-8"))["taskStates"][assignment_id]
                    self.assertEqual((persisted["validationRepairAttemptsStarted"], persisted["validationRepairCallsStarted"]), (1, 1))
                    self.assertIn("failed command", args[5])
                    self.assertIn("failed.log", args[5])
                    (target / "src" / "value.txt").write_text("good\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(target), "commit", "-am", "repair"], check=True, capture_output=True)
                    repaired = git_output(target, "rev-parse", "HEAD").strip()
                    store.state["activeProcesses"].pop(process_id)
                    return {"candidateSha": repaired, "summary": "repaired validation"}
                with patch("run.invoke_agent", side_effect=agent), patch("run.run_validations", side_effect=lambda *args, **kwargs: calls.append(args[3])):
                    repaired = run.repair_failed_validation(store, threading.Semaphore(1), assignment, target)
                self.assertNotEqual(repaired, candidate)
                self.assertEqual(calls, ["task", "campaign"])
                session = run.ensure_review_session(store, assignment_id, repaired)
                self.assertEqual((session["phase"], session["initialCandidateSha"], session["repairAttemptsStarted"], session["reviewCallsStarted"]), ("initial-review", repaired, 1, 1))

    def test_validation_timeout_replays_once_before_worker_repair(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            (target / "src").mkdir()
            (target / "src" / "value.txt").write_text("bad\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "src/value.txt"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "candidate"], check=True, capture_output=True)
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            assignment.update(allowedPaths=["src"], validationCommands=[])
            store.state["baselineValidation"]["phase"] = "passed"
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            failure = {"category": "campaign", "command": "hang", "commandHash": "timeout-hash", "outcome": "timeout"}
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation", "validationCandidateSha": candidate, "terminalValidationCandidateSha": candidate, "validationFailure": failure, "validationRepairAttemptsStarted": 0, "validationRepairCallsStarted": 0}
            events = []
            def validate(_store, _assignment, _worktree, result):
                events.append(("validate", result["candidateSha"]))
                if result["candidateSha"] == candidate:
                    self.assertEqual(store.state["taskStates"][assignment["id"]]["validationTimeoutReplayIdentity"], {"candidateSha": candidate, "commandHash": "timeout-hash"})
                    store.state["taskStates"][assignment["id"]]["validationFailure"] = failure
                    raise RuntimeError("campaign validation command 1 timed out after 1s; log: timeout.log")
                return result["candidateSha"]
            def agent(*args, **kwargs):
                events.append(("worker", candidate))
                number, process_id = run._consume_agent_call(store, assignment["id"], "worker", "repair", False, False, True)
                (target / "src" / "value.txt").write_text("good\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(target), "commit", "-am", "repair"], check=True, capture_output=True)
                repaired = git_output(target, "rev-parse", "HEAD").strip()
                store.state["activeProcesses"].pop(process_id)
                return {"candidateSha": repaired}
            with patch("run.validate_candidate", side_effect=validate), patch("run.invoke_agent", side_effect=agent):
                repaired = run.repair_failed_validation(store, threading.Semaphore(1), assignment, target)
            self.assertEqual([event[0] for event in events], ["validate", "worker", "validate"])
            self.assertNotEqual(repaired, candidate)

    def test_completed_validation_repair_resumes_without_free_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            (target / "src").mkdir()
            (target / "src" / "value.txt").write_text("bad\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "src/value.txt"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "candidate"], check=True, capture_output=True)
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            (target / "src" / "value.txt").write_text("good\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "commit", "-am", "completed repair"], check=True, capture_output=True)
            repaired = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            assignment.update(allowedPaths=["src"], validationCommands=[])
            store.state.update(campaignValidationCommands=[])
            store.state["baselineValidation"]["phase"] = "passed"
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            store.state["taskStates"][assignment["id"]] = {
                "phase": "validation-repair-1", "validationCandidateSha": candidate,
                "validationFailure": {"category": "task", "command": "test", "commandHash": "hash", "outcome": "exit:1"},
                "validationRepairAttemptsStarted": 1, "validationRepairCallsStarted": 1,
                "validationRepairInProgress": {"baseSha": candidate, "failure": {}},
            }
            with patch("run.invoke_with_replacements") as worker, patch("run.validate_candidate", return_value=repaired) as validate:
                self.assertEqual(run.repair_failed_validation(store, threading.Semaphore(1), assignment, target), repaired)
            worker.assert_not_called()
            validate.assert_called_once()
            self.assertEqual((store.state["taskStates"][assignment["id"]]["validationRepairAttemptsStarted"], store.state["taskStates"][assignment["id"]]["validationRepairCallsStarted"]), (1, 1))

    def test_same_sha_validation_repairs_are_rejected_before_persistence_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            (target / "src").mkdir()
            (target / "src" / "value.txt").write_text("bad\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "src/value.txt"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "candidate"], check=True, capture_output=True)
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target, fix_loops=2)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            assignment.update(allowedPaths=["src"], validationCommands=[])
            store.state["baselineValidation"]["phase"] = "passed"
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            store.state["taskStates"][assignment["id"]] = {
                "phase": "candidate-validation", "validationCandidateSha": candidate,
                "validationFailure": {"category": "task", "command": "test", "commandHash": "hash", "outcome": "exit:1"},
                "validationRepairAttemptsStarted": 0, "validationRepairCallsStarted": 0,
            }
            def agent(*args, **kwargs):
                _, process_id = run._consume_agent_call(store, assignment["id"], "worker", "repair", False, False, True)
                store.state["activeProcesses"].pop(process_id)
                return {"candidateSha": candidate}
            with patch("run.invoke_agent", side_effect=agent):
                self.assertIsNone(run.repair_failed_validation(store, threading.Semaphore(1), assignment, target))
            task_state = store.state["taskStates"][assignment["id"]]
            self.assertEqual((task_state["validationRepairAttemptsStarted"], task_state["validationRepairCallsStarted"]), (2, 2))
            self.assertNotIn("validationRepairPendingSha", task_state)
            self.assertNotIn("validationRepairInProgress", task_state)

    def test_legacy_same_sha_repair_state_replays_candidate_without_refunding_counters(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            assignment = ContractTests().task()
            store.state.update(
                attemptCounters={assignment["id"]: 2},
                validationCommandsStarted={assignment["id"]: 5},
                recoveryAttemptGrants={assignment["id"]: 1},
                recoveryAttemptsStarted={assignment["id"]: 1},
            )
            store.state["worktrees"][assignment["id"]] = {"path": str(target), "root": str(target), "branch": "branch", "baseSha": candidate}
            store.state["taskStates"][assignment["id"]] = {
                "phase": "candidate-validation", "mode": "task", "pushed": False, "merged": False,
                "validationCandidateSha": candidate, "validationCircuitBroken": True,
                "validationFailure": {"category": "task", "command": "test", "commandHash": "hash", "outcome": "exit:1"},
                "validationRepairAttemptsStarted": 1, "validationRepairCallsStarted": 1,
                "validationRepairPendingSha": candidate,
                "validationRepairInProgress": {"baseSha": candidate, "failure": {}},
            }
            store.save()
            counters = (dict(store.state["attemptCounters"]), dict(store.state["validationCommandsStarted"]), dict(store.state["recoveryAttemptsStarted"]), 1, 1)
            run.reconcile(store)
            task_state = store.state["taskStates"][assignment["id"]]
            self.assertEqual(task_state["pendingWorkerSha"], candidate)
            self.assertNotIn("validationRepairPendingSha", task_state)
            self.assertNotIn("validationRepairInProgress", task_state)
            self.assertNotIn("validationCircuitBroken", task_state)
            def approved(*args):
                store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": candidate}
                return True
            with patch("run.invoke_with_replacements") as worker, patch("run.validate_candidate", return_value=candidate) as validate, patch("run.publish_candidate", return_value={"number": 1}), patch("run.run_review", side_effect=approved), patch("run.merge_assignment", return_value=True), patch("run.cleanup_worktree"):
                self.assertTrue(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            worker.assert_not_called()
            validate.assert_called_once()
            task_state = store.state["taskStates"][assignment["id"]]
            self.assertEqual((store.state["attemptCounters"], store.state["validationCommandsStarted"], store.state["recoveryAttemptsStarted"], task_state["validationRepairAttemptsStarted"], task_state["validationRepairCallsStarted"]), counters)

    def test_validation_repair_zero_budget_and_unsafe_candidate_need_user(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            (target / "src").mkdir()
            (target / "src" / "value.txt").write_text("bad\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "src/value.txt"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "candidate"], check=True, capture_output=True)
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            assignment = ContractTests().task()
            assignment.update(allowedPaths=["src"], validationCommands=[])
            store = self.state_store(target, fix_loops=0)
            run.exclude_relay_files(target)
            store.state["baselineValidation"]["phase"] = "passed"
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            store.state["taskStates"][assignment["id"]] = {"validationCandidateSha": candidate, "validationFailure": {"category": "task", "commandHash": "hash", "outcome": "exit:1"}}
            with patch("run.invoke_with_replacements") as worker:
                self.assertIsNone(run.repair_failed_validation(store, threading.Semaphore(1), assignment, target))
            worker.assert_not_called()
            store.state["fixLoopLimit"] = 1
            (target / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with patch("run.invoke_with_replacements") as worker:
                self.assertIsNone(run.repair_failed_validation(store, threading.Semaphore(1), assignment, target))
            worker.assert_not_called()

    def test_repair_validation_failures_consume_shared_fix_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root, fix_loops=2)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "initial-review"}
            store.state["worktrees"][assignment["id"]] = {"baseSha": "base"}
            store.state["reviewSessions"][assignment["id"]] = {
                "phase": "repair-1", "acceptedBlockerIds": [], "repairAttemptsStarted": 0,
                "reviewCallsStarted": 0, "reviewCallLimit": 9,
            }
            candidate = {"status": "candidate", "candidateSha": "repair"}
            with patch("run.invoke_with_replacements", return_value=candidate) as worker, patch("run.validate_candidate", side_effect=[RuntimeError("first validation"), RuntimeError("last validation")]):
                self.assertFalse(run.run_review(store, threading.Semaphore(1), assignment, Path(root), "sha"))
            session = store.state["reviewSessions"][assignment["id"]]
            self.assertEqual((worker.call_count, session["repairAttemptsStarted"], session["phase"]), (2, 2, "needs-user"))
            self.assertEqual(store.state["taskStates"][assignment["id"]]["error"], "last validation")

    def test_completed_worker_resumes_validation_without_another_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["attemptCounters"][assignment["id"]] = store.state["taskAttemptLimit"]
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation", "mode": "task", "pendingWorkerSha": "abc", "pushed": False, "merged": False}
            def approved(*args):
                store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc"}
                return True
            with patch("run.create_worktree", return_value=(Path(root), "branch")), patch("run.invoke_with_replacements") as worker, patch("run.validate_candidate", return_value="abc") as validate, patch("run.publish_candidate", return_value={"number": 1}), patch("run.run_review", side_effect=approved), patch("run.merge_assignment", return_value=True), patch("run.cleanup_worktree"):
                self.assertTrue(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            worker.assert_not_called()
            validate.assert_called_once()

    def test_terminal_candidate_replay_is_persisted_without_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "needs-user", "terminalValidationCandidateSha": "candidate", "validationFailure": {"category": "campaign", "command": "build", "commandHash": hashlib.sha256(b"build").hexdigest(), "outcome": "exit:1"}}
            store.state["worktrees"][assignment["id"]] = {"baseSha": "base"}
            def replay_git(repo_path, *args, **kwargs):
                if args[0] == "rev-parse":
                    return subprocess.CompletedProcess([], 0, "candidate\n", "")
                return subprocess.CompletedProcess([], 0, "", "")
            with patch("run.recovery_worktree", return_value=(Path(root), store.state["worktrees"][assignment["id"]])), patch("run.git", side_effect=replay_git):
                run.prepare_terminal_validation_replays(store)
            task_state = store.state["taskStates"][assignment["id"]]
            self.assertEqual((task_state["phase"], task_state["pendingWorkerSha"]), ("candidate-validation", "candidate"))
            self.assertTrue(task_state["terminalValidationReplayUsed"])
            self.assertEqual(store.state["attemptCounters"], {})

    def test_terminal_review_validation_recovery_replays_candidate_without_worker_or_budget_changes(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["phase"] = "needs-user"
            store.state["attemptCounters"][assignment["id"]] = 3
            store.state["recoveryAttemptGrants"] = {assignment["id"]: 1}
            store.state["recoveryAttemptsStarted"] = {assignment["id"]: 0}
            store.state["taskStates"][assignment["id"]] = {
                "phase": "needs-user", "candidateSha": "old", "validationCandidateSha": "new",
                "terminalValidationCandidateSha": "new", "terminalValidationReviewRepair": 2,
                "validationFailure": {"category": "campaign", "command": "build", "commandHash": "hash", "outcome": "exit:1"},
                "validationRepairAttemptsStarted": 0, "validationRepairCallsStarted": 0,
            }
            history = {"contract-reviewer": {"findings": []}}
            store.state["reviewSessions"][assignment["id"]] = {
                "phase": "needs-user", "initialCandidateSha": "old", "currentCandidateSha": "old", "previousCandidateSha": "old",
                "acceptedBlockerIds": [], "repairAttemptsStarted": 2, "reviewCallsStarted": 5, "reviewCallLimit": 9,
                "initialResults": history,
            }
            store.state["worktrees"][assignment["id"]] = {"baseSha": "base", "branch": "branch", "path": root}
            counters = (dict(store.state["attemptCounters"]), dict(store.state["recoveryAttemptGrants"]), dict(store.state["recoveryAttemptsStarted"]), 2, 5)

            def recovery_git(_repo, *args, **_kwargs):
                return subprocess.CompletedProcess([], 0, "new\n" if args[0] == "rev-parse" else "", "")

            with patch("run.recovery_worktree", return_value=(Path(root), store.state["worktrees"][assignment["id"]])), patch("run.git", side_effect=recovery_git):
                actions = run.plan_recovery(store, [assignment], [], [assignment["id"]])
                _lines, next_line = run.campaign_summary(store.state, [assignment], [])
                self.assertNotIn("grant-attempt", next_line)
                self.assertEqual(actions, [{"action": "resume-review-validation", "assignmentId": assignment["id"], "headSha": "new", "repair": 2, "commandHash": "hash"}])
                with patch("run.validate_candidate", return_value="new") as validate, patch("run.invoke_with_replacements") as worker:
                    run.apply_recovery(store, [assignment], actions)
                validate.assert_called_once_with(store, assignment, Path(root), {"candidateSha": "new"})
                worker.assert_not_called()

            task_state = store.state["taskStates"][assignment["id"]]
            session = store.state["reviewSessions"][assignment["id"]]
            self.assertEqual((task_state["phase"], session["phase"], session["pendingRepairSha"]), ("verify-2", "verify-2", "new"))
            self.assertEqual(task_state["reviewValidationReplayIdentity"], {"candidateSha": "new", "commandHash": "hash", "repairNumber": 2})
            self.assertEqual((store.state["attemptCounters"], store.state["recoveryAttemptGrants"], store.state["recoveryAttemptsStarted"], session["repairAttemptsStarted"], session["reviewCallsStarted"]), counters)
            self.assertEqual(session["initialResults"], history)

            store.state["phase"] = task_state["phase"] = session["phase"] = "needs-user"
            with patch("run.recovery_worktree", return_value=(Path(root), store.state["worktrees"][assignment["id"]])), patch("run.git", side_effect=recovery_git), self.assertRaisesRegex(RuntimeError, "explicit disposition"):
                run.plan_recovery(store, [assignment], [], [assignment["id"]])

    def test_clean_changed_terminal_candidate_replays_without_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {
                "phase": "needs-user", "terminalValidationCandidateSha": "old",
                "validationFailure": {"category": "campaign", "command": "build", "commandHash": "hash", "outcome": "exit:1"},
            }
            store.state["worktrees"][assignment["id"]] = {"baseSha": "base"}

            def replay_git(_repo, *args, **kwargs):
                if args[0] == "rev-parse":
                    return subprocess.CompletedProcess([], 0, "new\n", "")
                if args[0] == "diff":
                    return subprocess.CompletedProcess([], 0, "src/fixed.ps1\n", "")
                if args[0] == "merge-base":
                    return subprocess.CompletedProcess([], 0, "", "")
                return subprocess.CompletedProcess([], 0, "", "")

            with patch("run.recovery_worktree", return_value=(Path(root), store.state["worktrees"][assignment["id"]])), patch("run.git", side_effect=replay_git):
                run.prepare_terminal_validation_replays(store)

            task_state = store.state["taskStates"][assignment["id"]]
            self.assertEqual((task_state["phase"], task_state["pendingWorkerSha"]), ("candidate-validation", "new"))
            self.assertTrue(task_state["terminalValidationReplayUsed"])
            self.assertEqual(store.state["attemptCounters"], {})

    def test_completed_repair_resumes_validation_then_verification(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "repair-2"}
            store.state["worktrees"][assignment["id"]] = {"baseSha": "base"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "repair-2", "acceptedBlockerIds": [], "repairAttemptsStarted": 2, "reviewCallsStarted": 3, "reviewCallLimit": 9, "pendingWorkerSha": "new", "previousCandidateSha": "old"}
            verified = {"assignmentId": assignment["id"], "candidateSha": "new", "status": "resolved"}
            with patch("run.validate_candidate", return_value="new") as validate, patch("run.invoke_with_replacements", return_value=verified) as agent:
                self.assertTrue(run.run_review(store, threading.Semaphore(1), assignment, Path(root), "old"))
            validate.assert_called_once()
            self.assertEqual(agent.call_args.args[4], "verification-reviewer")

    def test_real_cumulative_candidate_and_one_file_repair_validate(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            for name in ("one.txt", "two.txt"):
                (target / name).write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "one.txt", "two.txt"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "candidate"], check=True, capture_output=True)
            (target / "one.txt").write_text("repair\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "commit", "-am", "repair"], check=True, capture_output=True)
            repaired = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            assignment.update(allowedPaths=["one.txt", "two.txt"], validationCommands=[])
            store.state["campaignValidationCommands"] = []
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation"}
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            self.assertEqual(run.validate_candidate(store, assignment, target, {"candidateSha": repaired}), repaired)
            self.assertEqual(store.state["candidateShas"][assignment["id"]], repaired)

    def test_candidate_rejects_dirty_and_exact_out_of_scope_paths(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            (target / "outside.txt").write_text("committed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "outside.txt"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "outside"], check=True, capture_output=True)
            sha = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            assignment["validationCommands"] = []
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation"}
            store.state["worktrees"][assignment["id"]] = {"baseSha": base}
            with self.assertRaisesRegex(ValueError, "outside.txt"):
                run.validate_candidate(store, assignment, target, {"candidateSha": sha})
            (target / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "uncommitted"):
                run.validate_candidate(store, assignment, target, {"candidateSha": sha})

    def test_out_of_scope_blocker_stops_before_repair_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            bug = {"id": "BUG-0001", "title": "Export", "severity": "P1", "status": "active", "source": assignment["id"], "sourceFindingId": "export", "location": "module.psm1:1", "failure": "not exported", "reproduction": "test", "requirement": "export", "evidence": "missing", "allowedPaths": ["module.psm1"]}
            Path(root, "bugs.md").write_text(run.render_bugs("test", Path(root), [bug]), encoding="utf-8")
            store.state["taskStates"][assignment["id"]] = {"phase": "repair-1"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "repair-1", "acceptedBlockerIds": [bug["id"]], "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            with patch("run.invoke_with_replacements") as worker:
                self.assertFalse(run.run_review(store, threading.Semaphore(1), assignment, Path(root), "sha"))
            worker.assert_not_called()
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["repairAttemptsStarted"], 0)
            self.assertIn("module.psm1", store.state["taskStates"][assignment["id"]]["error"])

    def test_recovery_preview_preserves_state_and_grant_is_separate(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]] = {"phase": "needs-user", "candidateSha": "new", "error": "provider pull request source commit does not match candidate"}
            store.save()
            before = store.path.read_bytes()
            def recovery_git(repo_path, *args, **kwargs):
                output = "new\n" if args[0] == "rev-parse" else "new\trefs/heads/relay/TASK-0001\n" if args[0] == "ls-remote" else ""
                return subprocess.CompletedProcess([], 0, output, "")
            with patch("run.recovery_worktree", return_value=(Path(root), {"branch": "relay/TASK-0001"})), patch("run.git", side_effect=recovery_git):
                actions = run.plan_recovery(store, [assignment], [], [])
            self.assertEqual(actions, [{"action": "resume-publish", "assignmentId": assignment["id"], "candidateSha": "new", "headSha": "new", "branch": "relay/TASK-0001"}])
            self.assertEqual(store.path.read_bytes(), before)
            store.state["attemptCounters"][assignment["id"]] = store.state["taskAttemptLimit"]
            store.state["recoveryAttemptGrants"] = {assignment["id"]: 1}
            number, process_id = run._consume_agent_call(store, assignment["id"], "worker", "task", False, False)
            self.assertEqual(number, "recovery-1")
            store.state["activeProcesses"].pop(process_id)
            with self.assertRaisesRegex(RuntimeError, "attempt limit"):
                run._consume_agent_call(store, assignment["id"], "worker", "task", False, False)

    def test_granted_task_attempt_preserves_validation_repair_budgets(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]] = {
                "phase": "needs-user", "validationRepairAttemptsStarted": 1,
                "validationRepairCallsStarted": 2, "terminalValidationCandidateSha": "head",
            }
            action = {"action": "grant-attempt", "assignmentId": assignment["id"], "grant": 1, "headSha": "head", "dirty": [], "interruptionCause": "none"}
            def recovery_git(_repo, *args, **kwargs):
                return subprocess.CompletedProcess([], 0, "head\n" if args[0] == "rev-parse" else "", "")
            with patch("run.recovery_worktree", return_value=(Path(root), {"baseSha": "base"})), patch("run.git", side_effect=recovery_git):
                run.apply_recovery(store, [assignment], [action])
            task_state = store.state["taskStates"][assignment["id"]]
            self.assertEqual((task_state["validationRepairAttemptsStarted"], task_state["validationRepairCallsStarted"]), (1, 2))
            self.assertEqual(store.state["recoveryAttemptGrants"][assignment["id"]], 1)

    def test_exhausted_publication_recovers_existing_candidates_without_worker_attempts(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            tasks = [ContractTests().task(f"TASK-{number:04d}") for number in range(1, 4)]
            store.state.update(phase="needs-user", provider="azure-devops")
            for assignment in tasks[:2]:
                key = f"{assignment['id']}:pr-list"
                store.state["taskStates"][assignment["id"]] = {
                    "phase": "needs-user", "candidateSha": "abc", "pushedSha": "abc",
                    "error": f"Azure DevOps operation exhausted attempts: {key}",
                }
                store.state["providerAttemptCounters"][key] = store.state["providerAttemptLimit"]
            store.state["taskStates"]["TASK-0003"] = {
                "phase": "needs-user", "terminalValidationCandidateSha": "abc",
                "validationFailure": {"category": "campaign", "command": "build", "commandHash": "hash", "outcome": "exit:1"},
            }
            store.save()
            before = store.path.read_bytes()

            def worktree(_store, assignment_id):
                return Path(root), {"branch": f"relay/test/{assignment_id}"}

            def recovery_git(_repo, *args, **_kwargs):
                output = "abc\n" if args[0] == "rev-parse" else "abc\trefs/heads/branch\n" if args[0] == "ls-remote" else ""
                return subprocess.CompletedProcess([], 0, output, "")

            with patch("run.recovery_worktree", side_effect=worktree), patch("run.git", side_effect=recovery_git):
                actions = run.plan_recovery(store, tasks, [], ["TASK-0003"])
                self.assertEqual(store.path.read_bytes(), before)
                lines, next_line = run.campaign_summary(store.state, tasks, [])
                self.assertTrue(any(line.startswith("- BLOCKED category=provider-publication affected=test/TASK-0001,test/TASK-0002 reason=Azure DevOps PR discovery exhausted attempts log=") for line in lines))
                self.assertIn("--recover --grant-attempt TASK-0003", next_line)
                with patch("run.stderr_event") as event:
                    run.report_stopped(store.state)
                self.assertTrue(any("affected=TASK-0001,TASK-0002" in call.args[1] for call in event.call_args_list))
                run.apply_recovery(store, tasks, actions)

            self.assertEqual([action["action"] for action in actions], ["resume-publish", "resume-publish", "grant-attempt"])
            self.assertTrue(all(store.state["taskStates"][task_id]["phase"] == "push-and-open-pr" for task_id in ("TASK-0001", "TASK-0002")))
            self.assertEqual(store.state["taskStates"]["TASK-0003"]["phase"], "ready")
            self.assertFalse(any(key.endswith(":pr-list") for key in store.state["providerAttemptCounters"]))
            self.assertEqual(store.state["attemptCounters"], {})

    def test_status_groups_provider_publication_and_names_safe_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            key = "TASK-0001:pr-list"
            store.state.update(phase="needs-user", provider="azure-devops")
            store.state["taskStates"]["TASK-0001"] = {"phase": "needs-user", "error": f"Azure DevOps operation exhausted attempts: {key}"}
            store.save()
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(status.main(["--repo", root]), 0)
            shown = output.getvalue()
            self.assertIn("category=provider-publication affected=test/TASK-0001", shown)
            self.assertIn(str(Path(".relay") / "logs" / "provider.log"), shown)
            self.assertIn("restore provider access, then use safe publication recovery", shown)

    def test_confirmed_defer_archives_rejected_repair_and_restores_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            (target / "src").mkdir()
            (target / "src" / "run.ps1").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "src/run.ps1"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "candidate"], check=True, capture_output=True)
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            (target / "module.psm1").write_text("rejected\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "module.psm1"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "rejected repair"], check=True, capture_output=True)
            rejected = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            bug = {"id": "BUG-0001", "title": "Export", "severity": "P1", "status": "active", "source": assignment["id"], "sourceFindingId": "export", "location": "module.psm1:1", "failure": "not exported", "reproduction": "test", "requirement": "export", "evidence": "missing", "allowedPaths": ["module.psm1"]}
            Path(target, "bugs.md").write_text(run.render_bugs("test", target, [bug]), encoding="utf-8")
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]] = {"phase": "needs-user", "candidateSha": candidate}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "needs-user", "acceptedBlockerIds": [bug["id"]], "initialCandidateSha": candidate, "repairAttemptsStarted": 2}
            action = {"action": "defer-review", "assignmentId": assignment["id"], "bugIds": [bug["id"]], "headSha": rejected, "candidateSha": candidate, "branch": "relay/TASK-0001"}
            with patch("run.recovery_worktree", return_value=(target, {"branch": "relay/TASK-0001"})):
                run.apply_recovery(store, [assignment], [action])
            self.assertEqual(git_output(target, "rev-parse", "HEAD").strip(), candidate)
            archive = f"archive/test/{assignment['id']}/rejected-{rejected[:12]}"
            self.assertEqual(git_output(target, "rev-parse", archive).strip(), rejected)
            self.assertEqual(run.load_bugs(store)[0]["status"], "backlog")
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["repairAttemptsStarted"], 2)
            self.assertEqual((store.state["phase"], len(store.state["recoveryHistory"])), ("build", 1))

    def test_legacy_preview_and_confirm_archive_candidates_and_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            base = git_output(target, "rev-parse", "HEAD").strip()
            campaign = f"migration-{Path(root).name.lower()}"
            shared = "full build"
            tasks = [ContractTests().task(f"TASK-{number:04d}") for number in (1, 2)]
            for number, task in enumerate(tasks, 1):
                task["allowedPaths"] = [f"candidate-{number}.txt"]
                task["validationCommands"] = [f"focused-{number}", shared]
            args = run.parser().parse_args(["--repo", str(target)])
            metadata = {"baseSha": base, "requirementsHash": "abc123", "campaignValidationCommands": ["modern"], "legacyCampaignValidation": False}
            state = run.initial_state(target, metadata, args)
            state.update(campaignId=campaign, phase="needs-user", repositoryRoot=str(target), repositoryPrefix="")
            state.pop("campaignValidationCommands")
            state.pop("baselineValidation")
            relay = target / ".relay"; (relay / "logs").mkdir(parents=True)
            store = run.StateStore(relay / "state.json", state)
            (target / "tasks.md").write_text(plan.render_tasks(tasks, base, "abc123"), encoding="utf-8")
            (target / "bugs.md").write_text(run.render_bugs(campaign, target), encoding="utf-8")
            failure = {"category": "campaign", "command": shared, "commandHash": hashlib.sha256(shared.encode()).hexdigest(), "outcome": "exit:1"}
            roots = []
            try:
                for number, task in enumerate(tasks, 1):
                    worktree_root = Path(tempfile.gettempdir()).resolve() / "relay-worktrees" / campaign / task["id"]
                    roots.append(worktree_root)
                    subprocess.run(["git", "-C", str(target), "worktree", "add", "-b", f"relay/{task['id']}", str(worktree_root), base], check=True, capture_output=True)
                    (worktree_root / f"candidate-{number}.txt").write_text(f"candidate {number}\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(worktree_root), "add", "."], check=True)
                    subprocess.run(["git", "-C", str(worktree_root), "commit", "-m", f"candidate {number}"], check=True, capture_output=True)
                    candidate = git_output(worktree_root, "rev-parse", "HEAD").strip()
                    state["worktrees"][task["id"]] = {"path": str(worktree_root), "root": str(worktree_root), "branch": f"relay/{task['id']}", "baseSha": base}
                    state["taskStates"][task["id"]] = {"phase": "needs-user", "validationCandidateSha": candidate, "validationFailure": failure, "workerSummary": f"implemented {number}"}
                store.save()
                before = store.path.read_bytes()
                with patch("run.require_validation_shell"), patch("run.validation_command", return_value=[sys.executable, "-c", "raise SystemExit(7)"]):
                    actions = run.plan_recovery(store, tasks, [], [])
                self.assertEqual(store.path.read_bytes(), before)
                self.assertEqual(actions[0]["action"], "archive-and-handoff")
                with run.coordinator_lock(store.path.parent):
                    handoff = run.apply_recovery(store, tasks, actions)
                run.finish_archive_cleanup(store)
                archive = target / ".relay-archive" / campaign
                self.assertTrue(handoff.samefile(archive / "HANDOFF.md"))
                self.assertFalse((target / ".relay").exists())
                self.assertFalse((target / "tasks.md").exists())
                payload = plan.parse_handoff(handoff.read_text(encoding="utf-8"), target)
                self.assertEqual([entry["contract"]["id"] for entry in payload["tasks"]], ["TASK-0001", "TASK-0002"])
                extra = ContractTests().task("TASK-0003")
                commands, seeded = plan.apply_handoff([*[dict(task) for task in tasks], extra], ["invented"], payload)
                self.assertEqual(commands, [shared])
                self.assertEqual([task["id"] for task in seeded], ["TASK-0001", "TASK-0002"])
                self.assertEqual(seeded[0]["validationCommands"], ["focused-1"])
            finally:
                for worktree_root in roots:
                    if worktree_root.exists():
                        subprocess.run(["git", "-C", str(target), "worktree", "remove", "--force", str(worktree_root)], check=False, capture_output=True)

    def test_recovery_rejects_active_campaign_and_unknown_ids(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="needs-user", activeProcesses={"worker": {}})
            with self.assertRaisesRegex(RuntimeError, "inactive"):
                run.plan_recovery(store, [assignment], [], [])
            store.state["activeProcesses"] = {}
            with self.assertRaisesRegex(ValueError, "active campaign bug"):
                run.plan_recovery(store, [assignment], ["BUG-9999"], [])
            with self.assertRaisesRegex(ValueError, "campaign task"):
                run.plan_recovery(store, [assignment], [], ["TASK-9999"])

    def test_recovery_resumes_review_after_correcting_location_range(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            assignment["allowedPaths"] = ["src/run.ps1"]
            bug = {"id": "BUG-0001", "title": "Broken", "severity": "P1", "status": "active", "source": assignment["id"], "sourceFindingId": "range", "location": "src/run.ps1:27-34,60-70", "failure": "fails", "reproduction": "test", "requirement": "works", "evidence": "failure", "allowedPaths": ["src/run.ps1:27-34,60-70"]}
            Path(root, "bugs.md").write_text(run.render_bugs("test", Path(root), [bug]), encoding="utf-8")
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]] = {"phase": "needs-user", "candidateSha": "sha", "error": "accepted blocker requires paths outside assignment scope: src/run.ps1:27-34,60-70"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "needs-user", "acceptedBlockerIds": [bug["id"]], "repairAttemptsStarted": 0}
            def recovery_git(repo_path, *args, **kwargs):
                return subprocess.CompletedProcess([], 0, "sha\n" if args[0] == "rev-parse" else "", "")
            with patch("run.recovery_worktree", return_value=(Path(root), {"branch": "branch"})), patch("run.git", side_effect=recovery_git):
                actions = run.plan_recovery(store, [assignment], [], [])
                run.apply_recovery(store, [assignment], actions)
            self.assertEqual(actions[0]["action"], "resume-review")
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["phase"], "repair-1")
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["repairAttemptsStarted"], 0)

    def test_recovery_resumes_audit_bug_with_scope_commands_without_worker_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            command = "python -m unittest"
            finding = {"id": "AUDIT-F1"}
            bug = {"id": "BUG-0001", "title": "Broken", "severity": "P1", "status": "active", "source": "audit", "sourceFindingId": finding["id"], "location": "src/run.ps1:1", "failure": "fails", "reproduction": "Run the failing scenario.", "requirement": "works", "evidence": "failure", "allowedPaths": ["src/run.ps1"]}
            Path(root, "bugs.md").write_text(run.render_bugs("test", Path(root), [bug]), encoding="utf-8")
            store.state.update(phase="needs-user", auditScopes={"AUDIT-0001": {"commands": [command], "findings": [finding]}})
            store.state["taskStates"][bug["id"]] = {"phase": "needs-user", "error": "validation command 1 exited with code 1; log: failed.log"}
            record = {"branch": "relay/BUG-0001", "baseSha": "base"}
            def recovery_git(repo_path, *args, **kwargs):
                return subprocess.CompletedProcess([], 0, "head\n" if args[0] == "rev-parse" else "", "")
            with patch("run.recovery_worktree", return_value=(Path(root), record)), patch("run.git", side_effect=recovery_git), patch("run.target_changes", return_value=["src/run.ps1"]):
                actions = run.plan_recovery(store, [assignment], [], [])
                run.apply_recovery(store, [assignment], actions)
            self.assertEqual(actions, [{"action": "resume-audit-validation", "assignmentId": bug["id"], "headSha": "head", "commands": [command]}])
            self.assertEqual(store.state["attemptCounters"].get(bug["id"], 0), 0)
            self.assertEqual(store.state["auditBugValidationCommands"][bug["id"]], [command])
            self.assertEqual(store.state["taskStates"][bug["id"]]["pendingWorkerSha"], "head")

    def test_recovery_adopts_only_matching_user_owned_deletions(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]] = {"phase": "needs-user", "error": "candidate changed paths outside assignment scope: removed.txt"}
            record = {"branch": "branch", "baseSha": "base"}
            def recovery_git(repo_path, *args, **kwargs):
                return subprocess.CompletedProcess([], 0, "head\n" if args[0] == "rev-parse" else "", "")
            def paths(store_value, repo_path, *args):
                return ["src/ok.txt", "removed.txt"] if "--diff-filter=D" not in args else ["removed.txt"]
            with patch("run.recovery_worktree", return_value=(Path(root), record)), patch("run.git", side_effect=recovery_git), patch("run.target_git_paths", side_effect=paths), patch("run.target_changes", return_value=["src/ok.txt", "removed.txt"]):
                actions = run.plan_recovery(store, [assignment], [], [])
            self.assertEqual(actions, [{"action": "adopt-user-deletions", "assignmentId": assignment["id"], "headSha": "head", "paths": ["removed.txt"]}])
            def validate(store_value, assignment_value, worktree, result):
                self.assertEqual(run.assignment_paths(store_value, assignment_value), ["src", "removed.txt"])
                return "head"
            with patch("run.recovery_worktree", return_value=(Path(root), record)), patch("run.git", side_effect=recovery_git), patch("run.target_git_paths", return_value=["removed.txt"]), patch("run.validate_candidate", side_effect=validate):
                run.apply_recovery(store, [assignment], actions)
            self.assertEqual(store.state["recoveryAllowedPaths"][assignment["id"]], ["removed.txt"])

    def test_detects_supported_provider_remotes_and_decodes_names(self):
        cases = {
            "https://github.com/owner/repo.git": ("github", "owner/repo"),
            "git@github.com:owner/repo.git": ("github", "owner/repo"),
            "https://dev.azure.com/myorg/My%20Project/_git/My%20Repo": ("azure-devops", "myorg", "My Project", "My Repo"),
            "git@ssh.dev.azure.com:v3/myorg/My%20Project/My%20Repo": ("azure-devops", "myorg", "My Project", "My Repo"),
            "https://myorg.visualstudio.com/DefaultCollection/My%20Project/_git/My%20Repo": ("azure-devops", "myorg", "My Project", "My Repo"),
            "git@vs-ssh.visualstudio.com:v3/myorg/My%20Project/My%20Repo": ("azure-devops", "myorg", "My Project", "My Repo"),
        }
        for remote, expected in cases.items():
            with self.subTest(remote=remote):
                identity = run.detect_provider(remote)
                actual = (identity["provider"], identity.get("githubRepository")) if identity["provider"] == "github" else (identity["provider"], identity["azureOrganization"], identity["azureProject"], identity["azureRepository"])
                self.assertEqual(actual, expected)
        with self.assertRaises(ValueError):
            run.detect_provider("https://github.com.evil.invalid/owner/repo")
        with self.assertRaises(ValueError):
            run.detect_provider("file://github.com/owner/repo")

    def test_azure_pr_discovery_is_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            completed = subprocess.CompletedProcess([], 0, json.dumps([self.azure_pr()]), "")
            with patch("run.provider_with_retries", return_value=completed) as provider:
                prs = run.pr_discover(store, "list", "relay/TASK-0001")
            self.assertEqual(prs, [{"number": 7, "url": "https://dev.azure.com/my%20org/My%20Project/_git/My%20Repo/pullrequest/7", "headRefOid": "abc", "state": "OPEN"}])
            self.assertIn("--source-branch", provider.call_args.args)

    def test_azure_pr_create_uses_description_and_validates_sha(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            body = Path(root) / "body.md"; body.write_text("Relay assignment\r\n\r\nCandidate: abc\r\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, json.dumps(self.azure_pr()), "")
            with patch("run.provider_with_retries", return_value=completed) as provider:
                pr = run.pr_create(store, "create", "relay/TASK-0001", "title", body)
            self.assertEqual(pr["headRefOid"], "abc")
            description = provider.call_args.args[provider.call_args.args.index("--description") + 1]
            self.assertRegex(description, r"[\r\n]")
            self.assertIn("Candidate: abc", description)

    def test_stopped_reports_every_blocked_assignment(self):
        state = {
            "phase": "needs-user", "error": "campaign\nfailed",
            "agentsBootstrap": {"phase": "needs-user", "providerStatus": "bootstrap failed"},
            "taskStates": {
                "TASK-0002": {"phase": "waiting-provider", "providerStatus": "checks pending"},
                "TASK-0001": {"phase": "needs-user", "error": "task\nfailed"},
            },
        }
        with patch("run.stderr_event") as event:
            run.report_stopped(state)
        self.assertEqual(event.call_args_list, [
            unittest.mock.call("STOPPED", "phase=needs-user assignments=4"),
            unittest.mock.call("BLOCKED", "category=assignment affected=AGENTS reason=bootstrap failed log=not-recorded"),
            unittest.mock.call("BLOCKED", "category=assignment affected=CAMPAIGN reason=campaign failed log=not-recorded"),
            unittest.mock.call("BLOCKED", "category=assignment affected=TASK-0002 reason=checks pending log=not-recorded"),
            unittest.mock.call("BLOCKED", "category=assignment affected=TASK-0001 reason=task failed log=not-recorded"),
        ])

    def test_worktree_setup_blockers_are_grouped_and_recoverable_without_an_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            tasks = [ContractTests().task("TASK-0001"), ContractTests().task("TASK-0002")]
            error = "Command '['git', 'worktree', 'add']' returned non-zero exit status 255."
            store.state.update(phase="needs-user", baselineValidation={"phase": "passed", "baseSha": "0123456"})
            store.state["taskStates"] = {task["id"]: {"phase": "needs-user", "error": error} for task in tasks}
            store.save()
            before = store.path.read_bytes()
            actions = run.plan_recovery(store, tasks, [], [])
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(actions, [
                {"action": "resume-assignment-setup", "assignmentId": "TASK-0001", "baseSha": "0123456"},
                {"action": "resume-assignment-setup", "assignmentId": "TASK-0002", "baseSha": "0123456"},
            ])
            lines, next_line = run.campaign_summary(store.state, tasks, [])
            self.assertTrue(any(line.startswith("- BLOCKED category=worktree-setup affected=test/TASK-0001,test/TASK-0002 reason=Git could not create assignment worktrees before Worker launch") for line in lines))
            self.assertNotIn("non-zero exit status", "\n".join(lines))
            self.assertIn("Preview safe worktree setup recovery", next_line)
            with patch("run.stderr_event") as event:
                run.report_stopped(store.state)
            self.assertEqual(event.call_args_list[-1], unittest.mock.call("BLOCKED", "category=worktree-setup affected=TASK-0001,TASK-0002 reason=Git could not create assignment worktrees before Worker launch log=not-recorded"))
            run.apply_recovery(store, tasks, actions)
            self.assertEqual(store.state["phase"], "build")
            self.assertTrue(all(store.state["taskStates"][task["id"]] == {"phase": "ready"} for task in tasks))
            self.assertEqual(store.state["attemptCounters"], {})

    def test_create_worktree_uses_campaign_scoped_branch(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["campaignId"] = "campaign-123"
            assignment = ContractTests().task()
            calls = []
            def fake_git(_repo, *args, **_kwargs):
                calls.append(args)
                output = "base\n" if args[:2] == ("rev-parse", "origin/main") else ""
                return subprocess.CompletedProcess([], 0, output, "")
            with patch("run.tempfile.gettempdir", return_value=root), patch("run.git_provider_with_retries"), patch("run.git", side_effect=fake_git):
                _path, branch = run.create_worktree(store, assignment)
            self.assertEqual(branch, "relay/campaign-123/TASK-0001")
            self.assertIn(("worktree", "add", "-b", branch, str(Path(root).resolve() / "relay-worktrees" / "campaign-123" / "TASK-0001"), "base"), calls)

    def test_runtime_progress_is_compact_when_idle(self):
        state = {"workerLimit": 3, "taskTotal": 5, "taskStates": {}, "activeProcesses": {}}
        self.assertEqual(run.runtime_progress(state, []), "tasks 0/5 integrated | bugs 0 | agents 0/3 | idle")

    def test_status_shows_pre_review_repairs_in_shared_budgets(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["taskStates"]["TASK-0001"] = {"phase": "validation-repair-1", "validationRepairAttemptsStarted": 1, "validationRepairCallsStarted": 2}
            store.save()
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(status.main(["--repo", root]), 0)
            self.assertIn("fixes=1/2 review-calls=2/9", output.getvalue())

    def test_runtime_progress_counts_running_and_queued_agents_once(self):
        state = {
            "workerLimit": 3, "taskTotal": 5,
            "taskStates": {"TASK-0001": {"phase": "ready"}, "BUG-0001": {"phase": "integrated"}},
            "activeProcesses": {
                "one": {"assignmentId": "TASK-0001", "role": "contract-reviewer", "status": "running", "operationDeadline": 1},
                "two": {"assignmentId": "TASK-0001", "role": "risk-reviewer", "status": "running", "operationDeadline": 1},
                "three": {"assignmentId": "TASK-0003", "role": "triage-pm", "status": "running", "operationDeadline": 1},
                "four": {"assignmentId": "TASK-0003", "role": "verification-reviewer", "status": "queued", "operationDeadline": 1},
                "five": {"assignmentId": "TASK-0001", "role": "contract-reviewer", "status": "queued", "operationDeadline": 1},
                "six": {"assignmentId": "TASK-0003", "role": "risk-reviewer", "status": "queued", "operationDeadline": 1},
            },
        }
        line = run.runtime_progress(state, [])
        self.assertEqual(line, "tasks 0/5 integrated | bugs 0 | agents 3/3 (+3 queued) | review TASK-0001,TASK-0003")
        self.assertNotIn("deadline", line)

    def test_runtime_progress_orders_nonzero_bug_states_and_ignores_bug_tasks(self):
        state = {
            "workerLimit": 1, "taskTotal": 1, "activeProcesses": {},
            "taskStates": {"TASK-0001": {"phase": "integrated"}, "BUG-0001": {"phase": "integrated"}},
        }
        bugs = [{"status": status} for status in ("backlog", "resolved", "active", "needs-user", "waiting-provider", "resolved")]
        line = run.runtime_progress(state, bugs)
        self.assertEqual(line, "tasks 1/1 integrated | bugs active=1 needs-user=1 waiting-provider=1 resolved=2 backlog=1 | agents 0/1 | idle")

    def test_status_leads_with_approved_provider_wait(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["taskStates"]["TASK-0001"] = {"phase": "approved", "providerStatus": "pending"}
            store.state["pullRequests"]["TASK-0001"] = {"number": 25, "state": "OPEN", "url": "https://example.invalid/25"}
            store.state["providerDeadlines"]["TASK-0001"] = time.time() + 60
            store.save()
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(status.main(["--repo", root]), 0)
            shown = output.getvalue()
            self.assertTrue(shown.startswith("Overall: 0/1 integrated"))
            self.assertIn("External/provider waits", shown)
            self.assertIn("test/TASK-0001: PR #25 status=pending", shown)

    def test_status_does_not_count_integrated_audit_bugs_as_tasks(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["taskStates"].update({"TASK-0001": {"phase": "integrated"}, "BUG-0001": {"phase": "integrated"}})
            store.save()
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(status.main(["--repo", root]), 0)
            shown = output.getvalue()
            self.assertTrue(shown.startswith("Overall: 1/1 integrated"))
            self.assertIn("\n  integrated: 1\n", shown)

    def test_live_and_snapshot_status_name_approval_bypass_and_policy_wait(self):
        state = {
            "workerLimit": 2, "taskTotal": 2, "activeProcesses": {},
            "taskStates": {
                "TASK-0001": {"phase": "approved", "operation": "provider-approve"},
                "TASK-0002": {"phase": "approved", "operation": "merge-bypass"},
            },
        }
        live = run.runtime_progress(state, [])
        self.assertEqual(live, "tasks 0/2 integrated | bugs 0 | agents 0/2 | publish TASK-0001 | merge TASK-0002")
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["taskStates"]["TASK-0001"] = {"phase": "waiting-provider", "providerStatus": "policy-waiting"}
            store.state["pullRequests"]["TASK-0001"] = {"number": 25, "state": "OPEN", "url": "https://example.invalid/25"}
            store.save()
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(status.main(["--repo", root]), 0)
            self.assertIn("status=policy-waiting", output.getvalue())

    def test_azure_policy_pass_failure_conflict_and_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            show = subprocess.CompletedProcess([], 0, json.dumps(self.azure_pr()), "")
            approved = subprocess.CompletedProcess([], 0, json.dumps([{"configuration": {"isBlocking": True}, "status": "approved"}, {"configuration": {"isBlocking": False}, "status": "rejected"}]), "")
            with patch("run.run_tool", side_effect=[show, approved]):
                self.assertEqual(run.wait_for_checks(store, "PASS", {"number": 7}, "abc"), "passed")
            rejected = subprocess.CompletedProcess([], 0, json.dumps([{"configuration": {"isBlocking": True}, "status": "broken"}]), "")
            with patch("run.run_tool", side_effect=[show, rejected]):
                self.assertEqual(run.wait_for_checks(store, "FAIL", {"number": 7}, "abc"), "failed")
            conflict = subprocess.CompletedProcess([], 0, json.dumps(self.azure_pr(merge_status="conflicts")), "")
            with patch("run.run_tool", return_value=conflict):
                self.assertEqual(run.wait_for_checks(store, "CONFLICT", {"number": 7}, "abc"), "repair-required")
            store.state["providerCheckTimeoutSeconds"] = .01
            queued = subprocess.CompletedProcess([], 0, json.dumps([{"configuration": {"isBlocking": True}, "status": "queued"}]), "")
            with patch("run.run_tool", side_effect=lambda *args, **kwargs: queued if "policy" in args else show):
                self.assertEqual(run.wait_for_checks(store, "TIMEOUT", {"number": 7}, "abc"), "waiting-provider")

    def test_azure_approval_is_persisted_before_launch_and_is_sha_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            store.state["taskStates"]["TASK-0001"] = {"phase": "approved"}
            store.state["reviewSessions"]["TASK-0001"] = {"phase": "approved"}
            store.save()
            launches = []
            def approve(*args, **kwargs):
                persisted = json.loads(store.path.read_text(encoding="utf-8"))
                sha = ("abc", "def")[len(launches)]
                self.assertEqual(persisted["taskStates"]["TASK-0001"]["operation"], "provider-approve")
                self.assertGreater(persisted["taskStates"]["TASK-0001"]["operationDeadline"], time.time())
                self.assertEqual(persisted["providerAttemptCounters"][f"TASK-0001:provider-approve:{sha}"], 1)
                self.assertEqual(kwargs["timeout"], store.state["providerTimeoutSeconds"])
                launches.append(sha)
                return subprocess.CompletedProcess([], 0, "{}", "")
            with patch("run.run_tool", side_effect=approve) as provider:
                self.assertTrue(run.provider_approve(store, "TASK-0001", {"number": 7}, "abc"))
                self.assertTrue(run.provider_approve(store, "TASK-0001", {"number": 7}, "abc"))
                self.assertTrue(run.provider_approve(store, "TASK-0001", {"number": 7}, "def"))
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(provider.call_args_list[0].args, ("az", "repos", "pr", "set-vote", "--id", "7", "--vote", "approve", "--organization", "https://dev.azure.com/my%20org", "--output", "json"))
            self.assertEqual(store.state["reviewSessions"]["TASK-0001"]["providerApprovalSha"], "def")
            self.assertEqual((store.state["providerAttemptCounters"]["TASK-0001:provider-approve:abc"], store.state["providerAttemptCounters"]["TASK-0001:provider-approve:def"]), (1, 1))
            provider_log = (Path(root) / ".relay" / "logs" / "provider.log").read_text(encoding="utf-8")
            self.assertIn("TASK-0001:provider-approve:abc", provider_log)

    def test_azure_reviewer_policy_wait_does_not_consume_repair(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            assignment = ContractTests().task()
            store.state["providerCheckTimeoutSeconds"] = 1
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["pullRequests"][assignment["id"]] = {"number": 7, "state": "OPEN"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", {"number": 7, "state": "OPEN", "url": "x", "headRefOid": "abc"})
            show = subprocess.CompletedProcess([], 0, json.dumps(self.azure_pr()), "")
            waiting = subprocess.CompletedProcess([], 0, json.dumps([{"configuration": {"isBlocking": True, "type": {"id": "fa4e907d-c16b-4a4c-9dfa-4906e5d171dd"}}, "status": "rejected"}]), "")
            with patch("run.provider_approve"), patch("run.run_tool", side_effect=[show, waiting]), patch("run.time.time", side_effect=[100, 100, 101]), patch("run.time.sleep"), patch("run.invoke_with_replacements") as worker:
                self.assertFalse(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 7}, "abc"))
            self.assertEqual((store.state["taskStates"][assignment["id"]]["phase"], store.state["taskStates"][assignment["id"]]["providerStatus"]), ("waiting-provider", "policy-waiting"))
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["repairAttemptsStarted"], 0)
            worker.assert_not_called()

    def test_failed_azure_vote_can_be_followed_by_external_approval(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["pullRequests"][assignment["id"]] = {"number": 7, "state": "OPEN"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", {"number": 7, "state": "OPEN", "url": "x", "headRefOid": "abc"})
            merged = subprocess.CompletedProcess([], 0, "{}", "")
            merged_pr = {"number": 7, "state": "MERGED", "url": "x", "headRefOid": "abc"}
            with patch("run.provider_with_retries", side_effect=[RuntimeError("vote denied"), merged]) as provider, patch("run.pr_inspect", return_value=merged_pr), patch("run.wait_for_checks", return_value="passed"), patch("run.invoke_with_replacements") as worker:
                self.assertTrue(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 7}, "abc"))
            self.assertIn("set-vote", provider.call_args_list[0].args)
            self.assertIn("update", provider.call_args_list[1].args)
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["repairAttemptsStarted"], 0)
            worker.assert_not_called()

    def test_github_blocked_clean_checks_are_bypassable_but_bootstrap_is_not(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["provider"] = "github"
            blocked = subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": "abc", "mergeStateStatus": "BLOCKED", "statusCheckRollup": [], "state": "OPEN"}), "")
            with patch("run.run_tool", return_value=blocked):
                self.assertEqual(run.wait_for_checks(store, "TASK-0001", {"number": 1}, "abc"), "bypassable")
            store.state["agentsBootstrap"] = {"phase": "checks"}
            with patch("run.run_tool", return_value=blocked):
                self.assertEqual(run.wait_for_checks(store, "AGENTS", {"number": 1}, "abc"), "waiting-provider")

    def test_github_pending_and_failed_checks_never_become_bypassable(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state.update(provider="github", providerCheckTimeoutSeconds=1)
            pending = subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": "abc", "mergeStateStatus": "BLOCKED", "statusCheckRollup": [{"status": "IN_PROGRESS"}], "state": "OPEN"}), "")
            with patch("run.run_tool", return_value=pending), patch("run.time.time", side_effect=[100, 100, 101]), patch("run.time.sleep"):
                self.assertEqual(run.wait_for_checks(store, "PENDING", {"number": 1}, "abc"), "waiting-provider")
            failed = subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": "abc", "mergeStateStatus": "BLOCKED", "statusCheckRollup": [{"conclusion": "FAILURE"}], "state": "OPEN"}), "")
            with patch("run.run_tool", return_value=failed):
                self.assertEqual(run.wait_for_checks(store, "FAILED", {"number": 1}, "abc"), "failed")

    def test_github_bypass_uses_reviewed_sha_and_denial_waits_without_repair(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["provider"] = "github"
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["pullRequests"][assignment["id"]] = {"number": 1, "state": "OPEN"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", {"number": 1, "state": "OPEN", "url": "x", "headRefOid": "abc"})
            merged_pr = {"number": 1, "state": "MERGED", "url": "x", "headRefOid": "abc"}
            with patch("run.wait_for_checks", return_value="bypassable"), patch("run.pr_inspect", return_value=merged_pr), patch("run.provider_with_retries", return_value=subprocess.CompletedProcess([], 0, "", "")) as provider:
                self.assertTrue(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 1}, "abc"))
            self.assertIn("--admin", provider.call_args.args)
            self.assertEqual(provider.call_args.args[provider.call_args.args.index("--match-head-commit") + 1], "abc")

            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["pullRequests"][assignment["id"]]["state"] = "OPEN"
            with patch("run.wait_for_checks", return_value="bypassable"), patch("run.provider_with_retries", side_effect=RuntimeError("denied")), patch("run.invoke_with_replacements") as worker:
                self.assertFalse(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 1}, "abc"))
            self.assertEqual(store.state["taskStates"][assignment["id"]]["phase"], "waiting-provider")
            self.assertIn("provider.log", store.state["taskStates"][assignment["id"]]["providerStatus"])
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["repairAttemptsStarted"], 0)
            worker.assert_not_called()

    def test_github_unblocked_merge_retains_non_admin_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            with patch("run.provider_with_retries") as provider:
                run.pr_merge(store, "merge", 1, subject="test/TASK-0001: title", body="Relay-Candidate: abc")
            self.assertNotIn("--admin", provider.call_args.args)
            self.assertNotIn("--match-head-commit", provider.call_args.args)
            self.assertEqual(provider.call_args.args[provider.call_args.args.index("--subject") + 1], "test/TASK-0001: title")
            self.assertEqual(provider.call_args.args[provider.call_args.args.index("--body") + 1], "Relay-Candidate: abc")
            store.state["mergeMethod"] = "rebase"
            with patch("run.provider_with_retries") as provider:
                run.pr_merge(store, "rebase", 1, subject="ignored", body="ignored")
            self.assertNotIn("--subject", provider.call_args.args)

    def test_azure_sha_drift_and_merge_modes(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            drift = subprocess.CompletedProcess([], 0, json.dumps(self.azure_pr(sha="changed")), "")
            with patch("run.run_tool", return_value=drift):
                self.assertEqual(run.wait_for_checks(store, "DRIFT", {"number": 7}, "abc"), "sha-drift")
            with patch("run.provider_with_retries") as provider:
                run.pr_merge(store, "merge", 7, subject="campaign/TASK-0001: title", body="Relay-Candidate: abc")
                self.assertIn("true", provider.call_args.args)
                message = provider.call_args.args[provider.call_args.args.index("--merge-commit-message") + 1]
                self.assertIn("Relay-Candidate: abc", message)
                store.state["mergeMethod"] = "merge"
                run.pr_merge(store, "merge-2", 7)
                self.assertIn("false", provider.call_args.args)
                self.assertIn("--delete-source-branch", provider.call_args.args)

    def test_azure_rebase_is_rejected_during_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root, "rebase")
            store.state.pop("provider")
            remote = subprocess.CompletedProcess([], 0, "https://dev.azure.com/org/project/_git/repo\n", "")
            with patch("run.git", return_value=remote), patch("run.provider_with_retries") as provider, self.assertRaisesRegex(RuntimeError, "rebase"):
                run.provider_preflight(store)
            provider.assert_not_called()

    def test_ready_tasks_respect_dependencies_priority_and_paths(self):
        tasks = [ContractTests().task("TASK-0002", ["TASK-0001"]), ContractTests().task("TASK-0001")]
        tasks[0]["priority"] = "P0"
        self.assertEqual([item["id"] for item in run.ready_tasks(tasks, set())], ["TASK-0001"])
        self.assertEqual([item["id"] for item in run.ready_tasks(tasks, {"TASK-0001"}, {"src/file.py"})], [])

    def test_one_hundred_restarts_cannot_restore_review_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["reviewSessions"]["TASK-0001"] = {"phase": "repair-1", "reviewCallsStarted": 3, "reviewCallLimit": 9, "repairAttemptsStarted": 0}
            store.save()
            for _ in range(100):
                state = json.loads(store.path.read_text(encoding="utf-8"))
                store = run.StateStore(store.path, state)
                session = state["reviewSessions"]["TASK-0001"]
                if session["phase"] == "needs-user":
                    continue
                if session["phase"].startswith("repair-"):
                    session["repairAttemptsStarted"] += 1
                    store.save()
                    run._consume_agent_call(store, "TASK-0001", "worker", "repair", True, False)
                    number = session["repairAttemptsStarted"]
                    run.transition_review(session, f"verify-{number}", 2)
                    store.save()
                elif session["phase"].startswith("verify-"):
                    run._consume_agent_call(store, "TASK-0001", "verification-reviewer", None, True, False)
                    number = session["repairAttemptsStarted"]
                    run.transition_review(session, f"repair-{number + 1}" if number < 2 else "needs-user", 2)
                    store.save()
            final = json.loads(store.path.read_text(encoding="utf-8"))["reviewSessions"]["TASK-0001"]
            self.assertEqual(final["phase"], "needs-user")
            self.assertEqual(final["repairAttemptsStarted"], 2)
            self.assertLessEqual(final["reviewCallsStarted"], final["reviewCallLimit"])
            self.assertEqual(run.parse_tasks((Path(root) / "tasks.md").read_text(encoding="utf-8"), runtime=True)[1][0]["fixLoop"], 2)

    def test_attempt_counter_is_persisted_before_launch_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            for _ in range(3):
                run._consume_agent_call(store, "TASK-0001", "worker", "task", False, False)
            with self.assertRaises(RuntimeError):
                run._consume_agent_call(store, "TASK-0001", "worker", "task", False, False)
            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["attemptCounters"]["TASK-0001"], 3)
            self.assertEqual(run.parse_tasks((Path(root) / "tasks.md").read_text(encoding="utf-8"), runtime=True)[1][0]["attempt"], 3)

    def test_cleanup_preview_removes_nothing_and_incomplete_refused(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / ".relay").mkdir()
            state = {"repository": str(root), "phase": "build", "activeProcesses": {}, "worktrees": {}, "pullRequests": {}, "campaignId": "x"}
            (root / ".relay" / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                run.permanent_cleanup(root, False)
            self.assertTrue((root / ".relay" / "state.json").exists())

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                run.safe_within(Path(root).parent / "outside", Path(root))

    def test_permanently_pending_provider_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["providerCheckTimeoutSeconds"] = .01
            pending = subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": "abc", "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"status": "IN_PROGRESS"}], "state": "OPEN"}), "")
            with patch("run.run_tool", return_value=pending):
                self.assertEqual(run.wait_for_checks(store, "TASK-0001", {"number": 1}, "abc"), "waiting-provider")
            self.assertGreaterEqual(store.state["providerOperationsStarted"], 1)
            self.assertLessEqual(store.state["providerAttemptCounters"].get("TASK-0001:check-errors", 0), 3)

    def test_reviewed_sha_drift_prevents_merge(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            drift = subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": "changed", "mergeStateStatus": "CLEAN", "statusCheckRollup": [], "state": "OPEN"}), "")
            with patch("run.run_tool", return_value=drift):
                self.assertEqual(run.wait_for_checks(store, "TASK-0001", {"number": 1}, "reviewed"), "sha-drift")

    def test_unknown_mergeability_polls_within_deadline(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            unknown = subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": "sha", "mergeStateStatus": "UNKNOWN", "statusCheckRollup": [], "state": "OPEN"}), "")
            clean = subprocess.CompletedProcess([], 0, json.dumps({"headRefOid": "sha", "mergeStateStatus": "CLEAN", "statusCheckRollup": [], "state": "OPEN"}), "")
            with patch("run.run_tool", side_effect=[unknown, clean]) as provider, patch("run.time.time", side_effect=[100, 101, 102]), patch("run.time.sleep"):
                self.assertEqual(run.wait_for_checks(store, "TASK-0001", {"number": 1}, "sha"), "passed")
            self.assertEqual(provider.call_count, 2)

    def test_provider_failure_repair_uses_shared_fix_and_review_budgets(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["worktrees"][assignment["id"]] = {"path": root, "branch": "relay/TASK-0001", "baseSha": "base"}
            store.state["pullRequests"][assignment["id"]] = {"number": 1, "state": "OPEN"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "old", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            completed = subprocess.CompletedProcess([], 0, "base\n", "")
            def agent(*args, **kwargs):
                role = args[4]
                store.state["reviewSessions"][assignment["id"]]["reviewCallsStarted"] += 1
                if role == "worker":
                    return {"mode": "repair", "assignmentId": assignment["id"], "status": "candidate", "candidateSha": "new", "validation": [], "summary": ""}
                return {"assignmentId": assignment["id"], "candidateSha": "new", "status": "resolved"}
            with patch("run.wait_for_checks", side_effect=["failed", "passed"]), patch("run.git", return_value=completed), patch("run.invoke_with_replacements", side_effect=agent), patch("run.validate_candidate", return_value="new"), patch("run.publish_candidate"), patch("run.validate_publication_proof"), patch("run.inspect_merged_pr", return_value={"number": 1, "state": "MERGED", "url": "x", "headRefOid": "new"}), patch("run.provider_with_retries", return_value=completed), patch("run.mark_integrated"):
                self.assertTrue(run.merge_assignment(store, __import__("threading").Semaphore(1), assignment, Path(root), "relay/TASK-0001", {"number": 1}, "old"))
            session = store.state["reviewSessions"][assignment["id"]]
            self.assertEqual((session["repairAttemptsStarted"], session["reviewCallsStarted"], session["reviewedSha"]), (1, 5, "new"))
            self.assertEqual(session["phase"], "approved")

    def test_existing_pr_and_merged_pr_are_not_duplicated(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            pr = {"number": 1, "state": "OPEN", "url": "x", "headRefOid": "sha"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "sha", "pr": pr, "phase": "approved"}
            store.state["pullRequests"][assignment["id"]] = pr
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "sha", pr)
            with patch("run.pr_inspect", return_value=pr), patch("run.provider_call") as provider:
                self.assertIs(run.publish_candidate(store, assignment, Path(root), "branch", "sha"), pr)
                provider.assert_not_called()
            merged = pr | {"state": "MERGED"}
            def already_merged(*_args):
                run.persist_pr_inspection(store, assignment["id"], merged)
                return "merged"
            with patch("run.wait_for_checks", side_effect=already_merged), patch("run.provider_with_retries") as merge:
                self.assertTrue(run.merge_assignment(store, __import__("threading").Semaphore(1), assignment, Path(root), "branch", pr, "sha"))
                merge.assert_not_called()

    def test_publication_proof_is_rejected_before_provider_merge(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            pr = {"number": 1, "state": "OPEN", "url": "x", "headRefOid": "abc"}
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", pr)
            store.state["taskStates"][assignment["id"]]["publicationProof"]["candidateSha"] = "stale"
            with patch("run.wait_for_checks", return_value="passed"), patch("run.pr_merge") as merge, patch("run.mark_integrated") as integrated, self.assertRaisesRegex(RuntimeError, "publication proof"):
                run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", pr, "abc")
            merge.assert_not_called()
            integrated.assert_not_called()

    def test_provider_already_merged_is_refreshed_and_reconciled_without_merge(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            opened = {"number": 9, "state": "OPEN", "url": "x", "headRefOid": "abc"}
            merged = opened | {"state": "MERGED"}
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", opened)
            store.state["taskStates"][assignment["id"]].pop("prMetadata")
            def provider_merged(*_args):
                run.persist_pr_inspection(store, assignment["id"], merged)
                return "merged"
            with patch("run.wait_for_checks", side_effect=provider_merged), patch("run.pr_edit") as edit, patch("run.pr_merge") as merge:
                self.assertTrue(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", opened, "abc"))
            edit.assert_called_once()
            merge.assert_not_called()
            self.assertEqual(store.state["taskStates"][assignment["id"]]["providerProof"]["mergedProviderRecord"], merged)

    def test_canonical_pr_metadata_is_qualified_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().backlog_task()
            pr = {"number": 8, "state": "OPEN", "url": "x", "headRefOid": "abc"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "abc", "pr": pr, "phase": "approved"}
            title, body, digest = run.canonical_pr_metadata(store.state, assignment, "abc")
            self.assertEqual(title, "test/TASK-0001 [source origin/BUG-0001]: Do the thing")
            for value in ("Campaign:", "Qualified assignment:", "Source reference:", "Current candidate SHA:", "## Test paths", "## Regression validation", "## Campaign validation"):
                self.assertIn(value, body)
            with patch("run.pr_inspect", return_value=pr), patch("run.pr_edit") as edit:
                run.publish_candidate(store, assignment, Path(root), "branch", "abc")
                edit.assert_called_once()
            self.assertEqual(store.state["taskStates"][assignment["id"]]["prMetadata"]["hash"], digest)
            with patch("run.pr_inspect", return_value=pr), patch("run.pr_edit") as edit:
                run.publish_candidate(store, assignment, Path(root), "branch", "abc")
                edit.assert_not_called()
            store.state["taskStates"][assignment["id"]].pop("prMetadata")
            with patch("run.pr_inspect", return_value=pr), patch("run.pr_edit") as edit:
                run.publish_candidate(store, assignment, Path(root), "branch", "abc")
                edit.assert_called_once()

    def test_publish_ignores_historical_pr_for_reused_branch(self):
        cases = (("github", 24, "e4086c53", "fcd0513f"), ("azure-devops", 25, "eefb0f87", "920d47a5"))
        for provider, number, historical_sha, candidate_sha in cases:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                store = self.state_store(root)
                store.state["provider"] = provider
                assignment = ContractTests().task()
                store.state["taskStates"][assignment["id"]] = {"pushedSha": candidate_sha, "phase": "approved"}
                old = {"number": number, "state": "MERGED", "url": "old", "headRefOid": historical_sha}
                new = {"number": number + 7, "state": "OPEN", "url": "new", "headRefOid": candidate_sha}
                with patch("run.pr_discover", return_value=[old]), patch("run.pr_create", return_value=new) as create, patch("run.pr_inspect", return_value=new), patch("run.pr_edit"):
                    self.assertEqual(run.publish_candidate(store, assignment, Path(root), "relay/TASK-0001", candidate_sha), new)
                create.assert_called_once()

    def test_repaired_push_refreshes_cached_pr_sha(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            stale = {"number": 30, "state": "OPEN", "url": "old", "headRefOid": "f63189a5"}
            fresh = stale | {"headRefOid": "60d09d4a"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "f63189a5", "pr": stale, "phase": "approved"}
            completed = subprocess.CompletedProcess([], 0, "f63189a5\trefs/heads/relay/TASK-0001\n", "")
            with patch("run.git_provider_with_retries", return_value=completed) as git_provider, patch("run.pr_inspect", return_value=fresh) as inspect, patch("run.pr_edit") as edit:
                self.assertEqual(run.publish_candidate(store, assignment, Path(root), "relay/TASK-0001", "60d09d4a"), fresh)
            inspect.assert_called_once_with(store, "TASK-0001:pr-refresh:60d09d4a", 30)
            edit.assert_called_once()
            self.assertEqual(store.state["taskStates"][assignment["id"]]["prMetadata"]["candidateSha"], "60d09d4a")
            self.assertIn("--force-with-lease=refs/heads/relay/TASK-0001:f63189a5", git_provider.call_args_list[1].args)

    def test_publication_polls_stale_head_and_persists_each_live_inspection(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            stale = {"number": 30, "state": "OPEN", "url": "x", "headRefOid": "old"}
            fresh = stale | {"headRefOid": "new"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "new", "pr": stale, "phase": "approved"}
            persisted = []
            original = run.persist_pr_inspection
            def persist(*args):
                original(*args)
                persisted.append(store.state["taskStates"][assignment["id"]]["pr"])
            with patch("run.pr_inspect", side_effect=[stale, fresh]) as inspect, patch("run.persist_pr_inspection", side_effect=persist), patch("run.pr_edit"), patch("run.time.sleep"):
                self.assertEqual(run.publish_candidate(store, assignment, Path(root), "branch", "new"), fresh)
            self.assertEqual(inspect.call_count, 2)
            self.assertEqual(persisted, [stale, fresh])
            self.assertEqual(store.state["taskStates"][assignment["id"]]["publicationProof"], {"candidateSha": "new", "providerRecord": fresh})

    def test_matching_pushed_sha_still_refreshes_stale_publication_proof(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            stale = {"number": 4, "state": "OPEN", "url": "x", "headRefOid": "old"}
            fresh = stale | {"headRefOid": "new"}
            store.state["taskStates"][assignment["id"]] = {
                "pushedSha": "new", "pr": stale, "phase": "approved",
                "publicationProof": {"candidateSha": "old", "providerRecord": stale},
            }
            with patch("run.git_provider_with_retries") as push, patch("run.pr_inspect", return_value=fresh) as inspect, patch("run.pr_edit"):
                self.assertEqual(run.publish_candidate(store, assignment, Path(root), "branch", "new"), fresh)
            push.assert_not_called()
            inspect.assert_called_once()

    def test_push_before_pr_refresh_resumes_refresh_without_repush(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            stale = {"number": 30, "state": "OPEN", "url": "old", "headRefOid": "old"}
            fresh = stale | {"headRefOid": "new"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "new", "pr": stale, "phase": "approved"}
            with patch("run.git_provider_with_retries") as git_provider, patch("run.pr_inspect", return_value=fresh), patch("run.pr_edit"):
                self.assertEqual(run.publish_candidate(store, assignment, Path(root), "branch", "new"), fresh)
            git_provider.assert_not_called()

    def test_publish_rejects_open_pr_for_different_sha(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "new", "phase": "approved"}
            stale = {"number": 1, "state": "OPEN", "url": "stale", "headRefOid": "old"}
            with patch("run.pr_discover", return_value=[stale]), patch("run.pr_create") as create, self.assertRaisesRegex(RuntimeError, "source commit"):
                run.publish_candidate(store, assignment, Path(root), "relay/TASK-0001", "new")
            create.assert_not_called()

    def test_agent_and_provider_timeouts_consume_prelaunch_counters(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["targetInstructions"] = "custom target rules"
            def timeout(*args, **kwargs):
                persisted = json.loads(store.path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["attemptCounters"]["TASK-0001"], 1)
                self.assertEqual(persisted["taskStates"]["TASK-0001"]["operation"], "worker")
                raise subprocess.TimeoutExpired("codex", 1)
            store.state["taskStates"]["TASK-0001"] = {"phase": "implementing"}
            with patch("run.bounded_run", side_effect=timeout) as command, self.assertRaises(RuntimeError):
                run.invoke_agent(store, __import__("threading").Semaphore(1), Path(root), "TASK-0001", "worker", "prompt", mode="task")
            self.assertTrue(command.call_args.kwargs["input"].startswith("Target repository instructions:\ncustom target rules\n\n"))
            self.assertNotEqual(command.call_args.kwargs["env"].get("PYTHONUSERBASE"), os.environ.get("PYTHONUSERBASE"))
            self.assertEqual(store.state["attemptCounters"]["TASK-0001"], 1)
            self.assertEqual(store.state["activeProcesses"], {})
            with patch("run.run_tool", side_effect=subprocess.TimeoutExpired("gh", 1)), self.assertRaises(subprocess.TimeoutExpired):
                run.provider_call(store, "operation", "repo", "view")
            self.assertEqual(store.state["providerAttemptCounters"]["operation"], 1)

    def test_all_agent_roles_have_bounded_prompt_and_schema(self):
        self.assertEqual(set(run.ROLE_JSON_SCHEMAS), set(run.AGENT_SCHEMAS))
        self.assertNotIn("implementer", run.ROLE_JSON_SCHEMAS)
        self.assertNotIn("repairer", run.ROLE_JSON_SCHEMAS)
        self.assertIn('literal string "candidate"', run.worker_prompt("task", ContractTests().task()))
        self.assertIn("AUDIT-NNNN", run.ROLE_PROMPTS["audit-planner"])

    def test_stale_coordinator_lock_is_reconciled(self):
        with tempfile.TemporaryDirectory() as root:
            relay = Path(root); (relay / "coordinator.lock").write_text("99999999\n", encoding="utf-8")
            with run.coordinator_lock(relay):
                pass
            self.assertEqual(int((relay / "coordinator.lock").read_text()), os.getpid())

    def test_active_windows_safe_lock_probe_does_not_signal_owner(self):
        with tempfile.TemporaryDirectory() as root:
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; from pathlib import Path; import run;\nwith run.coordinator_lock(Path(sys.argv[1])):\n print('ready', flush=True); sys.stdin.readline()", root],
                cwd=Path(run.__file__).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(RuntimeError):
                    with run.coordinator_lock(Path(root)):
                        pass
                self.assertIsNone(child.poll())
            finally:
                if child.poll() is None:
                    child.stdin.write("\n"); child.stdin.flush(); child.wait(timeout=5)
                child.stdin.close(); child.stdout.close()

    def test_pending_ledger_operation_is_replayed(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            updated = (Path(root) / "tasks.md").read_text(encoding="utf-8").replace("- Status: ready", "- Status: integrated")
            store.state["pendingLedgerOperation"] = {"ledger": "tasks.md", "content": updated, "sha256": __import__("hashlib").sha256(updated.encode()).hexdigest()}
            store.save()
            run.reconcile(store)
            self.assertIn("- Status: integrated", (Path(root) / "tasks.md").read_text(encoding="utf-8"))
            self.assertIsNone(store.state["pendingLedgerOperation"])

    def test_markdown_is_resume_authority_not_state_task_copy(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["tasks"] = [{"id": "TASK-9999", "title": "stale"}]
            store.state["bugs"] = [{"id": "BUG-9999", "title": "stale"}]
            store.save()
            run.reconcile(store)
            self.assertEqual([task["id"] for task in run.load_tasks(store)], ["TASK-0001"])
            self.assertNotIn("tasks", store.state)
            self.assertNotIn("bugs", store.state)

    def test_tasks_ledger_provenance_must_match_campaign_state(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            task = ContractTests().backlog_task()
            Path(root, "tasks.md").write_text(plan.render_tasks([task], "0123456", "abc123", campaign_validation_commands=store.state["campaignValidationCommands"]), encoding="utf-8")
            store.state["taskProvenance"] = {task["id"]: {key: task[key] for key in ("sourceRef", "testPaths", "regressionValidationCommands")}}
            self.assertEqual(run.load_tasks(store)[0]["sourceRef"], "origin/BUG-0001")
            Path(root, "tasks.md").write_text(Path(root, "tasks.md").read_text(encoding="utf-8").replace("origin/BUG-0001", "origin/BUG-0002"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "provenance"):
                run.load_tasks(store)

    def test_complete_cleanup_preview_then_confirm(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve(); relay = root / ".relay"; relay.mkdir(); (root / ".git" / "info").mkdir(parents=True)
            campaign = "test"; marker = f"<!-- relay: campaign={campaign} repository={__import__('hashlib').sha256(str(root).encode()).hexdigest()[:12]} -->"
            (root / "tasks.md").write_text("# Tasks\n\n<!-- relay: planned-base=0123456 requirements=abc123 -->\n", encoding="utf-8")
            (root / "bugs.md").write_text(f"# Bugs\n\n{marker}\n", encoding="utf-8")
            relay_plan = plan.render_tasks([ContractTests().task()], "0123456", "abc123")
            (root / "PLAN.md").write_text(relay_plan, encoding="utf-8")
            (root / "PLAN.reviewed.md").write_text(relay_plan, encoding="utf-8")
            (root / "post-mvp-plan.md").write_text("keep plan\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("keep agents\n", encoding="utf-8")
            (root / "BACKLOG.md").write_text("keep backlog\n", encoding="utf-8")
            (root / ".git" / "info" / "exclude").write_text("tasks.md\nbugs.md\n.relay/\nkeep.me\n", encoding="utf-8")
            state = {"repository": str(root), "phase": "complete", "activeProcesses": {}, "worktrees": {}, "pullRequests": {}, "campaignId": campaign}
            (relay / "state.json").write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(run.permanent_cleanup(root, False), 0)
            self.assertTrue((root / "tasks.md").exists())
            self.assertTrue((root / "PLAN.md").exists())
            self.assertEqual(run.permanent_cleanup(root, True), 0)
            self.assertFalse(relay.exists())
            self.assertFalse((root / "PLAN.md").exists())
            self.assertFalse((root / "PLAN.reviewed.md").exists())
            self.assertEqual((root / "post-mvp-plan.md").read_text(encoding="utf-8"), "keep plan\n")
            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "keep agents\n")
            self.assertEqual((root / "BACKLOG.md").read_text(encoding="utf-8"), "keep backlog\n")
            self.assertEqual((root / ".git" / "info" / "exclude").read_text(encoding="utf-8"), "keep.me\n")

    def test_cleanup_refuses_missing_or_stale_provider_proof(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve()
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="complete", worktrees={}, activeProcesses={})
            store.state["taskStates"][assignment["id"]] = {"phase": "integrated", "candidateSha": "abc"}
            store.save()
            with self.assertRaisesRegex(RuntimeError, "provider proof"):
                run.permanent_cleanup(root, False)
            record = {"number": 1, "url": "x", "headRefOid": "abc", "state": "MERGED"}
            self.provider_metadata(store, assignment, "abc", record)
            _subject, _body, merge_hash = run.canonical_merge_metadata(store.state, assignment, "abc")
            metadata = store.state["taskStates"][assignment["id"]]["prMetadata"]
            store.state["taskStates"][assignment["id"]].update(
                phase="integrated", providerProof={
                    "finalCandidate": "abc", "sourceRef": None, "prMetadataHash": metadata["hash"],
                    "mergeMetadataHash": merge_hash, "mergedProviderRecord": record,
                },
            )
            store.save()
            self.assertEqual(run.permanent_cleanup(root, False), 0)
            store.state["taskStates"][assignment["id"]]["providerProof"]["finalCandidate"] = "old"
            store.save()
            with self.assertRaisesRegex(RuntimeError, "stale"):
                run.permanent_cleanup(root, False)

    def test_campaign_initialization_generates_only_missing_agents(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            args = run.parser().parse_args(["--repo", str(target)])
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123", campaign_validation_commands=["python -c \"print('baseline')\""])
            store, _ = run.initialize_campaign(target, text, args)
            self.assertEqual((target / "AGENTS.md").read_bytes(), repo.TARGET_AGENTS.encode())
            self.assertEqual(store.state["agentsBootstrap"]["phase"], "pending")
            self.assertEqual(store.state["agentsBootstrap"]["contentHash"], repo.TARGET_AGENTS_SHA256)
            self.assertEqual(store.state["schemaVersion"], run.STATE_SCHEMA_VERSION)

    def test_old_campaign_state_is_rejected_instead_of_recovered(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            relay = target / ".relay"; relay.mkdir()
            state = {"schemaVersion": 1, "repository": str(target)}
            (relay / "state.json").write_text(json.dumps(state), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("unsupported campaign state schema 1", completed.stderr)
            self.assertEqual(json.loads((relay / "state.json").read_text()), state)

    def test_custom_agents_is_honored_without_bootstrap(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            agents = target / "AGENTS.md"; agents.write_bytes(b"custom\r\n")
            args = run.parser().parse_args(["--repo", str(target)])
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123", campaign_validation_commands=["python -c \"print('baseline')\""])
            store, _ = run.initialize_campaign(target, text, args)
            self.assertEqual(agents.read_bytes(), b"custom\r\n")
            self.assertIsNone(store.state["agentsBootstrap"])
            self.assertEqual(store.state["targetInstructions"], "custom\r\n")

    def test_tracked_custom_agents_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            agents = target / "AGENTS.md"; agents.write_bytes(b"tracked custom\n")
            subprocess.run(["git", "-C", str(target), "add", "AGENTS.md"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "custom agents"], check=True, capture_output=True)
            args = run.parser().parse_args(["--repo", str(target)])
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123", campaign_validation_commands=["python -c \"print('baseline')\""])
            store, _ = run.initialize_campaign(target, text, args)
            self.assertEqual(agents.read_bytes(), b"tracked custom\n")
            self.assertIsNone(store.state["agentsBootstrap"])

    def test_modified_generated_agents_requires_user(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            content = repo.TARGET_AGENTS + "customized\n"
            (target / "AGENTS.md").write_text(content, encoding="utf-8", newline="\n")
            args = run.parser().parse_args(["--repo", str(target)])
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123", campaign_validation_commands=["python -c \"print('baseline')\""])
            store, tasks = run.initialize_campaign(target, text, args)
            self.assertEqual(store.state["phase"], "needs-user")
            self.assertEqual(store.state["agentsBootstrap"]["providerStatus"], "generated-file-modified")
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), content)
            with patch("run.require_validation_shell"), patch("run.run_baseline_validation", return_value=True), patch("run.provider_preflight") as preflight, patch("run.run_assignments") as workers:
                self.assertEqual(run.execute_campaign(store, tasks), 2)
            preflight.assert_not_called()
            workers.assert_not_called()


class FakeEndToEndTests(unittest.TestCase):
    def test_nested_target_keeps_bootstrap_and_worker_changes_in_subdirectory(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            module = target / "module"; module.mkdir(); (module / "README.md").write_text("# Module\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(target), "add", "module/README.md"], check=True)
            subprocess.run(["git", "-C", str(target), "commit", "-m", "module"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(target), "push", "origin", "main"], check=True, capture_output=True)
            fake_codex, fake_gh = root / "fake_codex.py", root / "fake_gh.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8"); fake_gh.write_text(FAKE_GH, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            requirements = root / "requirements.md"; requirements.write_text("Create one file.", encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {"RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}", "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_GH_STATE": str(provider)}
            subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(module), "--requirements", str(requirements)], capture_output=True, text=True, env=environment, check=True)
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(module), "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"], capture_output=True, text=True, env=environment, timeout=60)
            state = json.loads((module / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + json.dumps(state, indent=2))
            self.assertEqual(state["repositoryPrefix"], "module")
            self.assertEqual(state["worktrees"], {})
            self.assertEqual(git_output(target, "show", "origin/main:module/AGENTS.md"), repo.TARGET_AGENTS)
            task_branch = state["taskStates"]["TASK-0001"]["branch"]
            self.assertEqual(git_output(target, "show", f"origin/{task_branch}:module/one.txt"), "TASK-0001\n")
            self.assertNotIn("one.txt", git_output(target, "ls-tree", "--name-only", f"origin/{task_branch}"))
            exclude = (target / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn("module/.relay/", exclude)
            self.assertEqual(run.permanent_cleanup(module, True), 0)
            self.assertNotIn("module/.relay/", (target / ".git" / "info" / "exclude").read_text(encoding="utf-8"))

    def test_azure_campaign_bootstrap_pr_task_pr_and_complete(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            azure_url = "https://dev.azure.com/org/project/_git/repo"
            remote = root / "remote.git"
            subprocess.run(["git", "-C", str(target), "remote", "set-url", "origin", azure_url], check=True)
            subprocess.run(["git", "-C", str(target), "config", f"url.{remote}.insteadOf", azure_url], check=True)
            fake_codex, fake_az = root / "fake_codex.py", root / "fake_az.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8"); fake_az.write_text(FAKE_AZ, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            requirements = root / "requirements.md"; requirements.write_text("Create one file.", encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {
                "RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_AZ": f"{sys.executable} {fake_az}",
                "FAKE_AZ_STATE": str(provider), "FAKE_AZ_REMOTE": str(remote),
            }
            subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements)], capture_output=True, text=True, env=environment, check=True)
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"], capture_output=True, text=True, env=environment, timeout=60)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + json.dumps(state, indent=2))
            self.assertEqual((state["provider"], state["azureOrganization"], state["azureProject"], state["azureRepository"]), ("azure-devops", "org", "project", "repo"))
            self.assertEqual(state["phase"], "complete")
            self.assertEqual(state["baselineValidation"]["phase"], "passed")
            self.assertEqual(state["baselineValidation"]["commandsStarted"], 1)
            self.assertEqual(state["validationCommandsStarted"]["TASK-0001:campaign"], 1)
            self.assertIn("SUMMARY    tasks=1/1 completed", completed.stderr)
            self.assertIn("SUMMARY    - BASELINE passed:", completed.stderr)
            self.assertIn("NEXT       Preview cleanup with:", completed.stderr)
            self.assertIn("then confirm with:", completed.stderr)
            self.assertIn("--cleanup --confirm", completed.stderr)
            self.assertFalse((target / "BACKLOG.md").exists())
            self.assertEqual(len(list(provider.glob("*.json"))), 2)
            records = [json.loads(path.read_text()) for path in provider.glob("*.json")]
            self.assertEqual([record.get("operations", []) for record in records if "agents-bootstrap" not in record["branch"]], [["provider-approve"]])
            self.assertNotIn("provider-approve", next(record for record in records if "agents-bootstrap" in record["branch"]).get("operations", []))

    def test_bootstrap_timeout_resumes_without_duplicate_pr_or_task_launch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            fake_codex, fake_gh = root / "fake_codex.py", root / "fake_gh.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8"); fake_gh.write_text(FAKE_GH, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            requirements = root / "requirements.md"; requirements.write_text("Create one file.", encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {"RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}", "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_GH_STATE": str(provider), "FAKE_BOOTSTRAP_PENDING": "1"}
            subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements)], capture_output=True, text=True, env=environment, check=True)
            command = [sys.executable, str(Path(run.__file__)), "--repo", str(target), "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "1"]
            first = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual((first.returncode, state["phase"], state["agentsBootstrap"]["phase"], state["taskStates"]), (2, "waiting-provider", "waiting-provider", {}))
            resumed = subprocess.run(command, capture_output=True, text=True, env={key: value for key, value in environment.items() if key != "FAKE_BOOTSTRAP_PENDING"}, timeout=30)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr + json.dumps(state, indent=2))
            self.assertEqual(state["providerAttemptCounters"]["AGENTS:pr-create"], 1)

    def test_exhausted_azure_bootstrap_pr_stays_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            azure_url = "https://dev.azure.com/org/project/_git/repo"
            remote = root / "remote.git"
            subprocess.run(["git", "-C", str(target), "remote", "set-url", "origin", azure_url], check=True)
            subprocess.run(["git", "-C", str(target), "config", f"url.{remote}.insteadOf", azure_url], check=True)
            fake_codex, fake_az = root / "fake_codex.py", root / "fake_az.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8"); fake_az.write_text(FAKE_AZ, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            requirements = root / "requirements.md"; requirements.write_text("Create one file.", encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {
                "RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_AZ": f"{sys.executable} {fake_az}",
                "FAKE_AZ_STATE": str(provider), "FAKE_AZ_REMOTE": str(remote), "FAKE_AZ_FAIL_CREATE_ATTEMPTS": "6",
            }
            subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements)], capture_output=True, text=True, env=environment, check=True)
            command = [sys.executable, str(Path(run.__file__)), "--repo", str(target), "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"]
            first = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
            second = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=30)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual((first.returncode, second.returncode), (2, 2))
            self.assertEqual((state["phase"], state["agentsBootstrap"]["phase"]), ("needs-user", "needs-user"))
            self.assertEqual(state["providerAttemptCounters"]["AGENTS:pr-create"], 3)
            self.assertEqual((provider / ".create-attempts").read_text(), "3")
            self.assertEqual(state["taskStates"], {})
            self.assertNotIn("RECOVER", second.stderr)
            self.assertIn("category=provider-publication affected=AGENTS reason=Azure DevOps PR creation exhausted attempts", second.stderr)

    def test_parallel_workers_reviews_prs_merge_one_audit_and_complete(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = make_git_repository(root)
            fake_codex, fake_gh = root / "fake_codex.py", root / "fake_gh.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
            fake_gh.write_text(FAKE_GH, encoding="utf-8")
            events, provider = root / "events.log", root / "provider"
            provider.mkdir()
            requirements = root / "requirements.md"; requirements.write_text("Create two independent files.", encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {
                "RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}",
                "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_EVENTS": str(events), "FAKE_GH_STATE": str(provider), "FAKE_AUDIT_SCOPE": "1", "FAKE_TWO_TASKS": "1", "FAKE_REQUIRE_TARGET_INSTRUCTIONS": "1", "FAKE_GH_BLOCKED": "1",
            }
            planned = subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements), "--workers", "2"], capture_output=True, text=True, env=environment, check=True)
            self.assertTrue(Path(planned.stdout.strip()).samefile(target / "PLAN.md"))
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--workers", "2", "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"], capture_output=True, text=True, env=environment, timeout=60)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + json.dumps(state, indent=2))
            self.assertEqual(state["phase"], "complete")
            self.assertNotIn("tasks", state)
            self.assertNotIn("bugs", state)
            self.assertTrue(state["auditPlanStarted"] and state["auditPlanCompleted"] and state["auditTriageCompleted"])
            self.assertEqual(len(state["auditScopes"]), 1)
            self.assertTrue(next(iter(state["auditScopes"].values()))["completed"])
            self.assertLessEqual(state["auditCallsStarted"], state["auditCallLimit"])
            self.assertEqual(state["worktrees"], {})
            self.assertEqual(state["agentsBootstrap"]["phase"], "complete")
            self.assertNotIn("AGENTS", state["reviewSessions"])
            self.assertTrue(all(value["phase"] == "integrated" for value in state["taskStates"].values()))
            self.assertTrue(all(session["initialReviewAssignmentsStarted"] == 2 and session["initialReviewAssignmentsCompleted"] == 2 and session["triageCompleted"] and session["reviewCallsStarted"] == 3 for session in state["reviewSessions"].values()))
            spans = {task: {action: float(Path(f"{events}.{task}.{action}").read_text()) for action in ("start", "end")} for task in ("TASK-0001", "TASK-0002")}
            self.assertLess(max(spans[task]["start"] for task in spans), min(spans[task]["end"] for task in spans))
            self.assertEqual(len(list(provider.glob("*.json"))), 3)
            records = [json.loads(path.read_text()) for path in provider.glob("*.json")]
            self.assertTrue(all(record.get("operations") == ["merge-bypass"] for record in records if "agents-bootstrap" not in record["branch"]))
            self.assertEqual(next(record for record in records if "agents-bootstrap" in record["branch"])["operations"], ["merge"])
            self.assertEqual(state["providerAttemptCounters"]["AGENTS:pr-create"], 1)
            self.assertEqual(git_output(target, "diff", "--name-only", "HEAD^..HEAD").splitlines(), ["AGENTS.md"])
            self.assertTrue(all(state["providerAttemptCounters"][f"{task}:pr-create"] == 1 for task in ("TASK-0001", "TASK-0002")))
            self.assertIn("--repo', 'fake/relay", (target / ".relay" / "logs" / "provider.log").read_text(encoding="utf-8"))
            before = (target / ".relay" / "state.json").read_bytes()
            shown = subprocess.run([sys.executable, str(Path(status.__file__)), "--repo", str(target)], capture_output=True, text=True, check=True)
            self.assertIn("Review sessions", shown.stdout)
            self.assertIn("AGENTS.md bootstrap", shown.stdout)
            self.assertEqual((target / ".relay" / "state.json").read_bytes(), before)

    def test_accepted_audit_bug_runs_worker_bug_review_pr_merge_and_stops(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            fake_codex, fake_gh = root / "fake_codex.py", root / "fake_gh.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8"); fake_gh.write_text(FAKE_GH, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            requirements = root / "requirements.md"; requirements.write_text("Create one file and audit it.", encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {
                "RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}",
                "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_GH_STATE": str(provider), "FAKE_AUDIT_SCOPE": "1", "FAKE_AUDIT_BUG": "1",
            }
            subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements)], capture_output=True, text=True, env=environment, check=True)
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"], capture_output=True, text=True, env=environment, timeout=60)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + json.dumps(state, indent=2))
            self.assertEqual((state["phase"], state["taskStates"]["BUG-0001"]["mode"], state["taskStates"]["BUG-0001"]["phase"]), ("complete", "bug", "integrated"))
            self.assertEqual(state["auditCallsStarted"], 3)
            self.assertEqual(run.parse_bugs((target / "bugs.md").read_text(encoding="utf-8"))[1][0]["status"], "resolved")
            self.assertEqual(len(list(provider.glob("*.json"))), 3)

    def test_backlog_lifecycle_preserves_provenance_tests_provider_proof_and_backlog(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            bug = ContractTests().backlog_bug()
            bug.update(location="one.txt:1", allowedPaths=["one.txt"])
            backlog = target / "BACKLOG.md"
            backlog.write_text(run.render_backlog("origin", target, [bug]), encoding="utf-8")
            fake_codex, fake_gh = root / "fake_codex.py", root / "fake_gh.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8"); fake_gh.write_text(FAKE_GH, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            environment = os.environ | VALIDATION_ENV | {
                "RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}",
                "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_GH_STATE": str(provider), "FAKE_BACKLOG": "1",
            }
            subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(backlog)], capture_output=True, text=True, env=environment, check=True)
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"], capture_output=True, text=True, env=environment, timeout=60)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + json.dumps(state, indent=2))
            task = run.parse_tasks((target / "tasks.md").read_text(encoding="utf-8"), runtime=True)[1][0]
            self.assertEqual((task["sourceRef"], task["testPaths"]), ("origin/BUG-0001", ["test_one.txt"]))
            task_state = state["taskStates"]["TASK-0001"]
            self.assertEqual(task_state["providerProof"]["sourceRef"], "origin/BUG-0001")
            self.assertEqual(task_state["providerProof"]["finalCandidate"], task_state["candidateSha"])
            record = next(json.loads(path.read_text()) for path in provider.glob("*.json") if "agents-bootstrap" not in path.name)
            self.assertIn("[source origin/BUG-0001]", record["title"])
            self.assertIn(task_state["candidateSha"], record["body"])
            self.assertIn("Relay-Source: origin/BUG-0001", record["mergeBody"])
            shown = subprocess.run([sys.executable, str(Path(status.__file__)), "--repo", str(target)], capture_output=True, text=True, check=True)
            self.assertIn(f"{state['campaignId']}/TASK-0001: sourceRef=origin/BUG-0001", shown.stdout)
            cleanup = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--cleanup", "--confirm"], capture_output=True, text=True)
            self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
            self.assertTrue(backlog.is_file())
            self.assertFalse((target / ".relay").exists())

    def test_adversarial_review_terminates_at_shared_repair_budget(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = make_git_repository(root)
            fake_codex, fake_gh = root / "fake_codex.py", root / "fake_gh.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
            fake_gh.write_text(FAKE_GH, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            task = ContractTests().task()
            task["allowedPaths"] = ["file.txt"]
            task["validationCommands"] = [f'{sys.executable} -c "from pathlib import Path; assert Path(\'file.txt\').is_file()"']
            text = plan.render_tasks([task], git_output(target, "rev-parse", "HEAD").strip(), "abc123", campaign_validation_commands=["python -c \"print('baseline')\""])
            environment = os.environ | VALIDATION_ENV | {"RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}", "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_GH_STATE": str(provider), "FAKE_ADVERSARIAL": "1"}
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--workers", "2", "--fix-loops", "2", "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "1"], input=text, capture_output=True, text=True, env=environment, timeout=60)
            self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            review = state["reviewSessions"]["TASK-0001"]
            self.assertEqual((review["phase"], review["repairAttemptsStarted"]), ("needs-user", 2))
            self.assertLessEqual(review["reviewCallsStarted"], review["reviewCallLimit"])


def git_output(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], check=True, capture_output=True, text=True).stdout


def make_git_repository(root):
    target, remote = root / "target", root / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(target)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(target), "config", "user.name", "Relay Test"], check=True)
    subprocess.run(["git", "-C", str(target), "config", "user.email", "relay@example.invalid"], check=True)
    (target / "README.md").write_text("# Fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-m", "fixture"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(target), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return target


FAKE_CODEX = textwrap.dedent(r'''
import json, os, re, subprocess, sys, time
args = sys.argv[1:]
out = args[args.index("--output-last-message") + 1]
cwd = args[args.index("--cd") + 1]
prompt = sys.stdin.read()
if os.environ.get("FAKE_REQUIRE_TARGET_INSTRUCTIONS") and "<!-- relay: generated-target-instructions v1 -->" not in prompt:
    raise SystemExit("missing target instructions")
assignment = re.search(r"Assignment ID: ([A-Z]+-?\d*)", prompt)
assignment = assignment.group(1) if assignment else "AUDIT"
candidate = re.search(r"Candidate SHA: ([0-9a-f]+)", prompt)
candidate = candidate.group(1) if candidate else ""
if prompt.startswith("Role: Repository Scout"):
    scope = re.search(r"Inspect only this fixed scope: (.+)", prompt).group(1)
    schema = json.load(open(args[args.index("--output-schema") + 1], encoding="utf-8"))
    if schema["properties"]["scope"].get("enum") != [scope]: raise SystemExit("scout scope schema is not fixed")
    key = str(abs(hash(scope)))
    events = os.environ.get("FAKE_SCOUT_EVENTS")
    if events:
        with open(f"{events}.{key}.start", "w") as stream: stream.write(str(time.time()))
        time.sleep(.25)
        with open(f"{events}.{key}.end", "w") as stream: stream.write(str(time.time()))
    result = {"scope": scope, "implemented": [], "missing": ["work"], "conflicts": [], "relevantPaths": [], "validationCommands": [], "evidence": [scope]}
elif prompt.startswith("Role: Planning Project Manager"):
    if os.environ.get("FAKE_REQUIRE_EVIDENCE") and "Scout evidence:\n[]" in prompt: raise SystemExit("missing scout evidence")
    tasks = [{"id": "TASK-0001", "title": "Add one file", "status": "ready", "priority": "P1", "dependencies": [], "allowedPaths": ["one.txt"], "acceptanceCriteria": ["File exists."], "validationCommands": ["python -c \"from pathlib import Path; assert Path('one.txt').is_file()\""]}]
    if os.environ.get("FAKE_BACKLOG"):
        command = "python -c \"from pathlib import Path; assert Path('test_one.txt').is_file()\""
        tasks[0].update(allowedPaths=["one.txt", "test_one.txt"], sourceRef="origin/BUG-0001", testPaths=["test_one.txt"], regressionValidationCommands=[command], validationCommands=[command])
    if os.environ.get("FAKE_TWO_TASKS"):
        tasks.append({"id": "TASK-0002", "title": "Add another file", "status": "ready", "priority": "P1", "dependencies": [], "allowedPaths": ["two.txt"], "acceptanceCriteria": ["File exists."], "validationCommands": ["python -c \"from pathlib import Path; assert Path('two.txt').is_file()\""]})
    result = {"campaignValidationCommands": ["python -c \"print('baseline')\""], "tasks": tasks}
elif "Role: Worker" in prompt:
    mode = re.search(r"Mode: (task|bug|repair)", prompt).group(1)
    allowed = json.loads(re.search(r"Allowed paths: (\[[^\n]+\])", prompt).group(1))
    path = os.path.join(cwd, allowed[0])
    os.makedirs(os.path.dirname(path) or cwd, exist_ok=True)
    events = os.environ.get("FAKE_EVENTS")
    if events and mode == "task":
        with open(f"{events}.{assignment}.start", "w", encoding="utf-8") as stream: stream.write(str(time.time()))
        time.sleep(.35)
    content = assignment + (f" {mode} {time.time()}" if mode == "repair" else "") + "\n"
    with open(path, "w", encoding="utf-8") as stream: stream.write(content)
    changed = [allowed[0]]
    if os.environ.get("FAKE_BACKLOG") and mode == "task":
        with open(os.path.join(cwd, allowed[1]), "w", encoding="utf-8") as stream: stream.write("regression\n")
        changed.append(allowed[1])
    subprocess.run(["git", "-C", cwd, "add", *changed], check=True)
    subprocess.run(["git", "-C", cwd, "commit", "-m", assignment], check=True, capture_output=True)
    sha = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if events and mode == "task":
        with open(f"{events}.{assignment}.end", "w", encoding="utf-8") as stream: stream.write(str(time.time()))
    result = {"mode": mode, "assignmentId": assignment, "status": "candidate", "candidateSha": sha, "validation": [], "summary": "done"}
elif "Role: contract-reviewer" in prompt or "Role: risk-reviewer" in prompt:
    role = "contract-reviewer" if "Role: contract-reviewer" in prompt else "risk-reviewer"
    findings = []
    if os.environ.get("FAKE_ADVERSARIAL"):
        findings = [{"id": role, "severity": "P1", "location": "file.txt:1", "failure": "always fails", "reproduction": "python -c \"raise SystemExit(1)\"", "requirement": "must pass", "evidence": "deterministic failure", "candidateIntroduced": True}]
    result = {"assignmentId": assignment, "candidateSha": candidate, "findings": findings}
elif "Role: triage-pm" in prompt:
    if os.environ.get("FAKE_ADVERSARIAL") and assignment != "AUDIT":
        decisions = [{"findingId": role, "action": "accept-blocker", "reason": "reproduced"} for role in ("contract-reviewer", "risk-reviewer")]
    elif os.environ.get("FAKE_AUDIT_BUG") and assignment == "AUDIT":
        decisions = [{"findingId": "audit-bug", "action": "accept-blocker", "reason": "reproduced"}]
    else:
        decisions = []
    result = {"assignmentId": assignment, "decisions": decisions}
elif "Role: verification-reviewer" in prompt:
    result = {"assignmentId": assignment, "candidateSha": candidate, "status": "unresolved" if os.environ.get("FAKE_ADVERSARIAL") else "resolved"}
elif "Role: audit-planner" in prompt:
    paths = ["audit_fix.txt"] if os.environ.get("FAKE_AUDIT_BUG") else ["README.md"]
    result = {"scopes": [{"scopeId": "AUDIT-0001", "scope": "fixture", "requirements": ["fixture"], "paths": paths, "commands": ["python -c \"from pathlib import Path; assert Path('audit_fix.txt').is_file()\""] if os.environ.get("FAKE_AUDIT_BUG") else ["python -c \"print('audited')\""], "completionCondition": "scope inspected"}]} if os.environ.get("FAKE_AUDIT_SCOPE") else {"scopes": []}
elif "Role: audit-worker" in prompt:
    findings = [{"id": "audit-bug", "severity": "P1", "location": "audit_fix.txt:1", "failure": "audit fix is missing", "reproduction": "Observe that audit_fix.txt is absent.", "requirement": "audit fix exists", "evidence": "file absent", "candidateIntroduced": False}] if os.environ.get("FAKE_AUDIT_BUG") else []
    result = {"scopeId": assignment, "findings": findings}
else:
    raise SystemExit("unknown prompt")
with open(out, "w", encoding="utf-8") as stream: json.dump(result, stream)
''')


FAKE_GH = textwrap.dedent(r'''
import json, os, pathlib, re, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["FAKE_GH_STATE"])
def file_for(branch): return root / (branch.replace("/", "_") + ".json")
if args[:2] == ["auth", "status"] or args[:2] == ["repo", "view"]:
    raise SystemExit(0)
if args[:2] == ["pr", "list"]:
    branch = args[args.index("--head") + 1]; path = file_for(branch)
    print(json.dumps([json.loads(path.read_text())] if path.exists() else [])); raise SystemExit(0)
if args[:2] == ["pr", "create"]:
    branch = args[args.index("--head") + 1]; body = pathlib.Path(args[args.index("--body-file") + 1]).read_text()
    sha = (re.search(r"Current candidate SHA: `([0-9a-f]+)`", body) or re.search(r"Candidate: ([0-9a-f]+)", body)).group(1); number = int(re.search(r"(\d+)$", branch).group(1))
    file_for(branch).write_text(json.dumps({"number": number, "url": f"https://example.invalid/{number}", "headRefOid": sha, "state": "OPEN", "branch": branch, "title": args[args.index("--title") + 1], "body": body}))
    print(f"https://example.invalid/{number}"); raise SystemExit(0)
if args[:2] == ["pr", "edit"]:
    key = args[2]
    for path in root.glob("*.json"):
        record = json.loads(path.read_text())
        if str(record["number"]) == key:
            record.update(title=args[args.index("--title") + 1], body=pathlib.Path(args[args.index("--body-file") + 1]).read_text())
            path.write_text(json.dumps(record)); raise SystemExit(0)
if args[:2] == ["pr", "view"]:
    key = args[2]
    paths = list(root.glob("*.json")); records = [(path, json.loads(path.read_text())) for path in paths]
    path, record = next((item for item in records if item[1]["branch"] == key or str(item[1]["number"]) == key))
    if any("statusCheckRollup" in arg for arg in args):
        checks = [{"status": "IN_PROGRESS"}] if os.environ.get("FAKE_BOOTSTRAP_PENDING") and "agents-bootstrap" in record["branch"] else []
        blocked = os.environ.get("FAKE_GH_BLOCKED") and "agents-bootstrap" not in record["branch"]
        record.update(mergeStateStatus="BLOCKED" if blocked else "CLEAN", statusCheckRollup=checks)
    print(json.dumps(record)); raise SystemExit(0)
if args[:2] == ["pr", "merge"]:
    key = args[2]
    for path in root.glob("*.json"):
        record = json.loads(path.read_text())
        if str(record["number"]) == key:
            operation = "merge-bypass" if "--admin" in args else "merge"
            if operation == "merge-bypass" and args[args.index("--match-head-commit") + 1] != record["headRefOid"]: raise SystemExit("head mismatch")
            record.setdefault("operations", []).append(operation)
            record["mergeSubject"] = args[args.index("--subject") + 1] if "--subject" in args else None
            record["mergeBody"] = args[args.index("--body") + 1] if "--body" in args else None
            if "agents-bootstrap" in record["branch"]:
                remote = root.parent / "remote.git"
                subprocess.run(["git", "--git-dir", str(remote), "update-ref", "refs/heads/main", record["headRefOid"]], check=True)
            record["state"] = "MERGED"; path.write_text(json.dumps(record)); raise SystemExit(0)
raise SystemExit(f"unknown gh args: {args}")
''')


FAKE_AZ = textwrap.dedent(r'''
import json, os, pathlib, re, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["FAKE_AZ_STATE"])
def file_for(branch): return root / (branch.replace("/", "_") + ".json")
if args[:3] == ["devops", "project", "show"] or args[:2] == ["repos", "show"]:
    print("{}"); raise SystemExit(0)
if args[:3] == ["repos", "pr", "list"]:
    branch = args[args.index("--source-branch") + 1]; path = file_for(branch)
    print(json.dumps([json.loads(path.read_text())] if path.exists() else [])); raise SystemExit(0)
if args[:3] == ["repos", "pr", "create"]:
    branch = args[args.index("--source-branch") + 1]
    description = args[args.index("--description") + 1]
    attempts = root / ".create-attempts"
    attempt = int(attempts.read_text()) + 1 if attempts.exists() else 1
    attempts.write_text(str(attempt))
    if attempt <= int(os.environ.get("FAKE_AZ_FAIL_CREATE_ATTEMPTS", "0")):
        print("simulated create failure", file=sys.stderr); raise SystemExit(1)
    sha = (re.search(r"Current candidate SHA: `([0-9a-f]+)`", description) or re.search(r"Candidate: ([0-9a-f]+)", description)).group(1)
    number = int(re.search(r"(\d+)$", branch).group(1))
    record = {"pullRequestId": number, "status": "active", "mergeStatus": "succeeded", "lastMergeSourceCommit": {"commitId": sha}, "branch": branch, "title": args[args.index("--title") + 1], "description": description}
    file_for(branch).write_text(json.dumps(record)); print(json.dumps(record)); raise SystemExit(0)
if args[:3] == ["repos", "pr", "show"]:
    number = args[args.index("--id") + 1]
    record = next(json.loads(path.read_text()) for path in root.glob("*.json") if str(json.loads(path.read_text())["pullRequestId"]) == number)
    print(json.dumps(record)); raise SystemExit(0)
if args[:4] == ["repos", "pr", "policy", "list"]:
    print("[]"); raise SystemExit(0)
if args[:3] == ["repos", "pr", "set-vote"]:
    number = args[args.index("--id") + 1]
    for path in root.glob("*.json"):
        record = json.loads(path.read_text())
        if str(record["pullRequestId"]) == number:
            record.setdefault("operations", []).append("provider-approve"); path.write_text(json.dumps(record)); print(json.dumps(record)); raise SystemExit(0)
if args[:3] == ["repos", "pr", "update"]:
    number = args[args.index("--id") + 1]
    for path in root.glob("*.json"):
        record = json.loads(path.read_text())
        if str(record["pullRequestId"]) == number:
            if "--status" not in args:
                record.update(title=args[args.index("--title") + 1], description=args[args.index("--description") + 1]); path.write_text(json.dumps(record)); print(json.dumps(record)); raise SystemExit(0)
            if "agents-bootstrap" in record["branch"]:
                subprocess.run(["git", "--git-dir", os.environ["FAKE_AZ_REMOTE"], "update-ref", "refs/heads/main", record["lastMergeSourceCommit"]["commitId"]], check=True)
            record["status"] = "completed"; record["mergeMessage"] = args[args.index("--merge-commit-message") + 1] if "--merge-commit-message" in args else None; path.write_text(json.dumps(record)); print(json.dumps(record)); raise SystemExit(0)
raise SystemExit(f"unknown az args: {args}")
''')


if __name__ == "__main__":
    unittest.main()
