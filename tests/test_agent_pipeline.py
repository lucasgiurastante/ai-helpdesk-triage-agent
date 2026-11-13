"""End to end tests of the agent pipeline.

The suite defines its own local fakes (``FakeITSM``, ``FakeIdentity`` and
``FakeApps``) so that it runs without the ``adapters`` package. The test that
exercises the real connectors is skipped until that package is delivered.
"""

import json
import os
import shutil
import tempfile
import unittest

import agent
from services.common import repo_path

try:  # pragma: no cover - depends on the delivery of the other implementer
    from adapters import (  # noqa: F401
        build_app_adapter,
        build_identity_adapter,
        build_itsm_adapter,
    )

    ADAPTERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ADAPTERS_AVAILABLE = False


class FakeITSM:
    """In-memory ITSM fake following the adapter contract."""

    name = "mock"

    def __init__(self, tickets=None):
        self.tickets = {ticket["ticket_id"]: dict(ticket) for ticket in tickets or []}
        self.comments = []
        self.assignments = []
        self.priorities = []
        self.resolved = []
        self.write_calls = 0

    def write_enabled(self):
        """Writes are always allowed in the fake."""
        return True

    def get_ticket(self, ticket_id):
        """Return a ticket of the fake store."""
        if ticket_id not in self.tickets:
            raise KeyError(ticket_id)
        return dict(self.tickets[ticket_id])

    def list_tickets(self, limit=50, offset=0, query=None):
        """Return the tickets of the fake store."""
        return list(self.tickets.values())[offset : offset + limit]

    def add_comment(self, ticket_id, comment):
        """Record a comment."""
        self.comments.append((ticket_id, comment))
        return {"ticket_id": ticket_id, "status": "comment_added"}

    def assign(self, ticket_id, queue):
        """Record an assignment."""
        self.assignments.append((ticket_id, queue))
        return {"ticket_id": ticket_id, "status": "assigned", "queue": queue}

    def set_priority(self, ticket_id, priority):
        """Record a priority change."""
        self.priorities.append((ticket_id, priority))
        return {"ticket_id": ticket_id, "status": "priority_updated", "priority": priority}

    def resolve(self, ticket_id, resolution):
        """Record a resolution."""
        self.resolved.append((ticket_id, resolution))
        return {"ticket_id": ticket_id, "status": "resolved"}


class FakeIdentity:
    """Directory fake following the adapter contract."""

    name = "mock"

    def __init__(self, users=None):
        self.users = users or {}
        self.calls = []

    def get_user(self, username):
        """Return the directory record of a user."""
        self.calls.append(username)
        if username not in self.users:
            raise KeyError(username)
        return dict(self.users[username])

    def get_groups(self, username):
        """Return the groups of a user."""
        return list(self.get_user(username).get("groups") or [])

    def is_active(self, username):
        """Return whether the account is active."""
        return bool(self.get_user(username).get("active"))

    def get_manager(self, username):
        """Return the manager of a user."""
        return self.get_user(username).get("manager")

    def request_access(self, username, resource, justification=None):
        """Record an access request."""
        return {"status": "pending_approval", "user": username, "resource": resource}

    def reset_password(self, username, *, verified=False, verification_method=None):
        """Reset a password honouring the identity guardrails."""
        user = self.get_user(username)
        if not verified or not user.get("mfa_enrolled") or not user.get("active"):
            raise PermissionError("Password reset requires verified identity with MFA")
        return {"status": "success", "user": username}

    def unlock_account(self, username, *, verified=False):
        """Unlock an account honouring the identity guardrails."""
        user = self.get_user(username)
        if not verified:
            raise PermissionError("Account unlock requires a verified identity")
        if user.get("locked_by_admin"):
            raise PermissionError("locked by administrator; requires security approval")
        return {"status": "success", "user": username}


class FakeApps:
    """Application fake following the adapter contract."""

    name = "mock"
    service = "SAP Fiori"

    def __init__(self):
        self.actions = []

    def check_status(self, app):
        """Return the operational status."""
        return {"app": app, "status": "operational"}

    def execute_action(self, action):
        """Record the execution of an action."""
        self.actions.append(action)
        return {"action_id": "fake", "action": action.get("action"), "status": "simulated"}

    def list_resources(self):
        """Return no resource."""
        return []


