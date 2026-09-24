import base64
import io
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, patch

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
            "objective": "Do the thing", "requirementContext": ["The requested behavior must work."],
            "nonGoals": ["Unrelated behavior."], "downstreamConsumer": "",
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
            allowedPaths=["src/app.py", "tests/test_app.py"],
            validationCommands=["python -m unittest tests.test_app"],
        )
        return task

    def test_plan_round_trip(self):
        text = plan.render_tasks([self.task()], "0123456789abcdef", "abc123", campaign_validation_commands=["python -m unittest"])
        metadata, tasks = run.parse_tasks(text)
        self.assertEqual(metadata["baseSha"], "0123456789abcdef")
        self.assertEqual(tasks[0]["id"], "TASK-0001")
        for removed in ("sourceRef", "testPaths", "regressionCommand", "Seed commit"):
            self.assertNotIn(removed, text)

    def test_campaign_validation_round_trip_and_digest(self):
        commands = ["python -m unittest", "python -m compileall ."]
        text = plan.render_tasks([self.task()], "0123456789abcdef", "abc123", campaign_validation_commands=commands)
        metadata, tasks = run.parse_tasks(text)
        self.assertEqual(metadata["campaignValidationCommands"], commands)
        self.assertNotEqual(plan.plan_digest(tasks, commands), plan.plan_digest(tasks, commands[::-1]))
        with self.assertRaises(ValueError):
            plan.render_tasks(tasks, metadata["baseSha"], metadata["requirementsHash"], campaign_validation_commands=[])

    def test_planning_result_requires_campaign_validation(self):
        with self.assertRaisesRegex(ValueError, "campaign validation"):
            plan.validate_plan({"campaignObjective": "Relay campaign", "campaignValidationCommands": [], "tasks": [self.task()]})
        value = {"campaignObjective": "Relay campaign", "campaignValidationCommands": ["python -m unittest"], "tasks": [self.task()]}
        validated = plan.validate_plan(value)
        self.assertEqual(validated["campaignObjective"], "Relay campaign")
        self.assertEqual(validated["tasks"][0]["objective"], "Do the thing")

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
        with self.assertRaises(ValueError):
            plan.render_tasks([self.task(dependencies=["TASK-9999"])], "0123456", "abc123", campaign_validation_commands=["python -m unittest"])

    def test_cyclic_dependency_and_path_escape_rejected(self):
        first, second = self.task("TASK-0001", ["TASK-0002"]), self.task("TASK-0002", ["TASK-0001"])
        with self.assertRaises(ValueError):
            run.parse_tasks(plan.render_tasks([first, second], "0123456", "abc123", campaign_validation_commands=["python -m unittest"]))
        escaped = self.task(); escaped["allowedPaths"] = ["../outside"]
        with self.assertRaises(ValueError):
            run.parse_tasks(plan.render_tasks([escaped], "0123456", "abc123", campaign_validation_commands=["python -m unittest"]))

    def test_backward_review_transitions_rejected(self):
        forbidden = [("verify-1", "slice-review"), ("repair-1", "slice-review"), ("approved", "slice-review"), ("needs-user", "repair-2")]
        for source, target in forbidden:
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                run.transition_review({"phase": source}, target)

    def test_forward_review_transitions(self):
        session = {"phase": "slice-review"}
        for phase in ("repair-1", "verify-1", "repair-2", "verify-2", "approved"):
            run.transition_review(session, phase)
        self.assertEqual(session["phase"], "approved")

    def test_scope_resolution_is_forward_only(self):
        session = {"phase": "slice-review"}
        run.transition_review(session, "scope-resolution")
        run.transition_review(session, "repair-1")
        with self.assertRaisesRegex(ValueError, "illegal review transition"):
            run.transition_review(session, "slice-review")

    def test_prompts_require_small_verified_production_work(self):
        planning = plan.planning_prompt("requirements", "instructions", ["app.py"], "abc", [])
        review = plan.plan_review_prompt("plan-reviewer", "requirements", "instructions", [self.task()], "digest")
        for phrase in ("smallest independently verifiable", "fresh context", "named downstream consumer", "internal component"):
            self.assertIn(phrase, planning)
        for phrase in ("production entrypoint", "integration paths", "layer-only decomposition"):
            self.assertIn(phrase, review)
        self.assertEqual(json.loads(run.worker_prompt("task", self.task()))["mode"], "task")

    def test_worker_cannot_change_mode(self):
        value = {"mode": "repair", "assignmentId": "TASK-0001", "status": "candidate", "candidateSha": "abc", "validation": [], "summary": ""}
        with self.assertRaises(ValueError):
            run.validate_agent_result("worker", value, "TASK-0001", "task")

    def test_reviewer_and_auditor_accept_only_evidence(self):
        finding = {"severity": "P1", "location": "src/app.py:1", "failure": "fails", "reproduction": "run test", "requirement": "works", "evidence": "exit 1", "candidateIntroduced": True, "affectedPaths": ["src/app.py"]}
        value = {"assignmentId": "TASK-0001", "mode": "initial", "reviewEpoch": 0, "candidateSha": "abc", "resolvedFindingIds": [], "findings": [finding]}
        self.assertEqual(run.validate_agent_result("slice-reviewer", value, "TASK-0001", review_epoch=0), value)
        self.assertEqual(run.validate_agent_result("audit-worker", {"scopeId": "AUDIT-0001", "findings": [finding]}, "AUDIT-0001")["findings"], [finding])
        for changed, path in (
            (finding | {"action": "repair"}, "$.findings[0].action"),
            (finding | {"status": "open"}, "$.findings[0].status"),
            (finding | {"id": "agent-id"}, "$.findings[0].id"),
            (finding | {"affectedPaths": ["../escape.py"]}, "$.findings[0].affectedPaths[0]"),
        ):
            with self.subTest(path=path), self.assertRaisesRegex(run.ProtocolValidationError, re.escape(path)):
                run.validate_agent_result("slice-reviewer", value | {"findings": [changed]}, "TASK-0001", review_epoch=0)
        with self.assertRaisesRegex(run.ProtocolValidationError, r"\$\.mode"):
            run.validate_agent_result("slice-reviewer", value | {"mode": "incremental"}, "TASK-0001", review_epoch=0)
        with self.assertRaisesRegex(run.ProtocolValidationError, r"\$\.reviewEpoch"):
            run.validate_agent_result("slice-reviewer", value | {"reviewEpoch": 1}, "TASK-0001", review_epoch=0)
        incremental = value | {"mode": "incremental", "reviewEpoch": 1, "resolvedFindingIds": ["unknown"]}
        with self.assertRaisesRegex(run.ProtocolValidationError, r"\$\.resolvedFindingIds"):
            run.validate_agent_result("verification-reviewer", incremental, "TASK-0001", review_epoch=1, open_finding_ids={"F-known"})

    def test_review_epochs_are_monotonic_without_a_fixed_repair_count(self):
        session = {"phase": "verify-20"}
        run.transition_review(session, "repair-21")
        self.assertEqual(session["phase"], "repair-21")

    def test_positive_deadlines_cannot_be_disabled(self):
        with self.assertRaises(SystemExit):
            plan.parser().parse_args(["--repo", ".", "--requirements", "PLAN.md", "--agent-timeout", "0"])
        with self.assertRaises(SystemExit):
            run.parser().parse_args(["--repo", ".", "--agent-timeout", "0"])
        for option in ("--workers", "--campaign-active-timeout", "--campaign-agent-calls", "--validation-timeout", "--provider-timeout", "--provider-check-timeout"):
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
        with tempfile.TemporaryDirectory() as root, patch("plan.invoke_agent", side_effect=[invalid, valid]), patch("plan.progress", side_effect=lambda event, detail: events.append((event, detail))):
            result = plan.invoke_validated(Path(root), "prompt", plan.scout_schema("src"), lambda value: plan.validate_scout(value, "src"), 10, budget, 1, "role=scout slot=2")
        self.assertIs(result, valid)
        self.assertEqual([event for event, _ in events], ["START", "RETRY", "START", "DONE"])
        self.assertIn("attempt=1/2 call=1/2 timeout=10s", events[0][1])
        self.assertIn("attempt=2/2 call=2/2 timeout=10s", events[2][1])
        self.assertEqual(budget.started, 2)

    def test_plan_review_accepts_after_one_review(self):
        tasks = [ContractTests().task()]
        digest = plan.plan_digest(tasks)
        results = [{"assignmentId": "PLAN", "candidateSha": digest, "findings": []}]
        with patch("plan.invoke_validated", side_effect=results) as invoke:
            self.assertIs(plan.reviewed_plan(Path("."), "requirements", "instructions", [], "base", tasks, 10, plan.CallBudget(7), 2), tasks)
        self.assertEqual(invoke.call_count, 1)
        self.assertIn("Role: plan-reviewer", invoke.call_args_list[0].args[1])

    def test_plan_findings_get_one_repair_and_scoped_verification(self):
        tasks = [ContractTests().task()]
        revised = [tasks[0] | {"validationCommands": ["fixed command"]}]
        finding = {"id": "plan-command", "severity": "P1", "location": "TASK-0001 Validation", "failure": "command is invalid", "reproduction": "bad command", "requirement": "validation must run", "evidence": "unsupported syntax", "candidateIntroduced": True}
        results = [
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "findings": [finding]},
            revised,
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(revised), "status": "resolved"},
        ]
        with patch("plan.invoke_validated", side_effect=results) as invoke:
            self.assertEqual(plan.reviewed_plan(Path("."), "requirements", "instructions", [], "base", tasks, 10, plan.CallBudget(7), 2), revised)
        self.assertEqual(invoke.call_count, 3)
        self.assertIn("Repair only the supplied findings", invoke.call_args_list[1].args[1])
        self.assertIn("Verify only that every supplied finding", invoke.call_args_list[2].args[1])

    def test_unresolved_plan_repair_stops_without_another_loop(self):
        tasks = [ContractTests().task()]
        finding = {"id": "plan-command", "severity": "P1", "location": "TASK-0001 Validation", "failure": "command is invalid", "reproduction": "bad command", "requirement": "validation must run", "evidence": "unsupported syntax", "candidateIntroduced": True}
        results = [
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "findings": [finding]},
            tasks,
            {"assignmentId": "PLAN", "candidateSha": plan.plan_digest(tasks), "status": "unresolved"},
        ]
        with patch("plan.invoke_validated", side_effect=results) as invoke, self.assertRaisesRegex(RuntimeError, "verification unresolved"):
            plan.reviewed_plan(Path("."), "requirements", "instructions", [], "base", tasks, 10, plan.CallBudget(7), 2)
        self.assertEqual(invoke.call_count, 3)

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
            self.assertIn("--workers 2 --campaign-active-timeout 86400 --campaign-agent-calls 100", planned.stderr)
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

    def test_campaign_resource_limits_are_rendered(self):
        content = b"requirements"
        text = plan.render_plan(plan.validate_tasks({"tasks": [ContractTests().task()]}), "0123456", hashlib.sha256(content).hexdigest(), ["python -m unittest"], "objective", {"kind": "snapshot", "encoding": "base64", "content": base64.b64encode(content).decode()}, 7200, 40)
        metadata, _ = run.parse_tasks(text)
        self.assertEqual((metadata["campaignActiveTimeoutSeconds"], metadata["campaignAgentCallLimit"]), (7200, 40))

    def test_run_rejects_plan_limit_mismatch_before_creating_campaign(self):
        with tempfile.TemporaryDirectory() as root:
            args = run.parser().parse_args(["--repo", root, "--campaign-agent-calls", "99"])
            text = plan.render_tasks([ContractTests().task()], "0123456", "abc123", campaign_validation_commands=["python -m unittest"])
            with self.assertRaises(RuntimeError):
                run.initialize_campaign(Path(root), text, args)
            self.assertFalse((Path(root) / ".relay").exists())


