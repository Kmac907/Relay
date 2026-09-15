import io
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

    def test_plan_round_trip(self):
        text = plan.render_tasks([self.task()], "0123456789abcdef", "abc123")
        metadata, tasks = run.parse_tasks(text)
        self.assertEqual(metadata["baseSha"], "0123456789abcdef")
        self.assertEqual(tasks[0]["id"], "TASK-0001")

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
        state = run.initial_state(Path(root), {"baseSha": "0123456", "requirementsHash": "abc123"}, args)
        state["campaignId"] = "test"
        path = Path(root) / ".relay" / "state.json"
        Path(root, "tasks.md").write_text(plan.render_tasks([ContractTests().task()], "0123456", "abc123", args.task_attempts, args.fix_loops), encoding="utf-8")
        Path(root, "bugs.md").write_text(run.render_bugs("test", Path(root)), encoding="utf-8")
        return run.StateStore(path, state)

    def azure_store(self, root, merge_method="squash"):
        store = self.state_store(root)
        store.state.update(provider="azure-devops", azureOrganization="my org", azureProject="My Project", azureRepository="My Repo", mergeMethod=merge_method)
        return store

    def azure_pr(self, status="active", merge_status="succeeded", sha="abc"):
        return {"pullRequestId": 7, "status": status, "mergeStatus": merge_status, "lastMergeSourceCommit": {"commitId": sha}}

    def test_validation_uses_explicit_platform_shells(self):
        command = "$items = @('one', 'two'); $items | ForEach-Object { $_ }"
        with patch.object(run.os, "name", "nt"), patch("run.tool_command", return_value=[r"C:\Tools\pwsh.exe"]):
            self.assertEqual(run.validation_command(command), [r"C:\Tools\pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command])
        with patch.object(run.os, "name", "posix"):
            self.assertEqual(run.validation_command(command), ["/bin/sh", "-c", command])

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
            self.assertEqual(actions, [{"action": "resume-publish", "assignmentId": assignment["id"], "candidateSha": "new"}])
            self.assertEqual(store.path.read_bytes(), before)
            store.state["attemptCounters"][assignment["id"]] = store.state["taskAttemptLimit"]
            store.state["recoveryAttemptGrants"] = {assignment["id"]: 1}
            number, process_id = run._consume_agent_call(store, assignment["id"], "worker", "task", False, False)
            self.assertEqual(number, "recovery-1")
            store.state["activeProcesses"].pop(process_id)
            with self.assertRaisesRegex(RuntimeError, "attempt limit"):
                run._consume_agent_call(store, assignment["id"], "worker", "task", False, False)

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
            self.assertNotRegex(description, r"[\r\n]")
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
            unittest.mock.call("BLOCKED", "assignment=AGENTS reason=bootstrap failed log=not-recorded"),
            unittest.mock.call("BLOCKED", "assignment=CAMPAIGN reason=campaign failed log=not-recorded"),
            unittest.mock.call("BLOCKED", "assignment=TASK-0001 reason=task failed log=not-recorded"),
            unittest.mock.call("BLOCKED", "assignment=TASK-0002 reason=checks pending log=not-recorded"),
        ])

    def test_sparse_campaign_without_operation_fields_has_progress(self):
        state = {"workerLimit": 3, "taskTotal": 1, "taskStates": {"TASK-0001": {"phase": "ready"}}, "activeProcesses": {}}
        self.assertEqual(run.runtime_progress(state, 0), "0/1 complete | active 0/3")

    def test_provider_progress_names_actual_pr_policy_and_deadline(self):
        state = {
            "workerLimit": 3, "taskTotal": 1, "activeProcesses": {},
            "pullRequests": {"TASK-0001": {"number": 25}},
            "taskStates": {"TASK-0001": {
                "phase": "provider-checks", "operation": "provider-checks",
                "operationStartedAt": datetime.fromtimestamp(100, timezone.utc).isoformat(),
                "operationDeadline": 3700, "providerPolicyCounts": {"queued": 2}, "nextAction": "poll",
            }},
        }
        line = run.runtime_progress(state, 970)
        self.assertIn("TASK-0001 provider-checks", line)
        self.assertIn("PR #25", line)
        self.assertIn("queued 2", line)
        self.assertIn("next=poll", line)

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
            self.assertIn("TASK-0001: PR #25 status=pending", shown)

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
        live = run.runtime_progress(state, 0)
        self.assertIn("TASK-0001 provider-approve", live)
        self.assertIn("TASK-0002 merge-bypass", live)
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
            merged = subprocess.CompletedProcess([], 0, "{}", "")
            with patch("run.provider_with_retries", side_effect=[RuntimeError("vote denied"), merged]) as provider, patch("run.wait_for_checks", return_value="passed"), patch("run.invoke_with_replacements") as worker:
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
            with patch("run.wait_for_checks", return_value="bypassable"), patch("run.provider_with_retries", return_value=subprocess.CompletedProcess([], 0, "", "")) as provider:
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
                run.pr_merge(store, "merge", 1)
            self.assertNotIn("--admin", provider.call_args.args)
            self.assertNotIn("--match-head-commit", provider.call_args.args)

    def test_azure_sha_drift_and_merge_modes(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.azure_store(root)
            drift = subprocess.CompletedProcess([], 0, json.dumps(self.azure_pr(sha="changed")), "")
            with patch("run.run_tool", return_value=drift):
                self.assertEqual(run.wait_for_checks(store, "DRIFT", {"number": 7}, "abc"), "sha-drift")
            with patch("run.provider_with_retries") as provider:
                run.pr_merge(store, "merge", 7)
                self.assertIn("true", provider.call_args.args)
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
            with patch("run.wait_for_checks", side_effect=["failed", "passed"]), patch("run.git", return_value=completed), patch("run.invoke_with_replacements", side_effect=agent), patch("run.validate_candidate", return_value="new"), patch("run.publish_candidate"), patch("run.provider_with_retries", return_value=completed):
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
            with patch("run.provider_call") as provider:
                self.assertIs(run.publish_candidate(store, assignment, Path(root), "branch", "sha"), pr)
                provider.assert_not_called()
            with patch("run.wait_for_checks", return_value="merged"), patch("run.provider_with_retries") as merge:
                self.assertTrue(run.merge_assignment(store, __import__("threading").Semaphore(1), assignment, Path(root), "branch", pr, "sha"))
                merge.assert_not_called()

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
                with patch("run.pr_discover", return_value=[old]), patch("run.pr_create", return_value=new) as create:
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
            with patch("run.git_provider_with_retries", return_value=completed) as git_provider, patch("run.pr_inspect", return_value=fresh) as inspect:
                self.assertEqual(run.publish_candidate(store, assignment, Path(root), "relay/TASK-0001", "60d09d4a"), fresh)
            inspect.assert_called_once_with(store, "TASK-0001:pr-refresh:60d09d4a", 30)
            self.assertIn("--force-with-lease=refs/heads/relay/TASK-0001:f63189a5", git_provider.call_args_list[1].args)

    def test_push_before_pr_refresh_resumes_refresh_without_repush(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            assignment = ContractTests().task()
            stale = {"number": 30, "state": "OPEN", "url": "old", "headRefOid": "old"}
            fresh = stale | {"headRefOid": "new"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "new", "pr": stale, "phase": "approved"}
            with patch("run.git_provider_with_retries") as git_provider, patch("run.pr_inspect", return_value=fresh):
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
            self.assertEqual((root / ".git" / "info" / "exclude").read_text(encoding="utf-8"), "keep.me\n")

    def test_campaign_initialization_generates_only_missing_agents(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            args = run.parser().parse_args(["--repo", str(target)])
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123")
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
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123")
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
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123")
            store, _ = run.initialize_campaign(target, text, args)
            self.assertEqual(agents.read_bytes(), b"tracked custom\n")
            self.assertIsNone(store.state["agentsBootstrap"])

    def test_modified_generated_agents_requires_user(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); target = make_git_repository(root)
            content = repo.TARGET_AGENTS + "customized\n"
            (target / "AGENTS.md").write_text(content, encoding="utf-8", newline="\n")
            args = run.parser().parse_args(["--repo", str(target)])
            text = plan.render_tasks([ContractTests().task()], git_output(target, "rev-parse", "HEAD").strip(), "abc123")
            store, tasks = run.initialize_campaign(target, text, args)
            self.assertEqual(store.state["phase"], "needs-user")
            self.assertEqual(store.state["agentsBootstrap"]["providerStatus"], "generated-file-modified")
            self.assertEqual((target / "AGENTS.md").read_text(encoding="utf-8"), content)
            with patch("run.provider_preflight") as preflight, patch("run.run_assignments") as workers:
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
            self.assertIn("assignment=AGENTS reason=Azure DevOps operation exhausted attempts: AGENTS:pr-create", second.stderr)

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
            text = plan.render_tasks([task], git_output(target, "rev-parse", "HEAD").strip(), "abc123")
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
    if os.environ.get("FAKE_TWO_TASKS"):
        tasks.append({"id": "TASK-0002", "title": "Add another file", "status": "ready", "priority": "P1", "dependencies": [], "allowedPaths": ["two.txt"], "acceptanceCriteria": ["File exists."], "validationCommands": ["python -c \"from pathlib import Path; assert Path('two.txt').is_file()\""]})
    result = {"tasks": tasks}
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
    subprocess.run(["git", "-C", cwd, "add", allowed[0]], check=True)
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
    sha = re.search(r"Candidate: ([0-9a-f]+)", body).group(1); number = int(re.search(r"(\d+)$", branch).group(1))
    file_for(branch).write_text(json.dumps({"number": number, "url": f"https://example.invalid/{number}", "headRefOid": sha, "state": "OPEN", "branch": branch}))
    print(f"https://example.invalid/{number}"); raise SystemExit(0)
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
    if "\r" in description or "\n" in description: raise SystemExit("multiline Azure description")
    attempts = root / ".create-attempts"
    attempt = int(attempts.read_text()) + 1 if attempts.exists() else 1
    attempts.write_text(str(attempt))
    if attempt <= int(os.environ.get("FAKE_AZ_FAIL_CREATE_ATTEMPTS", "0")):
        print("simulated create failure", file=sys.stderr); raise SystemExit(1)
    sha = re.search(r"Candidate: ([0-9a-f]+)", description).group(1)
    number = int(re.search(r"(\d+)$", branch).group(1))
    record = {"pullRequestId": number, "status": "active", "mergeStatus": "succeeded", "lastMergeSourceCommit": {"commitId": sha}, "branch": branch}
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
            if "agents-bootstrap" in record["branch"]:
                subprocess.run(["git", "--git-dir", os.environ["FAKE_AZ_REMOTE"], "update-ref", "refs/heads/main", record["lastMergeSourceCommit"]["commitId"]], check=True)
            record["status"] = "completed"; path.write_text(json.dumps(record)); print(json.dumps(record)); raise SystemExit(0)
raise SystemExit(f"unknown az args: {args}")
''')


if __name__ == "__main__":
    unittest.main()