def _users():
    """Return the directory of record."""
    with open(repo_path("data", "users.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _tickets():
    """Return the demo tickets in the ITSM shape."""
    return agent._load_tickets(repo_path("data", "tickets.csv"))


class PipelineTests(unittest.TestCase):
    """The full pipeline over the demo batch."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="helpdesk-pipeline-")
        self.tickets = _tickets()
        self.itsm = FakeITSM(self.tickets)
        self.identity = FakeIdentity(_users())
        self.apps = FakeApps()
        self.adapters = {
            "itsm": self.itsm,
            "identity": self.identity,
            "apps": self.apps,
        }

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def _run(self, **kwargs):
        """Run the pipeline against the temporary outputs directory."""
        return agent.run_pipeline(
            self.tickets, self.adapters, outputs_dir=self.directory, **kwargs
        )

    def test_every_artefact_is_written(self):
        summary = self._run()
        for name in (
            "triaged_tickets.json",
            "triaged_tickets.csv",
            "actions.json",
            "approvals.json",
            "audit.log",
            "metrics.json",
            "metrics.md",
        ):
            self.assertTrue(
                os.path.isfile(os.path.join(self.directory, name)), "missing %s" % name
            )
        self.assertEqual(summary["run_id"].startswith("RUN-"), True)

    def test_the_batch_covers_the_three_decisions(self):
        summary = self._run()
        decisions = {result["decision"] for result in summary["tickets"]}
        self.assertIn("auto_execute", decisions)
        self.assertIn("require_approval", decisions)
        self.assertIn("escalate", decisions)

    def test_no_high_or_critical_action_is_executed_without_approval(self):
        self._run()
        with open(os.path.join(self.directory, "actions.json"), "r", encoding="utf-8") as handle:
            actions = json.load(handle)
        self.assertTrue(actions)
        for action in actions:
            if action["risk"] in ("high", "critical"):
                self.assertNotEqual(
                    action["status"],
                    "succeeded",
                    "a high/critical action was executed without approval",
                )
                self.assertIsNotNone(action["approval_id"])

    def test_every_requiring_ticket_has_an_approval_record(self):
        summary = self._run()
        approvals = summary["approvals"]
        by_ticket = {record["ticket_id"]: record for record in approvals}
        for result in summary["tickets"]:
            if result["decision"] in ("require_approval", "escalate"):
                self.assertIn(result["ticket_id"], by_ticket)
                self.assertEqual(by_ticket[result["ticket_id"]]["status"], "pending")

    def test_the_audit_log_contains_at_least_ten_events_per_ticket(self):
        self._run()
        with open(os.path.join(self.directory, "audit.log"), "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        per_ticket = {}
        for record in records:
            if record.get("ticket_id"):
                per_ticket.setdefault(record["ticket_id"], []).append(record["event"])
        self.assertEqual(len(per_ticket), len(self.tickets))
        for ticket_id, events in per_ticket.items():
            self.assertGreaterEqual(
                len(events), 10, "%s only produced %s events" % (ticket_id, events)
            )
        self.assertIn("run_started", [record["event"] for record in records])
        self.assertIn("run_completed", [record["event"] for record in records])

    def test_dry_run_does_not_write_to_the_itsm(self):
        self._run(dry_run=True)
        self.assertEqual(self.itsm.comments, [])
        self.assertEqual(self.itsm.assignments, [])

    def test_write_back_comments_and_prioritises_the_tickets(self):
        self._run(dry_run=False)
        self.assertEqual(len(self.itsm.comments), len(self.tickets))
        self.assertEqual(len(self.itsm.priorities), len(self.tickets))
        self.assertEqual(len(self.itsm.assignments), len(self.tickets))

    def test_only_auto_executed_tickets_are_resolved(self):
        summary = self._run(dry_run=False)
        auto_executed = [
            result["ticket_id"]
            for result in summary["tickets"]
            if result["status"] == "auto_executed"
        ]
        resolved = [ticket_id for ticket_id, _ in self.itsm.resolved]
        self.assertEqual(sorted(resolved), sorted(auto_executed))

    def test_approve_all_simulates_the_human_decision(self):
        summary = self._run(approve_all=True)
        approvals = summary["approvals"]
        self.assertTrue(approvals)
        for approval in approvals:
            self.assertEqual(approval["status"], "executed")
            self.assertTrue(approval["decided_by"])
        with open(os.path.join(self.directory, "audit.log"), "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        simulated = [
            record
            for record in records
            if record["event"] == "approval_resolved" and record.get("simulated_approval")
        ]
        self.assertTrue(simulated)
        self.assertEqual(simulated[0]["actor"], "human")

    def test_an_unknown_requester_does_not_break_the_batch(self):
        broken = self.tickets + [
            {
                "ticket_id": "TCK-999",
                "title": "x",
                "description": "Mi cuenta bloqueada en SAP Fiori",
                "requester": "ghost.user",
                "service": "SAP Fiori",
                "created_at": "2026-11-11T15:00:00Z",
            }
        ]
        summary = agent.run_pipeline(
            broken, self.adapters, outputs_dir=self.directory
        )
        self.assertEqual(len(summary["tickets"]), len(broken))
        ghost = [
            result for result in summary["tickets"] if result["ticket_id"] == "TCK-999"
        ][0]
        # An unknown requester is never handled automatically: the ticket goes
        # to a human and an approval is created for it.
        self.assertNotEqual(ghost["decision"], "auto_execute")
        self.assertNotEqual(ghost["status"], "auto_executed")
        self.assertEqual(ghost["suggested_action"], "human_review")
        self.assertTrue(
            any(
                record["ticket_id"] == "TCK-999"
                for record in summary["approvals"]
            )
        )

    def test_the_run_is_idempotent(self):
        self._run()
        with open(os.path.join(self.directory, "actions.json"), "r", encoding="utf-8") as handle:
            first = json.load(handle)
        self._run()
        with open(os.path.join(self.directory, "actions.json"), "r", encoding="utf-8") as handle:
            second = json.load(handle)
        first_ids = {action["action_id"] for action in first}
        second_ids = {action["action_id"] for action in second}
        self.assertEqual(first_ids, second_ids)
        statuses = {action["status"] for action in second}
        self.assertIn("duplicate_skipped", statuses)

    def test_metrics_snapshot(self):
        summary = self._run()
        metrics = summary["metrics"]
        self.assertEqual(metrics["tickets_processed"], len(self.tickets))
        self.assertGreater(metrics["auto_resolved"], 0)
        self.assertEqual(metrics["cost_estimate_usd"], 0.0)
        self.assertEqual(metrics["run_id"], summary["run_id"])

    def test_tickets_with_inactive_requester_are_never_automated(self):
        summary = self._run()
        inactive = [
            result
            for result in summary["tickets"]
            if result["requester"] == "pedro.gomez"
        ]
        self.assertTrue(inactive)
        for result in inactive:
            self.assertEqual(result["decision"], "require_approval")
            self.assertNotEqual(result["status"], "auto_executed")


class CliTests(unittest.TestCase):
    """The command line interface."""

    def test_help_exits_cleanly(self):
        with self.assertRaises(SystemExit) as context:
            agent.main(["--help"])
        self.assertEqual(context.exception.code, 0)

    def test_missing_input_is_a_fatal_error(self):
        self.assertEqual(agent.main(["--input", "does/not/exist.csv"]), 1)

    def test_dry_run_pipeline(self):
        directory = tempfile.mkdtemp(prefix="helpdesk-cli-")
        try:
            code = agent.main(
                [
                    "--input",
                    repo_path("data", "tickets.csv"),
                    "--dry-run",
                    "--verbose",
                    "--outputs-dir",
                    directory,
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(os.path.isfile(os.path.join(directory, "metrics.md")))
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class FallbackAdapterTests(unittest.TestCase):
    """The in-process fallback used when the ``adapters`` package is absent.

    The fallback keeps the repository runnable in any delivery order, so it is
    covered by its own tests instead of being left as untested dead code.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="helpdesk-fallback-")
        self.tickets = _tickets()

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_the_fallback_emulates_the_adapter_contract(self):
        adapters = agent._fallback_adapters(self.tickets)
        user = adapters["identity"].get_user("juan.perez")
        self.assertTrue(user["active"])
        self.assertTrue(user["mfa_enrolled"])
        self.assertEqual(adapters["identity"].get_manager("juan.perez"), "laura.gomez")
        self.assertIn("SAP_BASIC", adapters["identity"].get_groups("juan.perez"))

        ticket = adapters["itsm"].get_ticket("TCK-001")
        self.assertEqual(ticket["requester"], "juan.perez")
        self.assertEqual(len(adapters["itsm"].list_tickets()), len(self.tickets))

        result = adapters["apps"].execute_action(
            {"action": "restart_service", "target": "juan.perez", "params": {}}
        )
        self.assertIn(result["status"], ("simulated", "succeeded"))

    def test_the_fallback_enforces_the_identity_guardrails(self):
        adapters = agent._fallback_adapters(self.tickets)
        identity = adapters["identity"]
        with self.assertRaises(agent._FallbackVerificationRequired):
            identity.reset_password("juan.perez", verified=False)
        with self.assertRaises(agent._FallbackVerificationRequired):
            identity.reset_password("sofia.torres", verified=True)
        with self.assertRaises(agent._FallbackNotFound):
            identity.get_user("ghost.user")
        with self.assertRaises(agent._FallbackVerificationRequired):
            identity.unlock_account("luis.martinez", verified=True)

    def test_the_pipeline_runs_with_the_fallback_adapters(self):
        adapters = agent._fallback_adapters(self.tickets)
        summary = agent.run_pipeline(
            self.tickets, adapters, outputs_dir=self.directory, dry_run=True
        )
        self.assertEqual(summary["metrics"]["tickets_processed"], len(self.tickets))
        self.assertGreater(summary["metrics"]["auto_resolved"], 0)
        self.assertTrue(
            os.path.isfile(os.path.join(self.directory, "audit.log"))
        )


@unittest.skipUnless(ADAPTERS_AVAILABLE, "the adapters package is not available yet")
class RealAdaptersTests(unittest.TestCase):
    """Integration test against the real connectors."""

    def test_pipeline_with_the_real_mock_adapters(self):
        directory = tempfile.mkdtemp(prefix="helpdesk-real-")
        try:
            adapters = agent.build_adapters("mock", "mock", "mock")
            summary = agent.run_pipeline(
                _tickets(), adapters, outputs_dir=directory, dry_run=True
            )
            self.assertEqual(summary["metrics"]["tickets_processed"], 12)
            self.assertGreater(summary["metrics"]["auto_resolved"], 0)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