class DeterministicCoreTests(unittest.TestCase):
    def state_store(self, root, format_retries=2):
        args = run.parser().parse_args(["--repo", str(root), "--format-retries", str(format_retries)])
        campaign_commands = ["python -c \"pass\""]
        tasks_text = plan.render_tasks([ContractTests().task()], "0123456", "abc123", campaign_validation_commands=campaign_commands)
        metadata, _ = run.parse_tasks(tasks_text)
        state = run.initial_state(Path(root), metadata, args)
        state["campaignId"] = "test"
        state["pathDirectories"] = ["src"]
        path = Path(root) / ".relay" / "state.json"
        Path(root, "tasks.md").write_text(tasks_text, encoding="utf-8")
        Path(root, "bugs.md").write_text(run.render_bugs("test", Path(root)), encoding="utf-8")
        return run.StateStore(path, state)

    def azure_store(self, root, merge_method="squash"):
        store = self.state_store(root)
        store.state.update(provider="azure-devops", azureOrganization="my org", azureProject="My Project", azureRepository="My Repo", mergeMethod=merge_method)
        return store

    def azure_pr(self, status="active", merge_status="succeeded", sha="abc"):
        return {"pullRequestId": 7, "status": status, "mergeStatus": merge_status, "lastMergeSourceCommit": {"commitId": sha}}

    def test_worker_protocol_correction_uses_frozen_context_schema_and_redacted_bounded_output(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = self.state_store(root, format_retries=1)
            assignment = ContractTests().task(); store.state["taskStates"][assignment["id"]] = {"phase": "implementing", "mode": "task"}
            prompts, schemas, commands = [], [], []
            secret = "protocol-secret-value"
            invalid = {"mode": "task", "assignmentId": assignment["id"], "status": "candidate", "changedPaths": [], "validation": [], "summary": secret + "x" * 70000, "proposedLearnings": []}
            valid = invalid | {"candidateSha": "committed"}; valid["summary"] = "done"
            def agent(command, **kwargs):
                prompts.append(kwargs["input"]); commands.append(command)
                schemas.append(Path(command[command.index("--output-schema") + 1]).read_text(encoding="utf-8"))
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text(json.dumps(invalid if len(prompts) == 1 else valid), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch.dict(os.environ, {"RELAY_TEST_SECRET": secret}), patch("run.bounded_run", side_effect=agent):
                result = run.invoke_with_replacements(store, threading.Semaphore(1), root, assignment["id"], "worker", run.worker_prompt("task", assignment), mode="task")
            self.assertEqual(result["candidateSha"], "committed")
            self.assertEqual(schemas[0], schemas[1])
            self.assertIn("$.candidateSha", prompts[1])
            self.assertIn("correct only the response object", prompts[1])
            self.assertIn("[REDACTED]", prompts[1])
            self.assertNotIn(secret, prompts[1])
            sequence = next(iter(store.state["protocolSequences"].values()))
            self.assertTrue(sequence["rejections"][0]["truncated"])
            self.assertEqual([record["protocolAttempt"] for record in store.state["promptRecords"]], [1, 2])
            self.assertEqual(store.state["attemptCounters"][assignment["id"]], 1)
            self.assertEqual(commands[1][commands[1].index("--sandbox") + 1], "read-only")
            artifact = root / sequence["rejections"][0]["artifact"]
            self.assertTrue(artifact.is_file())
            self.assertNotIn(secret, artifact.read_text(encoding="utf-8"))

    def test_malformed_json_correction_reports_parser_location(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = self.state_store(root, format_retries=1)
            assignment = ContractTests().task(); store.state["taskStates"][assignment["id"]] = {"phase": "implementing", "mode": "task"}
            prompts = []
            valid = {"mode": "task", "assignmentId": assignment["id"], "status": "candidate", "candidateSha": "abc", "changedPaths": [], "validation": [], "summary": "done", "proposedLearnings": []}
            def agent(command, **kwargs):
                prompts.append(kwargs["input"])
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text("{\n" if len(prompts) == 1 else json.dumps(valid), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("run.bounded_run", side_effect=agent):
                run.invoke_with_replacements(store, threading.Semaphore(1), root, assignment["id"], "worker", run.worker_prompt("task", assignment), mode="task")
            self.assertIn('"path": "$"', prompts[1])
            self.assertIn('"code": "json-parse"', prompts[1])
            self.assertRegex(prompts[1], r"line 2, column 1")

    def test_agent_operational_failure_is_logged(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = self.state_store(root)
            assignment = ContractTests().task(); assignment_id = assignment["id"]
            store.state["taskStates"][assignment_id] = {"phase": "candidate-validation", "candidateSha": "abc"}
            run.ensure_review_session(store, assignment_id, "abc")
            failed = subprocess.CompletedProcess(["codex"], 1, "standard output", "invalid schema")
            with patch("run.bounded_run", return_value=failed) as command, self.assertRaisesRegex(RuntimeError, r"slice-reviewer failed with exit code 1; log:"):
                run.invoke_with_replacements(store, threading.Semaphore(1), root, assignment_id, "slice-reviewer", run.role_prompt("slice-reviewer", assignment, "abc", {"expectedReviewEpoch": 0, "openFindingIds": []}), review=True)
            self.assertFalse(command.call_args.kwargs["check"])
            log = root / ".relay" / "logs" / f"{assignment_id}-slice-reviewer-1.log"
            self.assertEqual(log.read_text(encoding="utf-8"), "standard output\n--- stderr ---\ninvalid schema")
            self.assertEqual(next(iter(store.state["protocolSequences"].values()))["status"], "operational-failed")

    def test_reviewer_protocol_exhaustion_preserves_candidate_and_epoch(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = self.state_store(root, format_retries=1)
            assignment = ContractTests().task(); assignment_id = assignment["id"]
            store.state["taskStates"][assignment_id] = {"phase": "candidate-validation", "candidateSha": "abc", "fixAttemptsStarted": 0}
            store.state["reviewSessions"][assignment_id] = {"phase": "slice-review", "reviewResult": None, "acceptedBlockerIds": [], "reviewCallsStarted": 0, "reviewCallLimit": 10}
            finding = {"severity": "P1", "location": "src/app.py:1", "failure": "fails", "reproduction": "run", "requirement": "It works.", "evidence": "proof", "candidateIntroduced": True, "affectedPaths": ["src/app.py"], "action": "repair"}
            rejected = {"assignmentId": assignment_id, "mode": "initial", "reviewEpoch": 0, "candidateSha": "abc", "resolvedFindingIds": [], "findings": [finding]}
            def agent(command, **_kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(rejected), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch("run.bounded_run", side_effect=agent), self.assertRaises(run.ProtocolExhaustedError):
                run.invoke_with_replacements(store, threading.Semaphore(1), root, assignment_id, "slice-reviewer", run.role_prompt("slice-reviewer", assignment, "abc", {"expectedReviewEpoch": 0, "openFindingIds": []}), review=True)
            self.assertEqual(store.state["taskStates"][assignment_id]["phase"], "candidate-validation")
            self.assertEqual(store.state["taskStates"][assignment_id]["fixAttemptsStarted"], 0)
            self.assertEqual(store.state["reviewSessions"][assignment_id]["phase"], "slice-review")
            self.assertEqual(store.state["reviewSessions"][assignment_id]["reviewCallsStarted"], 1)
            self.assertNotIn("verificationEvidence", store.state["taskStates"][assignment_id])
            self.assertEqual(run.load_bugs(store), [])
            self.assertEqual(store.state["coordinatorOperations"][0]["operation"], "protocol-failed")
            self.assertEqual(next(iter(store.state["protocolSequences"].values()))["attemptsStarted"], 2)
            self.assertEqual(run.blocked_assignments(store.state), [(assignment_id, "structured-output correction allowance exhausted")])

    def test_planning_protocol_correction_keeps_original_prompt_and_schema(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); prompts = []
            valid = {"scope": "src", "implemented": [], "missing": [], "conflicts": [], "relevantPaths": [], "validationCommands": [], "evidence": []}
            def agent(_repo, prompt, _schema, _timeout, _budget):
                prompts.append(prompt)
                if len(prompts) == 1:
                    raise run.ProtocolValidationError("$.scope", "scope", "expected src", '{"scope":"other"}')
                return valid
            with patch("plan.invoke_agent", side_effect=agent):
                self.assertEqual(plan.invoke_validated(root, "ORIGINAL", plan.scout_schema("src"), lambda value: plan.validate_scout(value, "src"), 10, plan.CallBudget(2), 1, "role=scout"), valid)
            self.assertEqual(prompts[0], "ORIGINAL")
            self.assertTrue(prompts[1].startswith("ORIGINAL"))
            self.assertIn("$.scope", prompts[1])
            self.assertIn("<untrusted-rejected-output>", prompts[1])

    def test_protocol_retry_reservation_survives_reload_without_free_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = self.state_store(root, format_retries=2)
            assignment = ContractTests().task(); assignment_id = assignment["id"]
            store.state["taskStates"][assignment_id] = {"phase": "implementing"}
            sequence_id, _ = run._prepare_protocol_sequence(store, root, assignment_id, "worker", run.worker_prompt("task", assignment), "task")
            _number, process_id = run._consume_agent_call(store, assignment_id, "worker", "task", False, False, sequence_id)
            store.state["activeProcesses"].pop(process_id); store.save()
            reloaded = run.StateStore(store.path, json.loads(store.path.read_text(encoding="utf-8")))
            self.assertEqual(reloaded.state["protocolSequences"][sequence_id]["attemptsStarted"], 1)
            run._consume_agent_call(reloaded, assignment_id, "worker", "task", False, False, sequence_id)
            self.assertEqual(reloaded.state["protocolSequences"][sequence_id]["attemptsStarted"], 2)

    def test_reconcile_normalizes_legacy_review_and_audit_findings(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = self.state_store(root)
            legacy = {"id": "old", "severity": "P1", "location": "src/app.py:1", "failure": "fails", "reproduction": "run", "requirement": "works", "evidence": "proof", "candidateIntroduced": True, "action": "repair", "reason": "old", "repairPaths": ["src/app.py"]}
            store.state["reviewSessions"]["TASK-0001"] = {"phase": "verify-1", "reviewResult": {"assignmentId": "TASK-0001", "candidateSha": "abc", "findings": [legacy]}, "verificationResult": {"assignmentId": "TASK-0001", "candidateSha": "abc", "status": "resolved", "resolvedFindingIds": [], "findings": [legacy]}, "pendingRepairNumber": 1}
            store.state["auditScopes"] = {"AUDIT-0001": {"findings": [legacy]}}
            run.reconcile(store)
            review = store.state["reviewSessions"]["TASK-0001"]
            self.assertEqual(review["reviewResult"]["findings"][0]["affectedPaths"], ["src/app.py"])
            self.assertNotIn("action", review["reviewResult"]["findings"][0])
            self.assertTrue(review["verificationResult"]["legacyResolvedAll"])
            self.assertEqual(store.state["auditScopes"]["AUDIT-0001"]["findings"][0]["affectedPaths"], ["src/app.py"])

    def provider_metadata(self, store, assignment, sha, pr):
        title, _body, digest = run.canonical_pr_metadata(store.state, assignment, sha)
        task_state = store.state["taskStates"].setdefault(assignment["id"], {})
        task_state.update(candidateSha=sha, pushedSha=sha, pr=pr, publicationProof={"candidateSha": sha, "providerRecord": pr}, prMetadata={"hash": digest, "candidateSha": sha, "title": title})
        store.state["pullRequests"][assignment["id"]] = pr

    def test_validation_uses_explicit_platform_shells(self):
        command = "$items = @('one', 'two'); $items | ForEach-Object { $_ }"
        with patch.object(run.os, "name", "nt"), patch("run.tool_command", return_value=[r"C:\Tools\pwsh.exe"]):
            self.assertEqual(run.validation_command(command), [r"C:\Tools\pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command])
        with patch.object(run.os, "name", "posix"):
            self.assertEqual(run.validation_command(command), ["/bin/sh", "-c", command])

    def test_shared_path_policy_handles_files_directories_and_segment_globs(self):
        self.assertTrue(run.allowed_change("tests/fixtures/offline/case.json", ["tests/fixtures/offline/**"]))
        self.assertTrue(run.allowed_change("tests/fixtures/offline/nested/case.json", ["tests/fixtures/offline/**"]))
        self.assertTrue(run.allowed_change("src/a/test_1.py", ["src/*/test_?.py"]))
        self.assertFalse(run.allowed_change("src/a/nested/test_1.py", ["src/*/test_?.py"]))
        self.assertTrue(run.allowed_change("src/nested/app.py", ["src"], {"src"}))
        self.assertFalse(run.allowed_change("src/app.py/generated", ["src/app.py"], set()))
        self.assertTrue(run.allowed_change("deleted.py", ["deleted.py"], set()))

    def test_glob_scopes_conservatively_serialize_possible_overlap(self):
        self.assertTrue(run.paths_conflict(["src/**"], {"src/generated/*.py"}))
        self.assertTrue(run.paths_conflict(["src/*/app.py"], {"src/service/*.py"}))
        self.assertFalse(run.paths_conflict(["src/api/**"], {"src/ui/**"}))

    def test_completed_transitive_dependencies_bound_repair_scope(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            first = ContractTests().task("TASK-0001"); first["allowedPaths"] = ["src/base.py"]
            second = ContractTests().task("TASK-0002", ["TASK-0001"]); second["allowedPaths"] = ["src/feature.py"]
            future = ContractTests().task("TASK-0003"); future["allowedPaths"] = ["src/future.py"]
            task_text = plan.render_tasks([first, second, future], "0123456", "abc123", campaign_validation_commands=store.state["campaignValidationCommands"])
            Path(root, "tasks.md").write_text(task_text, encoding="utf-8")
            store.state["planDigest"] = run.parse_tasks(task_text)[0]["planDigest"]
            store.state["taskStates"]["TASK-0001"] = {"phase": "integrated"}
            paths = run.maximum_repair_paths(store, second)
            self.assertEqual(paths, ["src/feature.py", "src/base.py"])
            self.assertEqual(run.approved_repair_paths(store, second, [{"allowedPaths": ["src/base.py"]}]), (["src/base.py"], []))
            self.assertEqual(run.approved_repair_paths(store, second, [{"allowedPaths": ["src/future.py"]}])[1], ["src/future.py"])

    def test_stop_states_distinguish_human_decisions_from_failures(self):
        self.assertEqual(run.stop_phase("credentials require authorization"), "needs-user")
        self.assertEqual(run.stop_phase("unrelated path requires paths outside assignment scope"), "needs-user")
        self.assertEqual(run.stop_phase("provider API returned malformed JSON"), "blocked")

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
                if "slice-reviewer" in output.name:
                    captured["reviewer"] = kwargs["env"]
                    result = {"assignmentId": assignment["id"], "mode": "initial", "reviewEpoch": 0, "candidateSha": "abc", "resolvedFindingIds": [], "findings": []}
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
                    run.invoke_agent(store, threading.Semaphore(1), worktree, assignment["id"], "slice-reviewer", "prompt", review=True)
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
            for removed in (b"sourceRef", b"testPaths", b"regressionCommand", b"seed"):
                self.assertNotIn(removed, first)
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

    def test_completion_requires_settled_work_and_execution_resources(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            task = ContractTests().task(); task["status"] = "satisfied"
            store.state["finalValidation"] = {"phase": "passed"}
            run.verify_campaign_completion(store, [task])
            store.state["workItems"]["repair"] = {"id": "repair", "status": "running"}
            with self.assertRaisesRegex(RuntimeError, "unfinished work"):
                run.verify_campaign_completion(store, [task])
            store.state["workItems"]["repair"]["status"] = "replaced"
            store.state["pathLeases"]["TASK-0001"] = {"paths": ["src"]}
            with self.assertRaisesRegex(RuntimeError, "active execution resources"):
                run.verify_campaign_completion(store, [task])

    def test_validation_repairs_stop_when_failures_do_not_advance(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation"}
            with patch("run.progress_fingerprint", side_effect=[("tree-a", "same-failure"), ("tree-b", "same-failure")]), patch("run.target_changes", return_value=["README.md"]):
                run.record_progress(store, assignment["id"], Path(root), "a", assignment["allowedPaths"], "validation")
                with self.assertRaisesRegex(RuntimeError, "did not advance"):
                    run.record_progress(store, assignment["id"], Path(root), "b", assignment["allowedPaths"], "validation")
            self.assertEqual(store.state["taskStates"][assignment["id"]]["visitedFingerprints"], ["tree-a"])

    def test_relevant_repairs_continue_until_a_state_repeats(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation"}
            fingerprints = [("tree-a", "same-failure"), ("tree-b", "same-failure"), ("tree-a", "same-failure")]
            with patch("run.progress_fingerprint", side_effect=fingerprints), patch("run.target_changes", return_value=["src/file.py"]):
                run.record_progress(store, assignment["id"], Path(root), "a", assignment["allowedPaths"], "validation")
                run.record_progress(store, assignment["id"], Path(root), "b", assignment["allowedPaths"], "validation")
                with self.assertRaisesRegex(RuntimeError, "repeated progress fingerprint"):
                    run.record_progress(store, assignment["id"], Path(root), "a", assignment["allowedPaths"], "validation")

    def test_incremental_review_may_only_add_repair_diff_findings(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            finding = {"location": "src/other.py:1", "affectedPaths": ["src/other.py"]}
            with patch("run.target_changes", return_value=["src/changed.py"]), self.assertRaisesRegex(ValueError, "outside the repair diff"):
                run.validate_incremental_findings(store, Path(root), "old", "new", [finding])
            finding.update(location="src/changed.py:1", affectedPaths=["src/changed.py"])
            with patch("run.target_changes", return_value=["src/changed.py"]):
                run.validate_incremental_findings(store, Path(root), "old", "new", [finding])

    def test_completed_worker_resumes_validation_without_another_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["campaignAgentCallsStarted"] = store.state["campaignAgentCallLimit"]
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation", "mode": "task", "pendingWorkerSha": "abc", "pushed": False, "merged": False}
            def approved(*args):
                store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc"}
                return True
            with patch("run.create_worktree", return_value=(Path(root), "branch")), patch("run.invoke_with_replacements") as worker, patch("run.validate_candidate", return_value="abc") as validate, patch("run.publish_candidate", return_value={"number": 1}), patch("run.run_review", side_effect=approved), patch("run.merge_assignment", return_value=True), patch("run.cleanup_worktree"):
                self.assertTrue(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            worker.assert_not_called()
            validate.assert_called_once()

    def test_already_satisfied_worker_is_independently_reviewed(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            result = {"mode": "task", "assignmentId": assignment["id"], "status": "satisfied", "candidateSha": "", "changedPaths": [], "validation": [], "summary": "already present", "proposedLearnings": []}
            def git_result(_repo, *args, **_kwargs):
                output = "head\n" if args[:2] == ("rev-parse", "HEAD") else ""
                return subprocess.CompletedProcess(args, 0, output, "")
            with patch("run.create_worktree", return_value=(Path(root), "branch")), patch("run.invoke_with_replacements", return_value=result), patch("run.run_validations"), patch("run.git", side_effect=git_result), patch("run.run_review", return_value=True) as review, patch("run.publish_candidate") as publish, patch("run.cleanup_worktree"):
                self.assertTrue(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            review.assert_called_once_with(store, ANY, assignment, Path(root), "head")
            publish.assert_not_called()
            self.assertEqual(store.state["workItems"][assignment["id"]]["status"], "integrated")

    def test_completed_repair_resumes_validation_then_verification(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "repair-2", "fixAttemptsStarted": 2}
            store.state["worktrees"][assignment["id"]] = {"baseSha": "base"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "repair-2", "acceptedBlockerIds": [], "reviewCallsStarted": 3, "reviewCallLimit": 9, "pendingWorkerSha": "new", "previousCandidateSha": "old"}
            verified = {"assignmentId": assignment["id"], "mode": "incremental", "reviewEpoch": 2, "candidateSha": "new", "resolvedFindingIds": [], "findings": []}
            with patch("run.candidate_integrity", return_value="new"), patch("run.validate_candidate", return_value="new") as validate, patch("run.invoke_with_replacements", return_value=verified) as agent:
                self.assertTrue(run.run_review(store, threading.Semaphore(1), assignment, Path(root), "old"))
            validate.assert_called_once()
            self.assertEqual(agent.call_args.args[4], "verification-reviewer")

    def test_second_review_repair_starts_from_current_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "repair-1"}
            store.state["worktrees"][assignment["id"]] = {"baseSha": "base"}
            bug = ContractTests().backlog_bug(); bug.update(status="active", source=assignment["id"], allowedPaths=["src"]); bug.pop("deferralReason")
            run.write_bugs(store, [bug])
            store.state["reviewSessions"][assignment["id"]] = {
                "phase": "repair-1", "acceptedBlockerIds": [bug["id"]], "reviewCallsStarted": 0, "reviewCallLimit": 9, "currentCandidateSha": "initial",
            }
            workers = iter(("repair-one", "repair-two"))
            verifications = iter((False, True))
            prompts = []
            parents = []

            def invoke(_store, _semaphore, _worktree, assignment_id, role, prompt, **_kwargs):
                prompts.append((role, prompt))
                if role == "worker":
                    return {"assignmentId": assignment_id, "candidateSha": next(workers), "status": "candidate"}
                candidate = "repair-one" if len([item for item in prompts if item[0] == role]) == 1 else "repair-two"
                resolved = next(verifications)
                return {"assignmentId": assignment_id, "mode": "incremental", "reviewEpoch": 1 if candidate == "repair-one" else 2, "candidateSha": candidate, "resolvedFindingIds": [bug["sourceFindingId"]] if resolved else [], "findings": []}

            def integrity(_store, _assignment, _worktree, result, parent):
                parents.append((result["candidateSha"], parent))
                return result["candidateSha"]

            def validate(_store, _assignment, _worktree, result):
                store.state["taskStates"][assignment["id"]].pop("error", None)
                return result["candidateSha"]

            with patch("run.invoke_with_replacements", side_effect=invoke), patch("run.candidate_integrity", side_effect=integrity), patch("run.validate_candidate", side_effect=validate), patch("run.record_progress"):
                self.assertTrue(run.run_review(store, threading.Semaphore(1), assignment, Path(root), "initial"))

            worker_prompts = [prompt for role, prompt in prompts if role == "worker"]
            self.assertEqual(json.loads(worker_prompts[0])["candidateSha"], "initial")
            self.assertEqual(json.loads(worker_prompts[1])["candidateSha"], "repair-one")
            self.assertEqual(parents, [("repair-one", "initial"), ("repair-two", "repair-one")])

    def test_same_sha_review_repair_skips_validation_and_verification(self):
        with tempfile.TemporaryDirectory() as root:
            target = make_git_repository(Path(root))
            candidate = git_output(target, "rev-parse", "HEAD").strip()
            store = self.state_store(target)
            run.exclude_relay_files(target)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "repair-1"}
            store.state["worktrees"][assignment["id"]] = {"baseSha": candidate}
            store.state["reviewSessions"][assignment["id"]] = {
                "phase": "repair-1", "acceptedBlockerIds": [], "reviewCallsStarted": 0, "reviewCallLimit": 9, "currentCandidateSha": candidate,
            }
            result = {"assignmentId": assignment["id"], "candidateSha": candidate, "status": "candidate"}
            with patch("run.invoke_with_replacements", return_value=result) as agent, patch("run.validate_candidate") as validate:
                self.assertFalse(run.run_review(store, threading.Semaphore(1), assignment, target, candidate))
            validate.assert_not_called()
            self.assertEqual(agent.call_count, 1)
            self.assertEqual(store.state["taskStates"][assignment["id"]]["fixAttemptsStarted"], 1)
            self.assertEqual(store.state["taskStates"][assignment["id"]]["error"], "validation repair must commit a descendant candidate")

    def test_failed_verification_overrides_passed_provider_status_in_summaries(self):
        for verification_status in ("unresolved", "invalid-result"):
            with self.subTest(status=verification_status), tempfile.TemporaryDirectory() as root:
                store = self.state_store(root)
                assignment = ContractTests().task()
                store.state["taskStates"][assignment["id"]] = {"phase": "blocked", "providerStatus": "passed", "error": f"verification {verification_status}"}
                lines, _ = run.campaign_summary(store.state, [assignment], [])
                self.assertIn(f"reason=verification {verification_status}", "\n".join(lines))
                with patch("run.relay_console.emit") as event:
                    run.report_stopped(store.state)
                output = "\n".join(call.args[1] for call in event.call_args_list)
                self.assertIn(f"reason=verification {verification_status}", output)
                self.assertNotIn("reason=passed", output)

    def test_slice_review_needs_user_records_review_error(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "slice-review", "providerStatus": "passed"}
            store.state["reviewSessions"][assignment["id"]] = {
                "phase": "slice-review", "reviewResult": None,
                "acceptedBlockerIds": [], "reviewCallsStarted": 0,
                "reviewCallLimit": 7,
            }
            finding = {"severity": "P1", "location": "docs/file.md:1", "failure": "decision required", "reproduction": "inspect", "requirement": "choose", "evidence": "conflict", "candidateIntroduced": True, "affectedPaths": ["docs/file.md"]}
            review = {"assignmentId": assignment["id"], "mode": "initial", "reviewEpoch": 0, "candidateSha": "candidate", "resolvedFindingIds": [], "findings": [finding]}
            with patch("run.invoke_with_replacements", return_value=review), patch("run.target_changes", return_value=["docs/file.md"]):
                self.assertFalse(run.run_review(store, threading.Semaphore(1), assignment, Path(root), "candidate"))
            self.assertIn("exceeds maximum scope", store.state["taskStates"][assignment["id"]]["error"])

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
            store.state["reviewSessions"][assignment["id"]] = {"phase": "repair-1", "acceptedBlockerIds": [bug["id"]], "reviewCallsStarted": 3, "reviewCallLimit": 9}
            with patch("run.invoke_with_replacements") as worker:
                self.assertFalse(run.run_review(store, threading.Semaphore(1), assignment, Path(root), "sha"))
            worker.assert_not_called()
            self.assertEqual(run.fix_attempts_started(store.state, assignment["id"]), 0)
            self.assertIn("module.psm1", store.state["taskStates"][assignment["id"]]["error"])

    def test_campaign_resource_grant_is_persisted_separately(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["campaignAgentCallsStarted"] = store.state["campaignAgentCallLimit"]
            store.save()
            with self.assertRaisesRegex(RuntimeError, "resource ceiling"):
                run._consume_agent_call(store, assignment["id"], "worker", "task", False, False)
            store.state["campaignAgentCallLimit"] += 1
            store.save()
            number, process_id = run._consume_agent_call(store, assignment["id"], "worker", "task", False, False)
            self.assertEqual(number, store.state["campaignAgentCallLimit"])
            store.state["activeProcesses"].pop(process_id)

    def test_recovery_resume_and_defer_are_previewed_then_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation", "pendingWorkerSha": "candidate"}
            store.state["worktrees"][assignment["id"]] = {"path": root, "root": root, "branch": "relay/test/TASK-0001", "baseSha": "base"}
            store.save()
            before = store.path.read_bytes()
            snapshot = {"headSha": "candidate", "branch": "relay/test/TASK-0001"}
            with patch("run._recovery_snapshot", return_value=snapshot):
                actions = run.plan_recovery(store, [assignment], [])
                self.assertEqual(store.path.read_bytes(), before)
                run.apply_recovery(store, [assignment], actions)
            self.assertEqual(store.state["taskStates"][assignment["id"]]["phase"], "candidate-validation")

            bug = {"id": "BUG-0001", "title": "Later", "severity": "P2", "status": "active", "source": assignment["id"], "sourceFindingId": "later", "location": "src/app.py:1", "failure": "fails", "reproduction": "test", "requirement": "works", "evidence": "proof", "allowedPaths": ["src/app.py"]}
            Path(root, "bugs.md").write_text(run.render_bugs("test", Path(root), [bug]), encoding="utf-8")
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]].update(phase="needs-user", error="repair requires a human decision")
            store.state["reviewSessions"][assignment["id"]] = {"phase": "needs-user", "initialCandidateSha": "candidate", "acceptedBlockerIds": [bug["id"]]}
            store.save()
            with patch("run._recovery_snapshot", return_value=snapshot), patch("run.recovery_worktree", return_value=(Path(root), store.state["worktrees"][assignment["id"]])):
                actions = run.plan_recovery(store, [assignment], [bug["id"]])
                run.apply_recovery(store, [assignment], actions)
            self.assertEqual(run.load_bugs(store)[0]["status"], "backlog")
            self.assertEqual(store.state["reviewSessions"][assignment["id"]]["reviewedSha"], "candidate")

    def test_recovery_preserves_attempts_for_validated_reviewer_process_failure(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task(); assignment_id = assignment["id"]
            store.state.update(phase="blocked", campaignAgentCallsStarted=4)
            store.state["taskStates"][assignment_id] = {
                "phase": "blocked", "candidateSha": "candidate", "validationCandidateSha": "candidate",
                "error": "slice-reviewer failed with exit code 1", "validationHistory": [{"outcome": "passed"}],
            }
            store.state["reviewSessions"][assignment_id] = {
                "phase": "slice-review", "initialCandidateSha": "candidate", "reviewResult": None,
                "acceptedBlockerIds": [], "reviewCallsStarted": 1,
            }
            store.state["worktrees"][assignment_id] = {"path": root, "root": root, "branch": "relay/test/TASK-0001", "baseSha": "base"}
            store.state["protocolSequences"]["review"] = {
                "assignmentId": assignment_id, "role": "slice-reviewer", "status": "operational-failed",
                "attemptsStarted": 1, "attemptLimit": 3,
            }
            store.save()
            snapshot = {"headSha": "candidate", "branch": "relay/test/TASK-0001"}
            before_calls = store.state["campaignAgentCallsStarted"]
            with patch("run._recovery_snapshot", return_value=snapshot):
                actions = run.plan_recovery(store, [assignment], [])
                self.assertEqual(actions[0]["protocolAttemptsStarted"], 1)
                run.apply_recovery(store, [assignment], actions)
            self.assertEqual(store.state["taskStates"][assignment_id]["phase"], "slice-review")
            self.assertEqual(store.state["protocolSequences"]["review"]["status"], "open")
            self.assertEqual(store.state["protocolSequences"]["review"]["attemptsStarted"], 1)
            self.assertEqual(store.state["campaignAgentCallsStarted"], before_calls)
            self.assertEqual(store.state["reviewSessions"][assignment_id]["reviewCallsStarted"], 1)

    def test_reconcile_ignores_obsolete_extra_state_keys(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state.update(sourceRef="old", seedMetadata={"old": True}, validationRepairAttempts={"TASK-0001": 9})
            store.state["taskStates"]["TASK-0001"] = {"phase": "ready"}
            store.save()
            run.reconcile(store)
            self.assertEqual(store.state["taskStates"]["TASK-0001"]["fixAttemptsStarted"], 0)

    def test_recovery_rejects_active_campaign_and_unknown_ids(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="needs-user", activeProcesses={"worker": {}})
            with self.assertRaisesRegex(RuntimeError, "inactive"):
                run.plan_recovery(store, [assignment], [])
            store.state["activeProcesses"] = {}
            with self.assertRaisesRegex(ValueError, "active campaign bug"):
                run.plan_recovery(store, [assignment], ["BUG-9999"])

    def test_recovery_refuses_dirty_drifted_and_escaped_worktrees(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            worktree = root / "relay-worktrees" / "test" / "TASK-0001"
            worktree.mkdir(parents=True)
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state.update(phase="needs-user")
            store.state["taskStates"][assignment["id"]] = {"phase": "candidate-validation", "candidateSha": "candidate"}
            store.state["worktrees"][assignment["id"]] = {"path": str(worktree), "root": str(worktree), "branch": "relay/test/TASK-0001", "baseSha": "base"}
            head, dirty = ["candidate"], ["changed.txt\n"]
            def recovery_git(_repo, *args, **_kwargs):
                if args[:2] == ("rev-parse", "--show-toplevel"):
                    output = f"{worktree}\n"
                elif args[:2] == ("rev-parse", "HEAD"):
                    output = f"{head[0]}\n"
                elif args[0] == "status":
                    output = dirty[0]
                else:
                    output = ""
                return subprocess.CompletedProcess([], 0, output, "")
            with patch("run.tempfile.gettempdir", return_value=str(root)), patch("run.git", side_effect=recovery_git):
                with self.assertRaisesRegex(RuntimeError, "unexpected worktree changes"):
                    run.plan_recovery(store, [assignment], [])
                dirty[0] = ""
                head[0] = "drifted"
                with self.assertRaisesRegex(RuntimeError, "candidate SHA drift"):
                    run.plan_recovery(store, [assignment], [])
                store.state["worktrees"][assignment["id"]].update(path=str(root / "outside"), root=str(root / "outside"))
                with self.assertRaisesRegex(ValueError, "escapes"):
                    run.plan_recovery(store, [assignment], [])

    def test_recovery_uses_only_revalidated_review_scope(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = self.state_store(root)
            dependency = ContractTests().task("TASK-0001"); dependency["allowedPaths"] = ["src/dependency.py"]
            assignment = ContractTests().task("TASK-0002", [dependency["id"]]); assignment["allowedPaths"] = ["src/feature.py"]
            task_text = plan.render_tasks([dependency, assignment], store.state["baseSha"], store.state["requirementsHash"], campaign_validation_commands=store.state["campaignValidationCommands"])
            (root / "tasks.md").write_text(task_text, encoding="utf-8")
            store.state["planDigest"] = run.parse_tasks(task_text)[0]["planDigest"]
            store.state["taskStates"].update({dependency["id"]: {"phase": "integrated"}, assignment["id"]: {"phase": "verify-1", "candidateSha": "candidate"}})
            worktree = root / "relay-worktrees" / "test" / assignment["id"]
            worktree.mkdir(parents=True)
            store.state["worktrees"][assignment["id"]] = {"path": str(worktree), "root": str(worktree), "branch": "branch", "baseSha": "base"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "verify-1", "approvedRepairPaths": ["src/dependency.py"]}
            store.state["recoveryAllowedPaths"] = {assignment["id"]: ["src/obsolete.py"]}
            def recovery_git(_repo, *args, **_kwargs):
                if args[:2] == ("rev-parse", "--show-toplevel"):
                    output = f"{worktree}\n"
                elif args[:2] == ("rev-parse", "HEAD"):
                    output = "candidate\n"
                elif args[0] == "status":
                    output = ""
                elif args[0] == "diff":
                    output = "src/dependency.py\n"
                else:
                    output = ""
                return subprocess.CompletedProcess([], 0, output, "")
            with patch("run.tempfile.gettempdir", return_value=str(root)), patch("run.git", side_effect=recovery_git):
                self.assertEqual(run._recovery_snapshot(store, assignment)["headSha"], "candidate")
                store.state["reviewSessions"][assignment["id"]]["approvedRepairPaths"] = ["src/outside.py"]
                with self.assertRaisesRegex(RuntimeError, "outside maximum scope"):
                    run._recovery_snapshot(store, assignment)
                store.state["reviewSessions"][assignment["id"]].pop("approvedRepairPaths")
                with self.assertRaisesRegex(RuntimeError, "scope drift"):
                    run._recovery_snapshot(store, assignment)

    def test_coordinator_dispositions_cover_repair_bug_backlog_discard_and_needs_user(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            base = {"location": "src/app.py:1", "failure": "fails", "reproduction": "run", "requirement": "works", "evidence": "proof", "candidateIntroduced": True, "affectedPaths": ["src/app.py"]}
            findings = [
                base | {"severity": "P1"},
                base | {"severity": "P1", "location": "src/old.py:1", "failure": "old fails", "candidateIntroduced": False, "affectedPaths": ["src/old.py"]},
                base | {"severity": "P2", "location": "src/later.py:1", "failure": "later", "affectedPaths": ["src/later.py"]},
                base | {"severity": "P3", "location": "src/noise.py:1", "failure": "noise", "affectedPaths": ["src/noise.py"]},
                base | {"severity": "P1", "location": "src/out.py:1", "failure": "out", "affectedPaths": ["src/out.py"]},
                base | {"severity": "P1", "location": "src/wrong.py:1", "failure": "unsupported", "affectedPaths": ["src/app.py"]},
            ]
            dispositions = run.coordinator_dispositions("TASK-0001", findings, maximum_paths=["src/app.py"], reviewed_paths=["src/app.py", "src/later.py", "src/noise.py", "src/out.py", "src/wrong.py"], requirements=["works"])
            self.assertEqual([item["coordinatorDisposition"] for item in dispositions], ["repair", "bug", "backlog", "discard", "needs-user", "discard"])
            accepted = run.record_findings(store, "TASK-0001", dispositions)
            bugs = run.load_bugs(store)
            self.assertEqual(([bug["status"] for bug in bugs], [bug["id"] for bug in accepted]), (["active", "active", "backlog", "needs-user"], ["BUG-0001"]))
            self.assertTrue(all(bug["coordinatorReason"] for bug in bugs))
            self.assertEqual(len(store.state["findingRecords"]), 6)

    def test_finding_ids_deduplicate_and_requirement_match_is_exact_after_normalization(self):
        finding = {"severity": "P1", "location": "src/old.py:1", "failure": "old fails", "reproduction": "run", "requirement": "Must   work", "evidence": "proof", "candidateIntroduced": False, "affectedPaths": ["src/old.py"]}
        dispositions = run.coordinator_dispositions("TASK-0001", [finding, dict(finding)], maximum_paths=["src/app.py"], reviewed_paths=[], requirements=["Must work"])
        self.assertEqual(len(dispositions), 1)
        self.assertEqual(dispositions[0]["coordinatorDisposition"], "bug")
        mismatch = run.coordinator_dispositions("TASK-0001", [finding | {"requirement": "must work"}], maximum_paths=["src/app.py"], reviewed_paths=[], requirements=["Must work"])
        self.assertEqual(mismatch[0]["coordinatorDisposition"], "backlog")

    def test_slice_review_precedes_first_publication(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "slice-review", "mode": "task", "candidateSha": "abc", "pushed": False, "merged": False}
            order = []
            def review(*_args):
                order.append("review")
                store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "finalReviewedSha": "abc"}
                return True
            def publish(*_args):
                order.append("publish")
                return {"number": 1, "headRefOid": "abc"}
            with patch("run.create_worktree", return_value=(Path(root), "branch")), patch("run.run_review", side_effect=review), patch("run.publish_candidate", side_effect=publish), patch("run.merge_assignment", return_value=False), patch("run.clear_operation"):
                self.assertFalse(run.process_assignment(store, threading.Semaphore(1), assignment, "task"))
            self.assertEqual(order, ["review", "publish"])

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
        with patch("run.relay_console.emit") as event:
            run.report_stopped(state)
        self.assertEqual(event.call_args_list, [
            unittest.mock.call("STOPPED", "phase=needs-user assignments=4"),
            unittest.mock.call("BLOCKED", "category=assignment affected=AGENTS reason=bootstrap failed log=not-recorded"),
            unittest.mock.call("BLOCKED", "category=assignment affected=CAMPAIGN reason=campaign failed log=not-recorded"),
            unittest.mock.call("BLOCKED", "category=assignment affected=TASK-0002 reason=checks pending log=not-recorded"),
            unittest.mock.call("BLOCKED", "category=assignment affected=TASK-0001 reason=task failed log=not-recorded"),
        ])

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

    def test_runtime_progress_counts_running_and_queued_agents_once(self):
        state = {
            "workerLimit": 3, "taskTotal": 5,
            "taskStates": {"TASK-0001": {"phase": "ready"}, "BUG-0001": {"phase": "integrated"}},
            "activeProcesses": {
                "one": {"assignmentId": "TASK-0001", "role": "slice-reviewer", "status": "running", "operationDeadline": 1},
                "two": {"assignmentId": "TASK-0001", "role": "verification-reviewer", "status": "running", "operationDeadline": 1},
                "three": {"assignmentId": "TASK-0003", "role": "slice-reviewer", "status": "running", "operationDeadline": 1},
                "four": {"assignmentId": "TASK-0003", "role": "verification-reviewer", "status": "queued", "operationDeadline": 1},
                "five": {"assignmentId": "TASK-0001", "role": "slice-reviewer", "status": "queued", "operationDeadline": 1},
                "six": {"assignmentId": "TASK-0003", "role": "verification-reviewer", "status": "queued", "operationDeadline": 1},
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

    def test_heartbeat_only_refreshes_progress_and_preserves_periodic_save(self):
        class Store:
            state = {"agentTimeoutSeconds": 2, "workerLimit": 1, "taskTotal": 1, "taskStates": {}, "activeProcesses": {}}
            saves = 0

            def save(self):
                self.saves += 1

        class Stop:
            waits = 0

            def wait(self, _timeout):
                self.waits += 1
                return self.waits > 1

        class Time:
            values = iter((0, 1, 1))

            def monotonic(self):
                return next(self.values)

        store = Store()
        with patch("run.load_bugs", return_value=[]), patch("run.relay_console.interactive", return_value=True), patch("run.relay_console.update") as update, patch("run.time", Time()):
            run.heartbeat_loop(store, Stop())
        self.assertEqual(update.call_count, 2)
        update.assert_called_with("tasks 0/1 integrated | bugs 0 | agents 0/1 | idle")
        self.assertEqual(store.saves, 1)

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
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", {"number": 7, "state": "OPEN", "url": "x", "headRefOid": "abc"})
            show = subprocess.CompletedProcess([], 0, json.dumps(self.azure_pr()), "")
            waiting = subprocess.CompletedProcess([], 0, json.dumps([{"configuration": {"isBlocking": True, "type": {"id": "fa4e907d-c16b-4a4c-9dfa-4906e5d171dd"}}, "status": "rejected"}]), "")
            with patch("run.refresh_integration_base", return_value=True), patch("run.provider_approve"), patch("run.run_tool", side_effect=[show, waiting]), patch("run.time.time", side_effect=[100, 100, 101]), patch("run.time.sleep"), patch("run.invoke_with_replacements") as worker:
                self.assertFalse(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 7}, "abc"))
            self.assertEqual((store.state["taskStates"][assignment["id"]]["phase"], store.state["taskStates"][assignment["id"]]["providerStatus"]), ("waiting-provider", "policy-waiting"))
            self.assertEqual(run.fix_attempts_started(store.state, assignment["id"]), 0)
            worker.assert_not_called()

    def test_failed_azure_vote_can_be_followed_by_external_approval(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            assignment = ContractTests().task()
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["pullRequests"][assignment["id"]] = {"number": 7, "state": "OPEN"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", {"number": 7, "state": "OPEN", "url": "x", "headRefOid": "abc"})
            merged = subprocess.CompletedProcess([], 0, "{}", "")
            merged_pr = {"number": 7, "state": "MERGED", "url": "x", "headRefOid": "abc"}
            with patch("run.refresh_integration_base", return_value=True), patch("run.provider_with_retries", side_effect=[RuntimeError("vote denied"), merged]) as provider, patch("run.pr_inspect", return_value=merged_pr), patch("run.wait_for_checks", return_value="passed"), patch("run.invoke_with_replacements") as worker:
                self.assertTrue(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 7}, "abc"))
            self.assertIn("set-vote", provider.call_args_list[0].args)
            self.assertIn("update", provider.call_args_list[1].args)
            self.assertEqual(run.fix_attempts_started(store.state, assignment["id"]), 0)
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
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", {"number": 1, "state": "OPEN", "url": "x", "headRefOid": "abc"})
            merged_pr = {"number": 1, "state": "MERGED", "url": "x", "headRefOid": "abc"}
            with patch("run.refresh_integration_base", return_value=True), patch("run.wait_for_checks", return_value="bypassable"), patch("run.pr_inspect", return_value=merged_pr), patch("run.provider_with_retries", return_value=subprocess.CompletedProcess([], 0, "", "")) as provider:
                self.assertTrue(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 1}, "abc"))
            self.assertIn("--admin", provider.call_args.args)
            self.assertEqual(provider.call_args.args[provider.call_args.args.index("--match-head-commit") + 1], "abc")

            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["pullRequests"][assignment["id"]]["state"] = "OPEN"
            with patch("run.refresh_integration_base", return_value=True), patch("run.wait_for_checks", return_value="bypassable"), patch("run.provider_with_retries", side_effect=RuntimeError("denied")), patch("run.invoke_with_replacements") as worker:
                self.assertFalse(run.merge_assignment(store, threading.Semaphore(1), assignment, Path(root), "branch", {"number": 1}, "abc"))
            self.assertEqual(store.state["taskStates"][assignment["id"]]["phase"], "waiting-provider")
            self.assertIn("provider.log", store.state["taskStates"][assignment["id"]]["providerStatus"])
            self.assertEqual(run.fix_attempts_started(store.state, assignment["id"]), 0)
            worker.assert_not_called()

    def test_github_unblocked_merge_retains_non_admin_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().backlog_task()
            subject, body, _digest = run.canonical_merge_metadata(store.state, assignment, "abc")
            with patch("run.provider_with_retries") as provider:
                run.pr_merge(store, "merge", 1, subject=subject, body=body)
            self.assertNotIn("--admin", provider.call_args.args)
            self.assertNotIn("--match-head-commit", provider.call_args.args)
            self.assertEqual(provider.call_args.args[provider.call_args.args.index("--subject") + 1], "Do the thing")
            self.assertEqual(provider.call_args.args[provider.call_args.args.index("--body") + 1], "")
            merge_args = "\n".join(map(str, provider.call_args.args[2:]))
            for value in ("test", "TASK-0001", "abc", "Relay-Campaign", "Relay-Assignment", "Relay-Source", "Relay-Candidate"):
                self.assertNotIn(value, merge_args)
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
            assignment = ContractTests().backlog_task()
            subject, body, _digest = run.canonical_merge_metadata(store.state, assignment, "abc")
            with patch("run.provider_with_retries") as provider:
                run.pr_merge(store, "merge", 7, subject=subject, body=body)
                self.assertIn("true", provider.call_args.args)
                message = provider.call_args.args[provider.call_args.args.index("--merge-commit-message") + 1]
                self.assertEqual(message, "Do the thing")
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

    def test_restarts_preserve_repair_history_without_a_fixed_repair_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["taskStates"]["TASK-0001"] = {"phase": "repair-1", "fixAttemptsStarted": 0}
            store.save()
            run.reserve_fix(store, "TASK-0001")
            run.reserve_fix(store, "TASK-0001")
            for _ in range(100):
                state = json.loads(store.path.read_text(encoding="utf-8"))
                store = run.StateStore(store.path, state)
            run.reserve_fix(store, "TASK-0001")
            final = json.loads(store.path.read_text(encoding="utf-8"))["taskStates"]["TASK-0001"]
            self.assertEqual(final["fixAttemptsStarted"], 3)

    def test_campaign_agent_call_counter_is_persisted_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            store.state["campaignAgentCallLimit"] = 3
            store.save()
            for _ in range(3):
                run._consume_agent_call(store, "TASK-0001", "worker", "task", False, False)
            with self.assertRaisesRegex(RuntimeError, "agent-call resource ceiling"):
                run._consume_agent_call(store, "TASK-0001", "worker", "task", False, False)
            persisted = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual((persisted["campaignAgentCallsStarted"], persisted["attemptCounters"]["TASK-0001"]), (3, 3))

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
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "old", "reviewCallsStarted": 3, "reviewCallLimit": 9}
            completed = subprocess.CompletedProcess([], 0, "base\n", "")
            def agent(*args, **kwargs):
                role = args[4]
                store.state["reviewSessions"][assignment["id"]]["reviewCallsStarted"] += 1
                if role == "worker":
                    return {"mode": "repair", "assignmentId": assignment["id"], "status": "candidate", "candidateSha": "new", "validation": [], "summary": ""}
                return {"assignmentId": assignment["id"], "mode": "incremental", "reviewEpoch": 1, "candidateSha": "new", "resolvedFindingIds": [], "findings": []}
            with patch("run.refresh_integration_base", return_value=True), patch("run.wait_for_checks", side_effect=["failed", "passed"]), patch("run.candidate_integrity"), patch("run.invoke_with_replacements", side_effect=agent), patch("run.validate_candidate", return_value="new"), patch("run.publish_candidate"), patch("run.validate_publication_proof"), patch("run.inspect_merged_pr", return_value={"number": 1, "state": "MERGED", "url": "x", "headRefOid": "new"}), patch("run.provider_with_retries", return_value=completed), patch("run.mark_integrated"):
                self.assertTrue(run.merge_assignment(store, __import__("threading").Semaphore(1), assignment, Path(root), "relay/TASK-0001", {"number": 1}, "old"))
            session = store.state["reviewSessions"][assignment["id"]]
            self.assertEqual((run.fix_attempts_started(store.state, assignment["id"]), session["reviewCallsStarted"], session["reviewedSha"]), (1, 5, "new"))
            self.assertEqual(session["phase"], "approved")

    def test_existing_pr_and_merged_pr_are_not_duplicated(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            pr = {"number": 1, "state": "OPEN", "url": "x", "headRefOid": "sha"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "sha", "pr": pr, "phase": "approved"}
            store.state["pullRequests"][assignment["id"]] = pr
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "sha", pr)
            with patch("run.pr_inspect", return_value=pr), patch("run.provider_call") as provider:
                self.assertIs(run.publish_candidate(store, assignment, Path(root), "branch", "sha"), pr)
                provider.assert_not_called()
            merged = pr | {"state": "MERGED"}
            def already_merged(*_args):
                run.persist_pr_inspection(store, assignment["id"], merged)
                return "merged"
            with patch("run.refresh_integration_base", return_value=True), patch("run.wait_for_checks", side_effect=already_merged), patch("run.provider_with_retries") as merge:
                self.assertTrue(run.merge_assignment(store, __import__("threading").Semaphore(1), assignment, Path(root), "branch", pr, "sha"))
                merge.assert_not_called()

    def test_publication_proof_is_rejected_before_provider_merge(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            pr = {"number": 1, "state": "OPEN", "url": "x", "headRefOid": "abc"}
            store.state["taskStates"][assignment["id"]] = {"phase": "approved"}
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", pr)
            store.state["taskStates"][assignment["id"]]["publicationProof"]["candidateSha"] = "stale"
            with patch("run.refresh_integration_base", return_value=True), patch("run.wait_for_checks", return_value="passed"), patch("run.pr_merge") as merge, patch("run.mark_integrated") as integrated, self.assertRaisesRegex(RuntimeError, "publication proof"):
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
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "reviewedSha": "abc", "reviewCallsStarted": 3, "reviewCallLimit": 9}
            self.provider_metadata(store, assignment, "abc", opened)
            store.state["taskStates"][assignment["id"]].pop("prMetadata")
            def provider_merged(*_args):
                run.persist_pr_inspection(store, assignment["id"], merged)
                return "merged"
            with patch("run.refresh_integration_base", return_value=True), patch("run.wait_for_checks", side_effect=provider_merged), patch("run.pr_edit") as edit, patch("run.pr_merge") as merge:
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
            self.assertEqual(title, "test/TASK-0001: Do the thing")
            for value in ("Campaign:", "Qualified assignment:", "Current candidate SHA:", "## Focused validation", "## Campaign validation"):
                self.assertIn(value, body)
            self.assertNotIn("Source reference", body)
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

    def test_canonical_merge_metadata_is_assignment_title_only(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().backlog_task()
            subject, body, digest = run.canonical_merge_metadata(store.state, assignment, "abc")
            self.assertEqual((subject, body), ("Do the thing", ""))
            self.assertEqual(digest, hashlib.sha256(b"Do the thing\n").hexdigest())

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
            rendered = command.call_args.kwargs["input"]
            self.assertIn('"projectInstructions":[{"content":"custom target rules","scope":"."}]', rendered)
            self.assertIn("Context packet:", rendered)
            record = store.state["promptRecords"][0]
            prompt_path = Path(root) / record["promptPath"]
            self.assertEqual(hashlib.sha256(prompt_path.read_bytes()).hexdigest(), record["promptSha256"])
            self.assertEqual(record["templateSha256"], hashlib.sha256(run.prompt_template("worker").encode()).hexdigest())
            self.assertNotEqual(command.call_args.kwargs["env"].get("PYTHONUSERBASE"), os.environ.get("PYTHONUSERBASE"))
            self.assertEqual(store.state["attemptCounters"]["TASK-0001"], 1)
            self.assertEqual(store.state["activeProcesses"], {})
            with patch("run.run_tool", side_effect=subprocess.TimeoutExpired("gh", 1)), self.assertRaises(subprocess.TimeoutExpired):
                run.provider_call(store, "operation", "repo", "view")
            self.assertEqual(store.state["providerAttemptCounters"]["operation"], 1)

    def test_worker_context_selects_dependency_history_bugs_attempts_and_learnings(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            dependency = ContractTests().task("TASK-0001"); dependency["allowedPaths"] = ["shared/base.py"]
            assignment = ContractTests().task("TASK-0002", ["TASK-0001"]); assignment["allowedPaths"] = ["app/feature.py"]
            tasks_text = plan.render_tasks([dependency, assignment], store.state["baseSha"], store.state["requirementsHash"], campaign_validation_commands=store.state["campaignValidationCommands"])
            Path(root, "tasks.md").write_text(tasks_text, encoding="utf-8")
            store.state["planDigest"] = run.parse_tasks(tasks_text)[0]["planDigest"]
            store.state["targetInstructions"] = "root rules"
            Path(root, "app").mkdir()
            Path(root, "app", "AGENTS.md").write_text("nested rules", encoding="utf-8")
            store.state["taskStates"] = {
                "TASK-0001": {"phase": "integrated", "mergedSha": "abc", "workerSummary": "built base", "changedPaths": ["shared/base.py"]},
                "TASK-0002": {"phase": "repair-1", "validationHistory": [{"outcome": "failed"}], "attemptHistory": [{"summary": "first try"}], "visitedFingerprints": ["one"]},
            }
            store.state["reviewSessions"]["TASK-0002"] = {"phase": "repair-1"}
            store.state["learnings"] = [{"scope": ["shared/base.py"], "fact": "use base helper", "sourceAssignment": "TASK-0001", "evidenceSha": "abc", "status": "active"}]
            bug = ContractTests().backlog_bug(); bug.update(status="active", source="TASK-0009", allowedPaths=["app/feature.py"]); bug.pop("deferralReason")
            run.write_bugs(store, [bug])
            packet = run.context_packet(store, Path(root), "TASK-0002", "worker", "repair", "repair this")
            self.assertEqual([item["content"] for item in packet["projectInstructions"]], ["root rules", "nested rules"])
            self.assertEqual(packet["dependencies"][0]["mergedSha"], "abc")
            self.assertEqual(packet["relevantBugs"][0]["id"], "BUG-0001")
            self.assertEqual(packet["previousAttempts"][0]["summary"], "first try")
            self.assertEqual(packet["validationEvidence"][0]["outcome"], "failed")
            self.assertEqual(packet["learnings"][0]["fact"], "use base helper")

    def test_all_agent_roles_have_bounded_prompt_and_schema(self):
        self.assertEqual(set(run.ROLE_JSON_SCHEMAS), set(run.AGENT_SCHEMAS))
        self.assertNotIn("implementer", run.ROLE_JSON_SCHEMAS)
        self.assertNotIn("repairer", run.ROLE_JSON_SCHEMAS)
        for role, mode in (("slice-reviewer", "initial"), ("verification-reviewer", "incremental")):
            self.assertEqual(run.ROLE_JSON_SCHEMAS[role]["properties"]["mode"], {"type": "string", "enum": [mode]})
        self.assertEqual(run.ROLE_JSON_SCHEMAS["slice-reviewer"]["properties"]["resolvedFindingIds"]["items"], {"type": "string"})
        self.assertEqual({path.name for path in run.PROMPTS.glob("*.md")}, {"scout.md", "planner.md", "plan-reviewer.md", "worker.md", "reviewer.md", "audit-planner.md", "auditor.md"})
        self.assertIn("AUDIT-NNNN", run.prompt_template("audit-planner"))

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

    def test_complete_cleanup_preview_then_confirm(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve(); relay = root / ".relay"; relay.mkdir(); (root / ".git" / "info").mkdir(parents=True)
            campaign = "test"; marker = f"<!-- relay: campaign={campaign} repository={__import__('hashlib').sha256(str(root).encode()).hexdigest()[:12]} -->"
            (root / "tasks.md").write_text("# Tasks\n\n<!-- relay: planned-base=0123456 requirements=abc123 -->\n", encoding="utf-8")
            (root / "bugs.md").write_text(f"# Bugs\n\n{marker}\n", encoding="utf-8")
            relay_plan = plan.render_tasks([ContractTests().task()], "0123456", "abc123", campaign_validation_commands=["python -m unittest"])
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
            store.state["workItems"][assignment["id"]] = {"id": assignment["id"], "type": "task", "status": "integrated"}
            store.save()
            with self.assertRaisesRegex(RuntimeError, "provider proof"):
                run.permanent_cleanup(root, False)
            record = {"number": 1, "url": "x", "headRefOid": "abc", "state": "MERGED"}
            self.provider_metadata(store, assignment, "abc", record)
            metadata = store.state["taskStates"][assignment["id"]]["prMetadata"]
            store.state["taskStates"][assignment["id"]].update(
                phase="integrated", providerProof={
                    "finalCandidate": "abc", "prMetadataHash": metadata["hash"],
                    "mergeMetadataHash": "legacy-nonempty-proof", "mergedProviderRecord": record,
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
            self.assertIn("extract incomplete requirements and backlog items", completed.stderr)
            self.assertIn("do not edit state.json", completed.stderr)
            self.assertEqual(json.loads((relay / "state.json").read_text()), state)

    def test_untracked_custom_agents_requires_user(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            agents = target / "AGENTS.md"; agents.write_bytes(b"custom\r\n")
            args = run.parser().parse_args(["--repo", str(target)])
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123", campaign_validation_commands=["python -c \"print('baseline')\""])
            store, _ = run.initialize_campaign(target, text, args)
            self.assertEqual(agents.read_bytes(), b"custom\r\n")
            self.assertEqual(store.state["phase"], "needs-user")
            self.assertEqual(store.state["agentsBootstrap"]["providerStatus"], "untracked-custom-agents")
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
            self.assertEqual(store.state["agentsBootstrap"]["providerStatus"], "untracked-custom-agents")
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
            self.assertIn("SUMMARY    phase=complete tasks=1/1 completed", completed.stderr)
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
            self.assertTrue(state["auditPlanStarted"] and state["auditPlanCompleted"] and state["auditDispositionsCompleted"])
            self.assertEqual(len(state["auditScopes"]), 1)
            self.assertTrue(next(iter(state["auditScopes"].values()))["completed"])
            self.assertLessEqual(state["auditCallsStarted"], state["auditCallLimit"])
            self.assertEqual(state["worktrees"], {})
            self.assertEqual(state["agentsBootstrap"]["phase"], "complete")
            self.assertNotIn("AGENTS", state["reviewSessions"])
            self.assertTrue(all(value["phase"] == "integrated" for value in state["taskStates"].values()))
            self.assertTrue(all(session["reviewResult"] is not None for session in state["reviewSessions"].values()))
            self.assertEqual(sorted(session["reviewCallsStarted"] for session in state["reviewSessions"].values()), [1, 3])
            self.assertTrue(any(item["type"] == "integration-repair" and item["status"] == "integrated" for item in state["workItems"].values()))
            spans = {task: {action: float(Path(f"{events}.{task}.{action}").read_text()) for action in ("start", "end")} for task in ("TASK-0001", "TASK-0002")}
            self.assertLess(max(spans[task]["start"] for task in spans), min(spans[task]["end"] for task in spans))
            self.assertEqual(len(list(provider.glob("*.json"))), 3)
            records = [json.loads(path.read_text()) for path in provider.glob("*.json")]
            self.assertTrue(all(record.get("operations") == ["merge-bypass"] for record in records if "agents-bootstrap" not in record["branch"]))
            self.assertEqual(next(record for record in records if "agents-bootstrap" in record["branch"])["operations"], ["merge"])
            self.assertEqual(state["providerAttemptCounters"]["AGENTS:pr-create"], 1)
            self.assertEqual(git_output(target, "show", "HEAD:one.txt"), "TASK-0001\n")
            self.assertEqual(git_output(target, "show", "HEAD:two.txt"), "TASK-0002\n")
            self.assertEqual(git_output(target, "show", "HEAD:AGENTS.md"), repo.TARGET_AGENTS)
            self.assertTrue(all(state["providerAttemptCounters"][f"{task}:pr-create"] == 1 for task in ("TASK-0001", "TASK-0002")))
            self.assertIn("--repo', 'fake/relay", (target / ".relay" / "logs" / "provider.log").read_text(encoding="utf-8"))
            before = (target / ".relay" / "state.json").read_bytes()
            shown = subprocess.run([sys.executable, str(Path(status.__file__)), "--repo", str(target)], capture_output=True, text=True, check=True)
            self.assertIn("phase=complete tasks=2/2 completed", shown.stdout)
            self.assertIn("RESOURCES agent-calls=8/100", shown.stdout)
            self.assertIn("NEXT Preview cleanup with:", shown.stdout)
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
            self.assertEqual(state["auditCallsStarted"], 2)
            self.assertEqual(run.parse_bugs((target / "bugs.md").read_text(encoding="utf-8"))[1][0]["status"], "resolved")
            self.assertEqual(len(list(provider.glob("*.json"))), 3)

    def test_review_repair_and_backlog_complete_end_to_end(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            fake_codex, fake_gh = root / "fake_codex.py", root / "fake_gh.py"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8"); fake_gh.write_text(FAKE_GH, encoding="utf-8")
            provider = root / "provider"; provider.mkdir()
            requirements = root / "requirements.md"; requirements.write_text("Create one file.", encoding="utf-8")
            environment = os.environ | VALIDATION_ENV | {"RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}", "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_GH_STATE": str(provider), "FAKE_REVIEW_MIX": "1"}
            subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements)], capture_output=True, text=True, env=environment, check=True)
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"], capture_output=True, text=True, env=environment, timeout=60)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + json.dumps(state, indent=2))
            bugs = run.parse_bugs((target / "bugs.md").read_text(encoding="utf-8"))[1]
            self.assertEqual([bug["status"] for bug in bugs], ["resolved", "backlog"])
            self.assertEqual(state["taskStates"]["TASK-0001"]["phase"], "integrated")
            self.assertTrue((target / "BACKLOG.md").is_file())

    def test_adversarial_review_terminates_on_no_progress(self):
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
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--workers", "2", "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "1"], input=text, capture_output=True, text=True, env=environment, timeout=60)
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            review = state["reviewSessions"]["TASK-0001"]
            self.assertEqual((review["phase"], run.fix_attempts_started(state, "TASK-0001")), ("blocked", 3))
            self.assertIn("repeated progress fingerprint", state["taskStates"]["TASK-0001"]["error"])
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
    tasks = [{"id": "TASK-0001", "title": "Add one file", "objective": "Create the first requested file.", "requirementContext": ["Create two independent files."], "nonGoals": ["Do not change unrelated files."], "downstreamConsumer": "", "status": "ready", "priority": "P1", "dependencies": [], "allowedPaths": ["one.txt"], "acceptanceCriteria": ["File exists."], "validationCommands": ["python -c \"from pathlib import Path; assert Path('one.txt').is_file()\""]}]
    if os.environ.get("FAKE_BACKLOG"):
        command = "python -c \"from pathlib import Path; assert Path('test_one.txt').is_file()\""
        tasks[0].update(allowedPaths=["one.txt", "test_one.txt"], validationCommands=[command])
    if os.environ.get("FAKE_TWO_TASKS"):
        tasks.append({"id": "TASK-0002", "title": "Add another file", "objective": "Create the second requested file.", "requirementContext": ["Create two independent files."], "nonGoals": ["Do not change unrelated files."], "downstreamConsumer": "", "status": "ready", "priority": "P1", "dependencies": [], "allowedPaths": ["two.txt"], "acceptanceCriteria": ["File exists."], "validationCommands": ["python -c \"from pathlib import Path; assert Path('two.txt').is_file()\""]})
    result = {"campaignObjective": "Create the requested files.", "campaignValidationCommands": ["python -c \"print('baseline')\""], "tasks": tasks}
elif "Role: plan-reviewer" in prompt:
    result = {"assignmentId": "PLAN", "candidateSha": candidate, "findings": []}
elif "Role: Worker" in prompt:
    mode = re.search(r"Mode: (task|bug|repair|integration-repair)", prompt).group(1)
    allowed = json.loads(re.search(r"Allowed paths: (\[[^\n]+\])", prompt).group(1))
    if mode == "integration-repair":
        subprocess.run(["git", "-C", cwd, "merge", "--no-edit", "origin/main"], check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        result = {"mode": mode, "assignmentId": assignment, "status": "candidate", "candidateSha": sha, "changedPaths": [allowed[0]], "validation": [], "summary": "integrated main"}
    else:
        path = os.path.join(cwd, allowed[0])
        os.makedirs(os.path.dirname(path) or cwd, exist_ok=True)
        events = os.environ.get("FAKE_EVENTS")
        if events and mode == "task":
            with open(f"{events}.{assignment}.start", "w", encoding="utf-8") as stream: stream.write(str(time.time()))
            time.sleep(.35)
        if os.environ.get("FAKE_ADVERSARIAL") and mode == "repair":
            previous = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            content = assignment + (" repair-a\n" if "repair-b" in previous or "repair-a" not in previous else " repair-b\n")
        else:
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
        result = {"mode": mode, "assignmentId": assignment, "status": "candidate", "candidateSha": sha, "changedPaths": changed, "validation": [], "summary": "done"}
elif "Role: slice-reviewer" in prompt:
    findings = []
    if os.environ.get("FAKE_ADVERSARIAL"):
        findings = [{"severity": "P1", "location": "file.txt:1", "failure": "always fails", "reproduction": "python -c \"raise SystemExit(1)\"", "requirement": "must pass", "evidence": "deterministic failure", "candidateIntroduced": True, "affectedPaths": ["file.txt"]}]
    if os.environ.get("FAKE_REVIEW_MIX"):
        findings = [
            {"severity": "P1", "location": "one.txt:1", "failure": "candidate needs repair", "reproduction": "inspect one.txt", "requirement": "File exists.", "evidence": "initial content", "candidateIntroduced": True, "affectedPaths": ["one.txt"]},
            {"severity": "P2", "location": "one.txt:1", "failure": "minor follow-up", "reproduction": "inspect one.txt", "requirement": "File exists.", "evidence": "minor", "candidateIntroduced": True, "affectedPaths": ["one.txt"]},
        ]
    result = {"assignmentId": assignment, "mode": "initial", "reviewEpoch": 0, "candidateSha": candidate, "resolvedFindingIds": [], "findings": findings}
elif "Role: verification-reviewer" in prompt:
    packet = json.loads(prompt.split("Context packet:\n", 1)[1].split("\n\nRequired output schema:", 1)[0])
    invocation = json.loads(packet["invocation"])
    epoch = invocation["context"]["expectedReviewEpoch"]
    open_ids = invocation["context"]["openFindingIds"]
    result = {"assignmentId": assignment, "mode": "incremental", "reviewEpoch": epoch, "candidateSha": candidate, "resolvedFindingIds": [] if os.environ.get("FAKE_ADVERSARIAL") else open_ids, "findings": []}
elif "Role: audit-planner" in prompt:
    paths = ["audit_fix.txt"] if os.environ.get("FAKE_AUDIT_BUG") else ["README.md"]
    result = {"scopes": [{"scopeId": "AUDIT-0001", "scope": "fixture", "requirements": ["audit fix exists"], "paths": paths, "commands": ["python -c \"from pathlib import Path; assert Path('audit_fix.txt').is_file()\""] if os.environ.get("FAKE_AUDIT_BUG") else ["python -c \"print('audited')\""], "completionCondition": "scope inspected"}]} if os.environ.get("FAKE_AUDIT_SCOPE") else {"scopes": []}
elif "Role: audit-worker" in prompt:
    findings = [{"severity": "P1", "location": "audit_fix.txt:1", "failure": "audit fix is missing", "reproduction": "Observe that audit_fix.txt is absent.", "requirement": "audit fix exists", "evidence": "file absent", "candidateIntroduced": False, "affectedPaths": ["audit_fix.txt"]}] if os.environ.get("FAKE_AUDIT_BUG") else []
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
    remote = root.parent / "remote.git"
    branch_head = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{record['branch']}"], capture_output=True, text=True)
    if branch_head.returncode == 0:
        record["headRefOid"] = branch_head.stdout.strip(); path.write_text(json.dumps(record))
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
