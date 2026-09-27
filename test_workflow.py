from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import plan
import repo
import run


class WorkflowTests(unittest.TestCase):
    def test_task_validation_accepts_minimal_task(self) -> None:
        self.assertEqual(
            plan.validate_tasks(
                {
                    "tasks": [
                        {
                            "id": "TASK-001",
                            "title": "Do work",
                            "description": "Implement work",
                            "acceptanceCriteria": ["It works"],
                            "validation": ["python -m unittest"],
                        }
                    ]
                }
            ),
            1,
        )

    def test_task_validation_rejects_duplicate_ids(self) -> None:
        task = {
            "id": "TASK-001",
            "title": "Do work",
            "description": "Implement work",
            "acceptanceCriteria": ["It works"],
            "validation": [],
        }
        with self.assertRaises(ValueError):
            plan.validate_tasks({"tasks": [task, dict(task)]})

    def test_bug_validation_requires_evidence(self) -> None:
        with self.assertRaises(ValueError):
            run.validate_bugs({"bugs": [{"id": "BUG-001"}]})

    def test_bug_validation_accepts_empty_campaign(self) -> None:
        self.assertEqual(run.validate_bugs({"bugs": []}), [])

    def test_branch_names_are_deterministic(self) -> None:
        self.assertEqual(run.task_branch("task", "TASK/001"), "relay/task/TASK-001")
        self.assertEqual(run.task_branch("bug", "BUG-001"), "relay/bug/BUG-001")

    def test_prompt_files_are_versioned(self) -> None:
        for name in ("planning.md", "task.md", "audit.md", "bug.md"):
            self.assertTrue((plan.ROOT / "prompts" / name).is_file())

    def test_generated_agents_are_project_focused(self) -> None:
        self.assertIn("plan.md", repo.TARGET_AGENTS)
        self.assertIn("tasks.json", repo.TARGET_AGENTS)
        self.assertNotIn(".relay", repo.TARGET_AGENTS)

    def test_no_worker_pool_argument(self) -> None:
        with self.assertRaises(SystemExit):
            run.parser().parse_args(["--repo", ".", "--workers", "4"])

    def test_parallelism_is_one_agent_per_item(self) -> None:
        async def check() -> None:
            status = run.Status(no_spinner=True, verbose=False)
            seen: list[str] = []

            async def fake_worker(repo_path, item, kind, base, current_status, merge_lock):
                seen.append(item["id"])
                await asyncio.sleep(0)
                return True

            items = [{"id": f"TASK-{index}"} for index in range(3)]
            with patch.object(run, "run_item", fake_worker):
                self.assertTrue(await run.run_items(Path("."), items, "task", status))
            self.assertEqual(sorted(seen), ["TASK-0", "TASK-1", "TASK-2"])

        asyncio.run(check())

    def test_status_snapshot_has_phase_and_counts(self) -> None:
        status = run.Status(no_spinner=True, verbose=False)
        status.phase = "build"
        status.active = 2
        status.complete = 1
        self.assertIn("phase=build", status.snapshot())
        self.assertIn("active=2", status.snapshot())
        self.assertIn("complete=1", status.snapshot())

    def test_plan_parser_requires_inputs(self) -> None:
        with self.assertRaises(SystemExit):
            plan.parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
