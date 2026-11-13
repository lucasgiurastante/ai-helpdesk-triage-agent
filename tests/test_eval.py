"""Tests of the evaluation harness and of its metric helpers.

The metric tests use a small embedded result set so that the suite stays
fast and does not depend on the 27 ticket labelled set; the labelled set
itself is only checked for its shape (SPEC sections 4.9, 4.10 and 5).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.metrics import accuracy, check_thresholds, confusion_matrix, summarize  # noqa: E402
from eval.run_eval import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    load_labeled,
    render_markdown,
    run_evaluation,
    write_reports,
)

#: Small embedded labelled set: one perfect ticket, one wrong ticket.
EMBEDDED_LABELED: List[Dict[str, Any]] = [
    {
        "ticket_id": "T-001",
        "description": "La cuenta bloqueada no deja entrar a SAP Fiori",
        "requester": "juan.perez",
        "service": "SAP Fiori",
        "expected": {
            "category": "account_locked",
            "intent": "trouble",
            "priority": "P3",
            "risk": "low",
            "action": "password_reset_link",
            "decision": "auto_execute",
        },
        "user_profile": {"active": True, "mfa_enrolled": True, "vip": False},
    },
    {
        "ticket_id": "T-002",
        "description": "Recibí un phishing con un virus",
        "requester": "maria.gomez",
        "service": "Security",
        "expected": {
            "category": "security",
            "intent": "trouble",
            "priority": "P1",
            "risk": "critical",
            "action": "escalate_security_team",
            "decision": "escalate",
        },
        "user_profile": {"active": True, "mfa_enrolled": True, "vip": False},
    },
]


class FakeIdentity:
    """Identity fake that always answers with a static user."""

    def get_user(self, username: str) -> Dict[str, Any]:
        """Return a static user."""
        return {
            "username": username,
            "email": "{0}@acme.com".format(username),
            "display_name": username,
            "department": "IT",
            "active": True,
            "locked": False,
            "locked_by_admin": False,
            "groups": [],
            "manager": None,
            "vip": False,
            "mfa_enrolled": True,
            "identity_verified": True,
            "password_last_set_days": 1,
            "source": "fake",
            "raw": {},
        }


class FakeTriage:
    """Triage fake with a fixed answer per ticket."""

    def __init__(self, answers: Dict[str, Dict[str, Any]]) -> None:
        self.answers = answers

    def classify(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        """Return the canned triage result of the ticket."""
        return dict(self.answers.get(ticket["ticket_id"], {}))


class FakePolicy:
    """Policy fake with a fixed answer per ticket."""

    def __init__(self, answers: Dict[str, Dict[str, Any]]) -> None:
        self.answers = answers

    def evaluate(self, triage: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
        """Return the canned decision of the ticket."""
        return dict(self.answers.get(triage.get("ticket_id", ""), {}))


class TestMetrics(unittest.TestCase):
    """Tests of the pure metric helpers."""

    def test_accuracy_with_tuples_and_dicts(self) -> None:
        self.assertEqual(accuracy([("a", "a"), ("b", "c")]), 0.5)
        self.assertEqual(accuracy([{"expected": "a", "actual": "a"}]), 1.0)
        self.assertEqual(accuracy([]), 0.0)
        self.assertEqual(accuracy([("a", "a")]), 1.0)

    def test_confusion_matrix_counts_and_shape(self) -> None:
        matrix = confusion_matrix(["a", "b", "a"], ["a", "a", "b"])
        self.assertEqual(matrix["a"]["a"], 1)
        self.assertEqual(matrix["a"]["b"], 1)
        self.assertEqual(matrix["b"]["a"], 1)
        self.assertEqual(matrix["b"]["b"], 0)
        self.assertEqual(sorted(matrix), ["a", "b"])

    def test_summarize_computes_the_contract_metrics(self) -> None:
        results = [
            {
                "ticket_id": "T-001",
                "expected": {
                    "category": "vpn",
                    "intent": "trouble",
                    "priority": "P2",
                    "action": "restart_service",
                    "decision": "require_approval",
                    "risk": "medium",
                },
                "actual": {
                    "category": "vpn",
                    "intent": "trouble",
                    "priority": "P2",
                    "action": "restart_service",
                    "decision": "require_approval",
                    "risk": "medium",
                    "confidence": 0.8,
                },
            },
            {
                "ticket_id": "T-002",
                "expected": {
                    "category": "security",
                    "intent": "trouble",
                    "priority": "P1",
                    "action": "escalate_security_team",
                    "decision": "escalate",
                    "risk": "critical",
                },
                "actual": {
                    "category": "unknown",
                    "intent": "trouble",
                    "priority": "P1",
                    "action": "human_review",
                    "decision": "require_approval",
                    "risk": "medium",
                    "confidence": 0.2,
                },
            },
        ]
        metrics = summarize(results)
        self.assertEqual(metrics["total"], 2)
        self.assertEqual(metrics["category_accuracy"], 0.5)
        self.assertEqual(metrics["priority_accuracy"], 1.0)
        self.assertEqual(metrics["action_accuracy"], 0.5)
        self.assertEqual(metrics["decision_accuracy"], 0.5)
        self.assertEqual(metrics["escalation_recall"], 0.0)
        self.assertEqual(metrics["auto_resolution_rate"], 0.0)
        self.assertEqual(metrics["escalation_rate"], 0.0)
        self.assertEqual(metrics["approval_rate"], 1.0)
        self.assertAlmostEqual(metrics["avg_confidence"], 0.5, places=4)
        self.assertEqual(metrics["failure_count"], 1)
        self.assertEqual(metrics["failures"][0]["ticket_id"], "T-002")
        self.assertIn("vpn", metrics["breakdown_by_category"])
        self.assertIn("medium", metrics["breakdown_by_risk"])

    def test_check_thresholds_reports_the_breaches(self) -> None:
        breaches = check_thresholds(
            {"category_accuracy": 0.5, "priority_accuracy": 1.0},
            {"category_accuracy": 0.85, "priority_accuracy": 0.8},
        )
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0]["metric"], "category_accuracy")
        self.assertEqual(check_thresholds({"category_accuracy": 0.9}, {"category_accuracy": 0.85}), [])


class TestLabelledSet(unittest.TestCase):
    """Tests of the labelled evaluation set shipped with the repository."""

    def test_labelled_set_has_the_expected_shape(self) -> None:
        tickets = load_labeled()
        self.assertEqual(len(tickets), 28)
        identifiers = [ticket["ticket_id"] for ticket in tickets]
        self.assertEqual(len(set(identifiers)), 28)
        for ticket in tickets:
            self.assertIn("description", ticket)
            self.assertIn("requester", ticket)
            self.assertIn("service", ticket)
            expected = ticket["expected"]
            for key in ("category", "priority", "risk", "action", "decision"):
                self.assertIn(key, expected)
            self.assertIn(
                expected["category"],
                (
                    "access",
                    "account_locked",
                    "password",
                    "network",
                    "vpn",
                    "software",
                    "hardware",
                    "email",
                    "security",
                    "unknown",
                ),
            )
            self.assertIn(expected["decision"], ("auto_execute", "require_approval", "escalate"))
            self.assertIn(expected["risk"], ("low", "medium", "high", "critical"))
            self.assertIn(expected["priority"], ("P1", "P2", "P3", "P4"))

    def test_labelled_set_covers_every_category(self) -> None:
        tickets = load_labeled()
        categories: Dict[str, int] = {}
        for ticket in tickets:
            category = ticket["expected"]["category"]
            categories[category] = categories.get(category, 0) + 1
        for category in (
            "access",
            "account_locked",
            "password",
            "network",
            "vpn",
            "software",
            "hardware",
            "email",
            "security",
            "unknown",
        ):
            self.assertIn(category, categories)
        self.assertGreaterEqual(categories["unknown"], 1)

    def test_labelled_set_has_both_request_and_trouble_access(self) -> None:
        tickets = load_labeled()
        intents = [
            ticket["expected"].get("intent")
            for ticket in tickets
            if ticket["expected"]["category"] == "access"
        ]
        self.assertIn("request", intents)
        self.assertIn("trouble", intents)


class TestHarness(unittest.TestCase):
    """Tests of the harness wiring (with fake triage and policy services)."""

    def _run(self) -> Dict[str, Any]:
        triage = FakeTriage(
            {
                "T-001": {
                    "ticket_id": "T-001",
                    "category": "account_locked",
                    "intent": "trouble",
                    "priority": "P3",
                    "impact": "individual",
                    "urgency": "normal",
                    "confidence": 0.81,
                    "kb_id": "KB001",
                    "suggested_action": "password_reset_link",
                },
                "T-002": {
                    "ticket_id": "T-002",
                    "category": "security",
                    "intent": "trouble",
                    "priority": "P1",
                    "impact": "individual",
                    "urgency": "critical",
                    "confidence": 0.73,
                    "kb_id": "KB011",
                    "suggested_action": "escalate_security_team",
                },
            }
        )
        policy = FakePolicy(
            {
                "T-001": {
                    "decision": "auto_execute",
                    "risk": "low",
                    "effective_action": "password_reset_link",
                    "approver_role": None,
                    "reasons": ["low risk, high confidence"],
                    "rule_ids": [],
                },
                "T-002": {
                    "decision": "escalate",
                    "risk": "critical",
                    "effective_action": "escalate_security_team",
                    "approver_role": "security",
                    "reasons": ["security category"],
                    "rule_ids": ["ESC-002"],
                },
            }
        )
        return run_evaluation(
            EMBEDDED_LABELED,
            adapters={"identity": FakeIdentity()},
            triage_service=triage,
            policy_engine=policy,
        )

    def test_run_evaluation_produces_the_expected_metrics(self) -> None:
        report = self._run()
        metrics = report["metrics"]
        self.assertEqual(metrics["total"], 2)
        self.assertEqual(metrics["category_accuracy"], 1.0)
        self.assertEqual(metrics["priority_accuracy"], 1.0)
        self.assertEqual(metrics["action_accuracy"], 1.0)
        self.assertEqual(metrics["decision_accuracy"], 1.0)
        self.assertEqual(metrics["auto_resolution_rate"], 0.5)
        self.assertEqual(metrics["escalation_rate"], 0.5)
        self.assertEqual(metrics["escalation_recall"], 1.0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["breaches"], [])

    def test_run_evaluation_detects_a_mismatch(self) -> None:
        report = self._run()
        report["results"][1]["actual"]["category"] = "unknown"
        metrics = summarize(report["results"])
        self.assertEqual(metrics["category_accuracy"], 0.5)
        self.assertEqual(metrics["failure_count"], 1)

    def test_identity_source_is_reported(self) -> None:
        report = self._run()
        self.assertEqual(report["results"][0]["identity_source"], "directory+scenario")

    def test_reports_are_written_to_disk(self) -> None:
        report = self._run()
        with tempfile.TemporaryDirectory() as folder:
            md_path = Path(folder) / "eval_report.md"
            json_path = Path(folder) / "eval_report.json"
            write_reports(report, str(md_path), str(json_path))
            self.assertTrue(md_path.is_file())
            self.assertTrue(json_path.is_file())
            markdown = md_path.read_text(encoding="utf-8")
            self.assertIn("# AI Help Desk Agent -- Evaluation Report", markdown)
            self.assertIn("## Category confusion matrix", markdown)
            self.assertIn("## Priority confusion matrix", markdown)
            self.assertIn("## Failures", markdown)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["metrics"]["total"], 2)

    def test_default_thresholds_match_the_spec(self) -> None:
        self.assertEqual(DEFAULT_THRESHOLDS["category_accuracy"], 0.85)
        self.assertEqual(DEFAULT_THRESHOLDS["priority_accuracy"], 0.80)
        self.assertEqual(DEFAULT_THRESHOLDS["action_accuracy"], 0.75)
        self.assertEqual(DEFAULT_THRESHOLDS["decision_accuracy"], 0.80)

    def test_render_markdown_is_serialisable(self) -> None:
        report = self._run()
        markdown = render_markdown(report)
        self.assertIsInstance(markdown, str)
        self.assertIn("Gate:", markdown)


if __name__ == "__main__":  # pragma: no cover - script entry point
    unittest.main()
