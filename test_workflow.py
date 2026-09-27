"""Exercise the real pipeline with real Git and shell checks, fake agents/GitHub."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import plan
import run


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.git(self.root, "init", "--bare", "--initial-branch=main", "remote.git")
        self.git(self.repo, "init", "--initial-branch=main")
        self.configure(self.repo)
        (self.repo / "AGENTS.md").write_text("Keep changes focused.\n", encoding="utf-8")
        self.git(self.repo, "add", "AGENTS.md")
        self.git(self.repo, "commit", "-m", "initial")
        self.git(self.repo, "remote", "add", "origin", str(self.root / "remote.git"))
        self.git(self.repo, "push", "origin", "main")
        self.git(self.root, "clone", str(self.root / "remote.git"), "github")
        self.github = self.root / "github"
        self.configure(self.github)
        (self.repo / "plan.md").write_text("Implement two independent files, then repair the defect.", encoding="utf-8")
        (self.repo / "requirements.md").write_text("Create two files.", encoding="utf-8")
        self.data = {"tasks": [
            {"id": name, "title": name, "description": "Create " + name,
             "validation": [f'python -c "from pathlib import Path; assert Path(\'{name}.txt\').is_file()"']}
            for name in ("ONE", "TWO")
        ]}
        self.calls = []
        self.prs = {}
        self.pr_lock = threading.Lock()
        self.barrier = threading.Barrier(2, timeout=15)
        self.fail = ""

    def tearDown(self):
        self.temp.cleanup()

    def git(self, cwd, *args):
        return plan.execute(cwd, "git", *args)

    def configure(self, cwd):
        self.git(cwd, "config", "user.name", "Relay test")
        self.git(cwd, "config", "user.email", "relay@example.invalid")
        self.git(cwd, "config", "commit.gpgsign", "false")

    def fake_agent(self, cwd, role, context, output, **kwargs):
        self.calls.append(role)
        if role == "merge":
            if self.fail != "merge":
                (cwd / "shared.txt").write_text("integration and task\n", encoding="utf-8")
                self.git(cwd, "add", "shared.txt")
            output.write_text("Resolved conflict" if self.fail != "merge" else "Need user input", encoding="utf-8")
            return output.read_text(encoding="utf-8")
        if role == "audit":
            # Audit must see both actual merged task changes.
            self.assertTrue((cwd / "ONE.txt").exists())
            self.assertTrue((cwd / "TWO.txt").exists())
            if self.fail == "audit":
                return "invalid json"
            return json.dumps({"bugs": [{"id": "FIX", "description": "Fix ONE", "evidence": "ONE contains a defect",
                "validation": ['python -c "from pathlib import Path; assert Path(\'ONE.txt\').read_text() == \'fixed\'"']}]})
        if role == "task":
            self.barrier.wait()  # Serial execution cannot pass this test.
            name = context["item"]["id"]
            if self.fail != "task" or name != "ONE":
                (cwd / f"{name}.txt").write_text("defect", encoding="utf-8")
        elif self.fail != "bug":
            (cwd / "ONE.txt").write_text("fixed", encoding="utf-8")
        output.write_text("Implemented the assigned work.", encoding="utf-8")
        return output.read_text(encoding="utf-8")

    def fake_execute(self, cwd, *args, **kwargs):
        if args[0] != "gh":
            return plan.execute(cwd, *args, **kwargs)
        if args[1] == "auth":
            return ""
        action = args[2]
        if action == "list":
            head, base = args[args.index("--head") + 1], args[args.index("--base") + 1]
            return json.dumps([{"url": url, "state": pr["state"],
                                "headRefOid": pr.get("headRefOid") or self.git(self.github, "ls-remote", "origin", "refs/heads/" + head).split()[0],
                                "mergeCommit": pr.get("mergeCommit")}
                               for url, pr in self.prs.items() if pr["head"] == head and pr["base"] == base])
        if action == "create":
            with self.pr_lock:
                url = f"https://example.invalid/pull/{len(self.prs) + 1}"
                self.prs[url] = {"head": args[args.index("--head") + 1],
                                 "base": args[args.index("--base") + 1], "state": "OPEN"}
            return url
        pr = self.prs[args[3]]
        if action == "view":
            if args[-1] == "statusCheckRollup":
                return json.dumps({"statusCheckRollup": []})
            if args[-1] == "state":
                return json.dumps({"state": pr["state"]})
            return json.dumps({"mergeable": pr.get("mergeable", "MERGEABLE"),
                               "baseRefName": pr["base"], "headRefName": pr["head"],
                               "headRefOid": self.git(self.github, "ls-remote", "origin", "refs/heads/" + pr["head"]).split()[0]})
        if action == "merge":
            self.git(self.github, "fetch", "origin")
            self.assertEqual(self.git(self.github, "rev-parse", "origin/" + pr["head"]), args[-1])
            pr["headRefOid"] = args[-1]
            self.git(self.github, "checkout", "--detach", "origin/" + pr["base"])
            try:
                self.git(self.github, "merge", "--no-ff", "--no-edit", "origin/" + pr["head"])
            except RuntimeError:
                pr["mergeable"] = "CONFLICTING"
                self.git(self.github, "merge", "--abort")
                raise
            self.git(self.github, "push", "origin", "HEAD:refs/heads/" + pr["base"])
            pr["mergeCommit"] = {"oid": self.git(self.github, "rev-parse", "HEAD")}
            pr["state"] = "MERGED"
            return ""
        self.failTest(f"Unexpected gh operation: {args}")

    def invoke(self):
        with patch.object(run, "agent", self.fake_agent), patch.object(run, "execute", self.fake_execute):
            return run.pipeline(self.repo, self.data, self.logs)

    def test_requirements_file_is_authoritative(self):
        self.data["requirements"] = "Stale embedded requirements must be ignored"
        (self.repo / "requirements.md").write_text("Existing project requirements", encoding="utf-8")
        with patch.object(run, "execute", return_value=""), patch.object(run, "parallel", return_value=False) as build:
            self.assertFalse(run.pipeline(self.repo, self.data, self.logs))
        self.assertEqual(build.call_args.args[-1]["requirements"], "Existing project requirements")

    def conflicting_pr(self):
        (self.github / "shared.txt").write_text("base\n", encoding="utf-8")
        self.git(self.github, "add", "shared.txt")
        self.git(self.github, "commit", "-m", "shared base")
        self.git(self.github, "push", "origin", "main")
        self.git(self.github, "checkout", "-b", "relay/task/ONE", "main")
        (self.github / "shared.txt").write_text("task\n", encoding="utf-8")
        (self.github / "ONE.txt").write_text("defect", encoding="utf-8")
        self.git(self.github, "add", "shared.txt", "ONE.txt")
        self.git(self.github, "commit", "-m", "task")
        self.git(self.github, "push", "origin", "relay/task/ONE")
        self.git(self.github, "checkout", "-b", run.INTEGRATION, "main")
        (self.github / "shared.txt").write_text("integration\n", encoding="utf-8")
        self.git(self.github, "add", "shared.txt")
        self.git(self.github, "commit", "-m", "sibling change")
        self.git(self.github, "push", "origin", run.INTEGRATION)
        self.git(self.repo, "fetch", "origin")
        self.prs["https://example.invalid/pull/1"] = {"head": "relay/task/ONE", "base": run.INTEGRATION, "state": "OPEN"}
        return self.git(self.repo, "rev-parse", "origin/" + run.INTEGRATION)

    def test_agent_resolves_real_git_conflict_before_merge(self):
        base = self.conflicting_pr()
        item = self.data["tasks"][0]
        item["validation"].append('python -c "from pathlib import Path; assert Path(\'shared.txt\').read_text().strip() == \'integration and task\'"')
        summary = run.Summary()
        with patch.object(run, "agent", self.fake_agent), patch.object(run, "execute", self.fake_execute):
            run.worker(self.repo, item, "task", base, self.logs, {}, threading.Lock(), summary)
        self.assertEqual(self.calls, ["merge"])
        self.assertEqual(self.prs["https://example.invalid/pull/1"]["state"], "MERGED")
        self.assertIn("merged=1/1", summary())
        self.git(self.repo, "fetch", "origin")
        self.assertEqual(self.git(self.repo, "show", "origin/relay/integration:shared.txt"), "integration and task")

    def test_unresolved_conflict_does_not_relaunch_agent(self):
        base = self.conflicting_pr()
        self.fail = "merge"
        with patch.object(run, "agent", self.fake_agent), patch.object(run, "execute", self.fake_execute):
            with self.assertRaisesRegex(RuntimeError, "unresolved conflicts"):
                run.worker(self.repo, self.data["tasks"][0], "task", base, self.logs, {}, threading.Lock())
        self.assertEqual(self.calls, ["merge"])
        self.assertEqual(self.prs["https://example.invalid/pull/1"]["state"], "OPEN")
        self.assertTrue((self.logs / "task-ONE-merge").exists())

    def test_summary_tracks_worker_stages(self):
        summary = run.Summary()
        summary.begin("bug fixes", ["BUG-001", "BUG-002"])
        summary.set("BUG-001", "resolving", "PR #5")
        summary.set("BUG-002", "merged")
        self.assertIn("BUG FIXES", summary())
        self.assertIn("conflicts=1", summary())
        self.assertIn("merged=1/2", summary())
        summary.set("BUG-001", "blocked", "PR #5 unresolved conflict")
        self.assertIn("conflicts=0", summary())
        self.assertIn("blocked=1", summary())
        self.assertIn("PR #5 unresolved conflict", summary())
        summary.retained = 2
        self.assertIn("PROJECT MERGED | validation=passed | cleanup=partial | retained=2", summary())
        summary.retained = 0
        self.assertIn("cleanup=complete | retained=0", summary())

    def test_worktree_preserves_git_line_endings_and_cleans_bytecode(self):
        self.git(self.repo, "config", "core.autocrlf", "true")
        path = self.logs / "final"
        run.worktree(self.repo, path, "HEAD")
        self.assertIn(b"\r\n", (path / "AGENTS.md").read_bytes())
        # Reproduce the older copy's LF rewrite, plus real Python-generated cache.
        (path / "AGENTS.md").write_bytes(b"Keep changes focused.\n")
        (path / "example.py").write_text("value = 1\n", encoding="utf-8")
        self.git(path, "add", "example.py")
        self.git(path, "commit", "-m", "example")
        plan.execute(path, "python", "-m", "py_compile", "example.py")
        self.assertTrue(list((path / "__pycache__").glob("*.pyc")))
        run.cleanup_worktree(self.repo, path)
        self.assertFalse(path.exists())

    def test_cleanup_preserves_actual_edits_and_untracked_source(self):
        path = self.logs / "audit"
        run.worktree(self.repo, path, "HEAD")
        (path / "AGENTS.md").write_text("Actual changed instructions\n", encoding="utf-8")
        (path / "notes.txt").write_text("Keep this work", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            run.cleanup_worktree(self.repo, path)
        self.assertEqual((path / "AGENTS.md").read_text(encoding="utf-8"), "Actual changed instructions\n")
        self.assertEqual((path / "notes.txt").read_text(encoding="utf-8"), "Keep this work")

    def test_complete_pipeline_parallel_work_and_one_audit(self):
        self.assertTrue(self.invoke())
        self.assertEqual(self.calls.count("task"), 2)
        self.assertEqual(self.calls.count("audit"), 1)
        self.assertEqual(self.calls.count("bug"), 1)
        self.assertEqual(len(self.prs), 4)
        self.git(self.repo, "fetch", "origin", "main")
        self.assertEqual(self.git(self.repo, "show", "origin/main:ONE.txt"), "fixed")
        self.assertFalse((self.logs / "task-ONE").exists())
        self.assertFalse((self.logs / "final").exists())
        # Re-running a completed project must not regenerate BUG IDs or spend agents.
        saved = (self.repo / "bugs.json").read_bytes()
        self.calls.clear()
        summary = run.Summary()
        with patch.object(run, "agent", side_effect=AssertionError("completed project launched an agent")), \
                patch.object(run, "execute", self.fake_execute):
            self.assertTrue(run.pipeline(self.repo, self.data, self.logs, summary))
        self.assertEqual((self.repo / "bugs.json").read_bytes(), saved)
        self.assertEqual(len(self.prs), 4)
        self.assertIn("ALREADY MERGED; NOT REVALIDATED", summary())
        # An old merged project PR must not certify newer integration commits.
        self.git(self.github, "checkout", "--detach", "origin/relay/integration")
        self.git(self.github, "commit", "--allow-empty", "-m", "new work")
        self.git(self.github, "push", "origin", "HEAD:refs/heads/relay/integration")
        with self.assertRaisesRegex(RuntimeError, "integration changed"):
            self.invoke()
        self.assertEqual(self.calls, [])

    def test_existing_integration_and_prs_continue_without_task_agents(self):
        self.git(self.repo, "push", "origin", "main:refs/heads/relay/integration")
        for name in ("ONE", "TWO"):
            self.git(self.github, "checkout", "-b", f"relay/task/{name}", "origin/main")
            (self.github / f"{name}.txt").write_text("defect", encoding="utf-8")
            self.git(self.github, "add", f"{name}.txt")
            self.git(self.github, "commit", "-m", name)
            self.git(self.github, "push", "origin", f"relay/task/{name}")
            self.prs[f"https://example.invalid/pull/{len(self.prs) + 1}"] = {
                "head": f"relay/task/{name}", "base": run.INTEGRATION, "state": "OPEN"}
        # One already merged task and one open PR cover both continuation paths.
        sha = self.git(self.github, "rev-parse", "origin/relay/task/ONE")
        self.fake_execute(self.repo, "gh", "pr", "merge", "https://example.invalid/pull/1",
                          "--merge", "--match-head-commit", sha)
        self.assertTrue(self.invoke())
        self.assertNotIn("task", self.calls)
        self.assertEqual(self.calls.count("audit"), 1)
        self.assertEqual(len(self.prs), 4)

    def test_failed_task_does_not_restart_or_audit(self):
        self.fail = "task"
        self.assertFalse(self.invoke())
        self.assertEqual(self.calls, ["task", "task"])
        self.assertTrue((self.logs / "task-ONE").exists())

    def test_failed_bug_does_not_restart_or_create_project_pr(self):
        self.fail = "bug"
        self.assertFalse(self.invoke())
        self.assertEqual(self.calls.count("audit"), 1)
        self.assertEqual(self.calls.count("bug"), 1)
        self.assertFalse(any(pr["base"] == "main" for pr in self.prs.values()))
        self.assertTrue((self.logs / "bug-FIX").exists())

    def test_final_failure_does_not_reaudit_or_repair(self):
        self.data["tasks"][0]["validation"].append('python -c "from pathlib import Path; assert Path.cwd().name != \'final\'"')
        with self.assertRaisesRegex(RuntimeError, "exited"):
            self.invoke()
        self.assertEqual(self.calls.count("audit"), 1)
        self.assertEqual(self.calls.count("bug"), 1)
        self.assertFalse(any(pr["base"] == "main" for pr in self.prs.values()))
        saved = (self.repo / "bugs.json").read_bytes()
        self.calls.clear()
        self.logs = self.root / "resumed"
        self.logs.mkdir()
        with self.assertRaisesRegex(RuntimeError, "exited"):
            self.invoke()
        self.assertEqual(self.calls, [])
        self.assertEqual((self.repo / "bugs.json").read_bytes(), saved)

    def test_resume_reuses_open_project_pr_and_saved_audit(self):
        original = self.fake_execute

        def fail_project_merge(cwd, *args, **kwargs):
            if args[:3] == ("gh", "pr", "merge") and self.prs[args[3]]["base"] == "main":
                raise RuntimeError("project merge interrupted")
            return original(cwd, *args, **kwargs)

        with patch.object(run, "agent", self.fake_agent), patch.object(run, "execute", fail_project_merge):
            with self.assertRaisesRegex(RuntimeError, "project merge interrupted"):
                run.pipeline(self.repo, self.data, self.logs)
        self.calls.clear()
        self.logs = self.root / "resumed"
        self.logs.mkdir()
        self.assertTrue(self.invoke())
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.prs), 4)
        self.assertTrue(all(pr["state"] == "MERGED" for pr in self.prs.values()))

    def test_invalid_audit_is_terminal(self):
        self.fail = "audit"
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.calls.count("audit"), 1)
        self.assertNotIn("bug", self.calls)

    def test_planning_external_requirements_and_malformed_output(self):
        requirements = self.root / "external.md"
        requirements.write_text("External requirements", encoding="utf-8")
        args = ["--repo", str(self.repo), "--requirements", str(requirements), "--no-spinner"]
        response = {"plan": "# Plan", "tasks": self.data["tasks"]}
        response["tasks"][0]["validation"] = {"command": "python --version"}
        with patch.object(plan, "agent", return_value=json.dumps(response)) as fake:
            self.assertEqual(plan.main(args), 0)
            self.assertEqual(fake.call_count, 1)
        saved = (self.repo / "tasks.json").read_text(encoding="utf-8")
        self.assertEqual(set(json.loads(saved)), {"tasks"})
        self.assertEqual((self.repo / "requirements.md").read_text(encoding="utf-8"), "External requirements")
        self.assertEqual(fake.call_args.args[2]["requirements"], "External requirements")
        with patch.object(plan, "agent", return_value="malformed") as fake:
            self.assertEqual(plan.main(args), 2)
            self.assertEqual(fake.call_count, 1)
        self.assertEqual((self.repo / "tasks.json").read_text(encoding="utf-8"), saved)

    def test_planning_defaults_to_project_requirements(self):
        response = {"plan": "# Plan", "tasks": self.data["tasks"]}
        with patch.object(plan, "agent", return_value=json.dumps(response)) as fake:
            self.assertEqual(plan.main(["--repo", str(self.repo), "--no-spinner"]), 0)
        self.assertEqual(fake.call_args.args[2]["requirements"], "Create two files.")
        self.assertEqual(set(json.loads((self.repo / "tasks.json").read_text(encoding="utf-8"))), {"tasks"})


if __name__ == "__main__":
    unittest.main()
