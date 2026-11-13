"""Behavioural tests of the monitoring layer: audit trail and metrics."""

import json
import os
import shutil
import tempfile
import unittest

from services.common import AUDIT_EVENTS
from services.monitoring import METRIC_KEYS, AuditLogger, MetricsCollector


class AuditLoggerTests(unittest.TestCase):
    """The JSONL audit trail."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="helpdesk-audit-")
        self.path = os.path.join(self.directory, "audit.log")
        self.logger = AuditLogger(self.path, "RUN-TEST")

    def tearDown(self):
        self.logger.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _lines(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_event_is_written_as_jsonl(self):
        self.logger.log("ticket_received", "TCK-001", requester="juan.perez")
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        record = lines[0]
        self.assertEqual(record["event"], "ticket_received")
        self.assertEqual(record["ticket_id"], "TCK-001")
        self.assertEqual(record["run_id"], "RUN-TEST")
        self.assertEqual(record["actor"], "agent")
        self.assertTrue(record["ts"].endswith("Z"))
        self.assertEqual(record["requester"], "juan.perez")

    def test_actor_can_be_overridden(self):
        self.logger.log("approval_resolved", "TCK-001", actor="human")
        self.assertEqual(self._lines()[0]["actor"], "human")

    def test_unknown_event_is_still_recorded(self):
        self.logger.log("not_a_real_event", "TCK-001")
        self.assertEqual(self._lines()[0]["event"], "not_a_real_event")

    def test_the_event_enumeration_is_fixed(self):
        self.assertEqual(len(AUDIT_EVENTS), 14)
        self.assertIn("run_started", AUDIT_EVENTS)
        self.assertIn("run_completed", AUDIT_EVENTS)
        self.assertIn("itsm_writeback", AUDIT_EVENTS)


class MetricsCollectorTests(unittest.TestCase):
    """The aggregated metrics."""

    def setUp(self):
        self.metrics = MetricsCollector()
        self.metrics.run_id = "RUN-TEST"

    def test_snapshot_contains_every_contract_key(self):
        snapshot = self.metrics.snapshot()
        for key in METRIC_KEYS:
            self.assertIn(key, snapshot)

    def test_counters(self):
        self.metrics.record_triage(
            "TCK-001", "account_locked", "P3", 0.80, 12.0, "low", "auto_execute"
        )
        self.metrics.record_action(
            "TCK-001", "password_reset_link", "succeeded", 30.0, 12.0, "low"
        )
        self.metrics.record_triage(
            "TCK-002", "vpn", "P2", 0.50, 8.0, "medium", "require_approval"
        )
        self.metrics.record_approval("TCK-002", "rejected", "support_lead")
        self.metrics.record_escalation("TCK-003", "security", "category security")
        self.metrics.record_error("TCK-003", "itsm", "timeout")
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["tickets_processed"], 2)
        self.assertEqual(snapshot["auto_resolved"], 1)
        self.assertEqual(snapshot["requires_approval"], 1)
        self.assertEqual(snapshot["human_overrides"], 1)
        self.assertEqual(snapshot["approvals_rejected"], 1)
        self.assertEqual(snapshot["integration_error_count"], 1)
        self.assertEqual(snapshot["escalation_reasons"], {"security": 1})
        self.assertEqual(snapshot["category_distribution"]["account_locked"], 1)
        self.assertEqual(snapshot["priority_distribution"]["P3"], 1)
        self.assertEqual(snapshot["cost_estimate_usd"], 0.0)
        self.assertEqual(snapshot["run_id"], "RUN-TEST")

    def test_deflection_and_fcr_are_the_same_ratio(self):
        self.metrics.record_triage("TCK-001", "email", "P3", 0.9, 5.0, "low", "auto_execute")
        self.metrics.record_action("TCK-001", "send_kb_article", "simulated", 5.0, 10.0, "low")
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["deflection_rate"], 1.0)
        self.assertEqual(snapshot["fcr_rate"], snapshot["deflection_rate"])
        self.assertEqual(snapshot["estimated_minutes_saved"], 9.0)
        self.assertEqual(snapshot["mttr_minutes"], 10.0)

    def test_average_confidence_ignores_unknown_categories(self):
        self.metrics.record_triage("TCK-001", "email", "P3", 0.8, 5.0, "low", "auto_execute")
        self.metrics.record_triage("TCK-002", "unknown", "P3", 0.2, 5.0, "medium", "require_approval")
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["average_confidence"], 0.8)
        self.assertEqual(snapshot["average_confidence_all"], 0.5)

    def test_failed_actions_are_counted(self):
        self.metrics.record_triage("TCK-001", "email", "P3", 0.9, 5.0, "low", "auto_execute")
        self.metrics.record_action("TCK-001", "send_kb_article", "failed", 5.0, 10.0, "low")
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["action_failure_count"], 1)
        self.assertEqual(snapshot["auto_resolved"], 0)

    def test_writers(self):
        directory = tempfile.mkdtemp(prefix="helpdesk-metrics-")
        try:
            markdown = os.path.join(directory, "metrics.md")
            payload = os.path.join(directory, "metrics.json")
            self.metrics.record_triage("TCK-001", "email", "P3", 0.9, 5.0, "low", "auto_execute")
            self.metrics.write_markdown(markdown)
            self.metrics.write_json(payload)
            with open(markdown, "r", encoding="utf-8") as handle:
                content = handle.read()
            for key in METRIC_KEYS:
                self.assertIn(key, content)
            with open(payload, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual(data["tickets_processed"], 1)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
