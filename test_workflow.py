import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import plan
import repo
import run
import status


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
            bug = {"id": "BUG-0001", "title": "Broken", "severity": "P1", "status": "active", "source": "audit", "location": "x.py:1", "failure": "fails", "reproduction": "python x.py", "requirement": "works", "evidence": "exit 1"}
            text = run.render_bugs("campaign", Path(root), [bug])
            metadata, bugs = run.parse_bugs(text)
            self.assertEqual(metadata["campaignId"], "campaign")
            self.assertEqual(bugs[0]["reproduction"], "python x.py")

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
        value = {"mode": "repair", "assignmentId": "TASK-0001", "status": "candidate", "candidateSha": "abc", "changedPaths": [], "validation": [], "summary": ""}
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


class RepositoryTests(unittest.TestCase):
    def completed(self, stdout=""):
        return subprocess.CompletedProcess([], 0, stdout, "")

    def test_creates_local_repository(self):
        with tempfile.TemporaryDirectory() as root, patch("repo.run") as command:
            command.side_effect = [self.completed(), self.completed(), self.completed(), self.completed("main\n"), self.completed("abcdef\n")]
            path, branch, sha = repo.create(Path(root) / "demo")
            self.assertEqual((path / "README.md").read_text(encoding="utf-8"), "# demo\n")
            self.assertEqual((branch, sha), ("main", "abcdef"))
            self.assertEqual(command.call_args_list[0].args[:4], ("git", "-C", str(path), "init"))

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

    def test_visibility_is_required_for_github(self):
        with self.assertRaises(SystemExit):
            repo.parser().parse_args(["--path", "x", "--github", "owner/x", "--private", "--public"])

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
        with self.assertRaises(ValueError):
            plan.validate_scout(value, "src")

    def test_hung_planning_call_consumes_budget(self):
        with tempfile.TemporaryDirectory() as root, patch("plan.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", .01)):
            budget = plan.CallBudget(1)
            with self.assertRaises(subprocess.TimeoutExpired):
                plan.invoke_agent(Path(root), "prompt", {"type": "object"}, .01, budget)
            self.assertEqual(budget.started, 1)

    def test_real_plan_to_dry_run_pipe_is_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = make_git_repository(root)
            requirements = root / "requirements.md"
            requirements.write_text("Add one file.", encoding="utf-8")
            fake = root / "fake_codex.py"
            fake.write_text(FAKE_CODEX, encoding="utf-8")
            environment = os.environ | {"RELAY_CODEX": f"{sys.executable} {fake}"}
            before = git_output(target, "status", "--porcelain=v1", "--untracked-files=all")
            planned = subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements), "--workers", "2"], capture_output=True, text=True, env=environment, check=True)
            self.assertTrue(planned.stdout.startswith("# Tasks\n"))
            self.assertIn("Relay Planner", planned.stderr)
            dry = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--dry-run"], input=planned.stdout, capture_output=True, text=True)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(git_output(target, "status", "--porcelain=v1", "--untracked-files=all"), before)

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
            environment = os.environ | {"RELAY_CODEX": f"{sys.executable} {fake}", "FAKE_SCOUT_EVENTS": str(events), "FAKE_REQUIRE_EVIDENCE": "1"}
            before = git_output(target, "status", "--porcelain=v1", "--untracked-files=all")
            completed = subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements), "--workers", "2"], capture_output=True, text=True, env=environment, check=True)
            starts = [float(path.read_text()) for path in root.glob("scout.*.start")]
            ends = [float(path.read_text()) for path in root.glob("scout.*.end")]
            self.assertEqual((len(starts), len(ends)), (2, 2))
            self.assertLess(max(starts), min(ends))
            self.assertTrue(completed.stdout.startswith("# Tasks"))
            self.assertEqual(git_output(target, "status", "--porcelain=v1", "--untracked-files=all"), before)


