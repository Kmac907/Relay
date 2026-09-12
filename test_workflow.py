import argparse
import unittest
from pathlib import Path

import plan
import run


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

    def test_invalid_dependency_rejected(self):
        text = plan.render_tasks([self.task(dependencies=["TASK-9999"])], "0123456", "abc123")
        with self.assertRaises(ValueError):
            run.parse_tasks(text)

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

    def test_review_budget_formula(self):
        self.assertEqual(run.review_call_limit(2, 2), 9)

    def test_positive_deadlines_cannot_be_disabled(self):
        for make_parser in (plan.parser, run.parser):
            with self.assertRaises(SystemExit):
                make_parser().parse_args(["--agent-timeout", "0"])


if __name__ == "__main__":
    unittest.main()
