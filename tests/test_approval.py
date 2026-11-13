"""Behavioural tests of the approval queue.

The most important test of the suite is
:meth:`ApprovalTests.test_high_risk_action_is_never_executed_without_a_human_decision`:
it encodes the invariant that a ``high`` or ``critical`` action is never
executed without an explicit and still valid human approval.
"""

import os
import shutil
import tempfile
import unittest

from services.approval import APPROVAL_STATUSES, ApprovalQueue, apply_approved_action
from services.common import load_json, repo_path


def _policy() -> dict:
    """Return the real risk matrix."""
    return load_json(repo_path("policies", "risk_matrix.json"))


def _request(**overrides):
    """Build an approval request payload."""
    request = {
        "ticket_id": "TCK-001",
        "requester": "juan.perez",
        "action": "grant_role",
        "resource": "sap-fiori-basic",
        "params": {"ticket_id": "TCK-001"},
        "risk": "high",
        "decision": "require_approval",
        "approver_role": "manager",
        "confidence": 0.77,
        "reasons": ["El riesgo efectivo high exige aprobación del rol manager"],
        "run_id": "RUN-TEST",
        "target": "juan.perez",
    }
    request.update(overrides)
    return request


class ApprovalQueueTests(unittest.TestCase):
    """Lifecycle of the approval records."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="helpdesk-approvals-")
        self.path = os.path.join(self.directory, "approvals.json")
        self.queue = ApprovalQueue(self.path, _policy())

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_create_never_executes_anything(self):
        record = self.queue.create(_request())
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["approval_id"], "APR-TCK-001-%s" % record["approval_id"][-8:])
        self.assertIsNone(record["decided_by"])
        self.assertTrue(record["expires_at"] > record["created_at"])

    def test_approval_id_is_deterministic(self):
        first = self.queue.create(_request())
        second = self.queue.create(_request(target="otro.usuario"))
        self.assertEqual(first["approval_id"], second["approval_id"])

    def test_create_is_idempotent_for_the_same_request(self):
        first = self.queue.create(_request())
        second = self.queue.create(_request())
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(len(self.queue.load()), 1)

    def test_a_new_request_supersedes_the_previous_one(self):
        first = self.queue.create(_request())
        second = self.queue.create(_request(resource="otro-recurso"))
        self.assertEqual(self.queue.get(first["approval_id"])["status"], "superseded")
        self.assertEqual(
            self.queue.get(first["approval_id"])["superseded_by"],
            second["approval_id"],
        )

    def test_approve_requires_the_matching_role(self):
        record = self.queue.create(_request())
        outcome = self.queue.approve(record["approval_id"], "support_lead", "ok")
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["error"], "approver_role_mismatch")
        self.assertEqual(outcome["expected_role"], "manager")

    def test_approve_requires_an_approver(self):
        record = self.queue.create(_request())
        outcome = self.queue.approve(record["approval_id"], "", "ok")
        self.assertEqual(outcome["error"], "approver_required")

    def test_approve_with_the_right_role(self):
        record = self.queue.create(_request())
        outcome = self.queue.approve(record["approval_id"], "manager:laura.gomez", "ok")
        self.assertEqual(outcome["status"], "approved")
        stored = self.queue.get(record["approval_id"])
        self.assertEqual(stored["status"], "approved")
        self.assertEqual(stored["decided_by"], "laura.gomez")

    def test_reject_requires_a_note(self):
        record = self.queue.create(_request())
        outcome = self.queue.reject(record["approval_id"], "manager", "")
        self.assertEqual(outcome["error"], "decision_note_required")

    def test_reject_with_a_note(self):
        record = self.queue.create(_request())
        outcome = self.queue.reject(record["approval_id"], "manager", "no corresponde")
        self.assertEqual(outcome["status"], "rejected")
        self.assertEqual(self.queue.get(record["approval_id"])["status"], "rejected")

    def test_expire_due_only_expires_pending_requests(self):
        record = self.queue.create(_request())
        expired = self.queue.expire_due(now="2999-01-01T00:00:00Z")
        self.assertEqual(len(expired), 1)
        self.assertEqual(self.queue.get(record["approval_id"])["status"], "expired")
        self.assertEqual(self.queue.expire_due(now="2999-01-01T00:00:00Z"), [])

    def test_pending_filters_by_ticket(self):
        self.queue.create(_request())
        self.queue.create(_request(ticket_id="TCK-002", target="otro"))
        self.assertEqual(len(self.queue.pending()), 2)
        self.assertEqual(len(self.queue.pending("TCK-001")), 1)

    def test_stats(self):
        self.queue.create(_request())
        stats = self.queue.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["pending"], 1)

    def test_queue_is_persisted(self):
        self.queue.create(_request())
        reloaded = ApprovalQueue(self.path, _policy())
        self.assertEqual(len(reloaded.load()), 1)


class ApplyApprovedActionTests(unittest.TestCase):
    """The execution gate of SPEC section 3.5."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="helpdesk-apply-")
        self.path = os.path.join(self.directory, "approvals.json")
        self.queue = ApprovalQueue(self.path, _policy())
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def _executor(self, approval):
        self.calls.append(approval.get("approval_id"))
        return {"status": "succeeded"}

    def test_pending_approval_is_not_executed(self):
        record = self.queue.create(_request())
        outcome = apply_approved_action(record, self._executor)
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "approval_required")
        self.assertEqual(self.calls, [])

    def test_rejected_approval_is_not_executed(self):
        record = self.queue.create(_request())
        self.queue.reject(record["approval_id"], "manager", "no")
        outcome = apply_approved_action(self.queue.get(record["approval_id"]), self._executor)
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "rejected")
        self.assertEqual(self.calls, [])

    def test_expired_approval_is_not_executed(self):
        record = self.queue.create(_request())
        self.queue.expire_due(now="2999-01-01T00:00:00Z")
        outcome = apply_approved_action(self.queue.get(record["approval_id"]), self._executor)
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "expired")
        self.assertEqual(self.calls, [])

    def test_high_risk_action_is_never_executed_without_a_human_decision(self):
        """Invariant: high/critical actions need an explicit human decision."""
        record = self.queue.create(_request(risk="high"))
        record["status"] = "approved"  # tampered record without a human decision
        outcome = apply_approved_action(record, self._executor)
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "no_human_decision")
        self.assertEqual(self.calls, [])

    def test_critical_action_is_never_executed_without_a_human_decision(self):
        record = self.queue.create(_request(risk="critical", action="disable_user"))
        record["status"] = "approved"
        outcome = apply_approved_action(record, self._executor)
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "no_human_decision")
        self.assertEqual(self.calls, [])

    def test_approved_action_is_executed(self):
        record = self.queue.create(_request(risk="high"))
        self.queue.approve(record["approval_id"], "manager:laura.gomez", "ok")
        outcome = apply_approved_action(self.queue.get(record["approval_id"]), self._executor)
        self.assertTrue(outcome["executed"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.queue.get(record["approval_id"])["status"], "executed")

    def test_failing_executor_does_not_raise(self):
        record = self.queue.create(_request(risk="low"))
        self.queue.approve(record["approval_id"], "manager:laura.gomez", "ok")

        def boom(approval):
            raise RuntimeError("backend down")

        outcome = apply_approved_action(self.queue.get(record["approval_id"]), boom)
        self.assertFalse(outcome["executed"])
        self.assertEqual(outcome["reason"], "execution_error")


class StatusTests(unittest.TestCase):
    """The approval status enumeration."""

    def test_statuses(self):
        self.assertEqual(
            set(APPROVAL_STATUSES),
            {"pending", "approved", "rejected", "expired", "superseded", "executed", "failed"},
        )


if __name__ == "__main__":
    unittest.main()