class DeterministicCoreTests(unittest.TestCase):
    def state_store(self, root, fix_loops=2, format_retries=2):
        args = run.parser().parse_args(["--repo", str(root), "--fix-loops", str(fix_loops), "--format-retries", str(format_retries)])
        state = run.initial_state(Path(root), {"baseSha": "0123456"}, args)
        state["campaignId"] = "test"
        path = Path(root) / ".relay" / "state.json"
        return run.StateStore(path, state)

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

    def test_attempt_counter_is_persisted_before_launch_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            for _ in range(3):
                run._consume_agent_call(store, "TASK-0001", "worker", "task", False, False)
            with self.assertRaises(RuntimeError):
                run._consume_agent_call(store, "TASK-0001", "worker", "task", False, False)
            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["attemptCounters"]["TASK-0001"], 3)

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
                    return {"mode": "repair", "assignmentId": assignment["id"], "status": "candidate", "candidateSha": "new", "changedPaths": ["src"], "validation": [], "summary": ""}
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
            pr = {"number": 1, "state": "OPEN", "url": "x"}
            store.state["taskStates"][assignment["id"]] = {"pushedSha": "sha", "pr": pr, "phase": "approved"}
            store.state["pullRequests"][assignment["id"]] = pr
            store.state["reviewSessions"][assignment["id"]] = {"phase": "approved", "repairAttemptsStarted": 0, "reviewCallsStarted": 3, "reviewCallLimit": 9}
            with patch("run.provider_call") as provider:
                self.assertIs(run.publish_candidate(store, assignment, Path(root), "branch", "sha"), pr)
                provider.assert_not_called()
            with patch("run.wait_for_checks", return_value="merged"), patch("run.provider_with_retries") as merge:
                self.assertTrue(run.merge_assignment(store, __import__("threading").Semaphore(1), assignment, Path(root), "branch", pr, "sha"))
                merge.assert_not_called()

    def test_agent_and_provider_timeouts_consume_prelaunch_counters(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.state_store(root)
            with patch("run.bounded_run", side_effect=subprocess.TimeoutExpired("codex", 1)), self.assertRaises(RuntimeError):
                run.invoke_agent(store, __import__("threading").Semaphore(1), Path(root), "TASK-0001", "worker", "prompt", mode="task")
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
                self.assertEqual(int((relay / "coordinator.lock").read_text()), os.getpid())
            self.assertFalse((relay / "coordinator.lock").exists())

    def test_complete_cleanup_preview_then_confirm(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root).resolve(); relay = root / ".relay"; relay.mkdir(); (root / ".git" / "info").mkdir(parents=True)
            campaign = "test"; marker = f"<!-- relay: campaign={campaign} repository={__import__('hashlib').sha256(str(root).encode()).hexdigest()[:12]} -->"
            (root / "tasks.md").write_text("# Tasks\n\n<!-- relay: planned-base=0123456 requirements=abc123 -->\n", encoding="utf-8")
            (root / "bugs.md").write_text(f"# Bugs\n\n{marker}\n", encoding="utf-8")
            (root / ".git" / "info" / "exclude").write_text("tasks.md\nbugs.md\n.relay/\nkeep.me\n", encoding="utf-8")
            state = {"repository": str(root), "phase": "complete", "activeProcesses": {}, "worktrees": {}, "pullRequests": {}, "campaignId": campaign}
            (relay / "state.json").write_text(json.dumps(state), encoding="utf-8")
            self.assertEqual(run.permanent_cleanup(root, False), 0)
            self.assertTrue((root / "tasks.md").exists())
            self.assertEqual(run.permanent_cleanup(root, True), 0)
            self.assertFalse(relay.exists())
            self.assertEqual((root / ".git" / "info" / "exclude").read_text(encoding="utf-8"), "keep.me\n")


class FakeEndToEndTests(unittest.TestCase):
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
            environment = os.environ | {
                "RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}",
                "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_EVENTS": str(events), "FAKE_GH_STATE": str(provider), "FAKE_AUDIT_SCOPE": "1", "FAKE_TWO_TASKS": "1",
            }
            planned = subprocess.run([sys.executable, str(Path(plan.__file__)), "--repo", str(target), "--requirements", str(requirements), "--workers", "2"], capture_output=True, text=True, env=environment, check=True)
            plan_text = planned.stdout
            completed = subprocess.run([sys.executable, str(Path(run.__file__)), "--repo", str(target), "--workers", "2", "--agent-timeout", "10", "--validation-timeout", "10", "--provider-timeout", "10", "--provider-check-timeout", "10"], input=plan_text, capture_output=True, text=True, env=environment, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            state = json.loads((target / ".relay" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "complete")
            self.assertTrue(state["auditPlanStarted"] and state["auditPlanCompleted"] and state["auditTriageCompleted"])
            self.assertEqual(len(state["auditScopes"]), 1)
            self.assertTrue(next(iter(state["auditScopes"].values()))["completed"])
            self.assertLessEqual(state["auditCallsStarted"], state["auditCallLimit"])
            self.assertEqual(state["worktrees"], {})
            self.assertTrue(all(value["phase"] == "integrated" for value in state["taskStates"].values()))
            self.assertTrue(all(session["initialReviewAssignmentsStarted"] == 2 and session["initialReviewAssignmentsCompleted"] == 2 and session["triageCompleted"] and session["reviewCallsStarted"] == 3 for session in state["reviewSessions"].values()))
            spans = {task: {action: float(Path(f"{events}.{task}.{action}").read_text()) for action in ("start", "end")} for task in ("TASK-0001", "TASK-0002")}
            self.assertLess(max(spans[task]["start"] for task in spans), min(spans[task]["end"] for task in spans))
            self.assertEqual(len(list(provider.glob("*.json"))), 2)
            self.assertTrue(all(state["providerAttemptCounters"][f"{task}:pr-create"] == 1 for task in ("TASK-0001", "TASK-0002")))
            self.assertIn("--repo', 'fake/relay", (target / ".relay" / "logs" / "provider.log").read_text(encoding="utf-8"))
            before = (target / ".relay" / "state.json").read_bytes()
            shown = subprocess.run([sys.executable, str(Path(status.__file__)), "--repo", str(target)], capture_output=True, text=True, check=True)
            self.assertIn("Review sessions", shown.stdout)
            self.assertEqual((target / ".relay" / "state.json").read_bytes(), before)

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
            environment = os.environ | {"RELAY_CODEX": f"{sys.executable} {fake_codex}", "RELAY_GH": f"{sys.executable} {fake_gh}", "RELAY_ALLOW_FAKE_PROVIDER": "1", "FAKE_GH_STATE": str(provider), "FAKE_ADVERSARIAL": "1"}
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
assignment = re.search(r"Assignment ID: ([A-Z]+-?\d*)", prompt)
assignment = assignment.group(1) if assignment else "AUDIT"
candidate = re.search(r"Candidate SHA: ([0-9a-f]+)", prompt)
candidate = candidate.group(1) if candidate else ""
if prompt.startswith("Role: Repository Scout"):
    scope = re.search(r"Inspect only this fixed scope: (.+)", prompt).group(1)
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
elif prompt.startswith("Role: Worker"):
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
    result = {"mode": mode, "assignmentId": assignment, "status": "candidate", "candidateSha": sha, "changedPaths": allowed, "validation": [], "summary": "done"}
elif "Role: contract-reviewer" in prompt or "Role: risk-reviewer" in prompt:
    role = "contract-reviewer" if "Role: contract-reviewer" in prompt else "risk-reviewer"
    findings = []
    if os.environ.get("FAKE_ADVERSARIAL"):
        findings = [{"id": role, "severity": "P1", "location": "file.txt:1", "failure": "always fails", "reproduction": "python -c \"raise SystemExit(1)\"", "requirement": "must pass", "evidence": "deterministic failure", "candidateIntroduced": True}]
    result = {"assignmentId": assignment, "candidateSha": candidate, "findings": findings}
elif "Role: triage-pm" in prompt:
    decisions = [{"findingId": role, "action": "accept-blocker", "reason": "reproduced"} for role in ("contract-reviewer", "risk-reviewer")] if os.environ.get("FAKE_ADVERSARIAL") and assignment != "AUDIT" else []
    result = {"assignmentId": assignment, "decisions": decisions}
elif "Role: verification-reviewer" in prompt:
    result = {"assignmentId": assignment, "candidateSha": candidate, "status": "unresolved" if os.environ.get("FAKE_ADVERSARIAL") else "resolved"}
elif "Role: audit-planner" in prompt:
    result = {"scopes": [{"scopeId": "AUDIT-0001", "scope": "README", "requirements": ["fixture"], "paths": ["README.md"], "commands": [], "completionCondition": "README inspected"}]} if os.environ.get("FAKE_AUDIT_SCOPE") else {"scopes": []}
elif "Role: audit-worker" in prompt:
    result = {"scopeId": assignment, "findings": []}
else:
    raise SystemExit("unknown prompt")
with open(out, "w", encoding="utf-8") as stream: json.dump(result, stream)
''')


FAKE_GH = textwrap.dedent(r'''
import json, os, pathlib, re, sys
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
    if any("statusCheckRollup" in arg for arg in args): record.update(mergeStateStatus="CLEAN", statusCheckRollup=[])
    print(json.dumps(record)); raise SystemExit(0)
if args[:2] == ["pr", "merge"]:
    key = args[2]
    for path in root.glob("*.json"):
        record = json.loads(path.read_text())
        if str(record["number"]) == key:
            record["state"] = "MERGED"; path.write_text(json.dumps(record)); raise SystemExit(0)
raise SystemExit(f"unknown gh args: {args}")
''')


if __name__ == "__main__":
    unittest.main()
