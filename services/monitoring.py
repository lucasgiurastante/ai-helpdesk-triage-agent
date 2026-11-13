"""Observability layer: structured audit log and aggregated metrics.

Every relevant step of the pipeline emits one JSONL line into
``outputs/audit.log`` and feeds the counters behind ``outputs/metrics.md``.
Both artifacts are the evidence an operator (or an interviewer) uses to
reconstruct why the agent made a decision (SPEC section 3.6).
"""

import json
import logging
from typing import Any, Dict, List, Optional

from services.common import AUDIT_EVENTS, ensure_directory, save_json, utc_now_iso

LOGGER = logging.getLogger(__name__)

#: Snapshot keys written to ``outputs/metrics.json`` -- SPEC section 3.6.
METRIC_KEYS: List[str] = [
    "tickets_processed",
    "auto_resolved",
    "requires_approval",
    "escalated",
    "unknown_category",
    "human_overrides",
    "approvals_approved",
    "approvals_rejected",
    "approvals_expired",
    "approvals_superseded",
    "average_confidence",
    "average_confidence_all",
    "average_triage_latency_ms",
    "average_resolution_latency_ms",
    "mttr_minutes",
    "deflection_rate",
    "fcr_rate",
    "estimated_minutes_saved",
    "action_failure_count",
    "integration_error_count",
    "escalation_reasons",
    "category_distribution",
    "priority_distribution",
    "cost_estimate_usd",
    "run_id",
]

#: Statuses that mean "the action reached the end user without human help".
#: ``duplicate_skipped`` counts as well: the effect of the action is already in
#: place because a previous run applied it (idempotency), so the ticket was
#: still resolved by the agent and not by a human.
_SUCCESS_STATUSES = ("succeeded", "simulated", "duplicate_skipped")

#: Statuses that mean "the action failed".
_FAILURE_STATUSES = ("failed", "error")


class AuditLogger:
    """Append-only JSONL audit logger.

    Args:
        path: Destination file (usually ``outputs/audit.log``).
        run_id: Identifier of the run, stamped on every record.
    """

    def __init__(self, path: str, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        ensure_directory(path)
        self._handle = open(path, "a", encoding="utf-8")
        self.events: List[Dict[str, Any]] = []

    def log(self, event: str, ticket_id: Optional[str] = None, **fields: Any) -> dict:
        """Write one audit record.

        Args:
            event: Event name; it should belong to :data:`AUDIT_EVENTS`.
            ticket_id: Ticket the event belongs to (may be ``None``).
            **fields: Additional structured fields (category, priority,
                confidence, risk, decision, reasons, rule_ids, latency_ms...).

        Returns:
            The record that was written.
        """
        if event not in AUDIT_EVENTS:
            LOGGER.warning("Audit event %r is not part of the fixed enumeration", event)
        record: Dict[str, Any] = {
            "ts": utc_now_iso(),
            "run_id": self.run_id,
            "event": event,
            "ticket_id": ticket_id,
            "actor": fields.pop("actor", "agent"),
        }
        record.update(fields)
        self.events.append(record)
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        return record

    def close(self) -> None:
        """Flush and close the underlying file handle."""
        if not self._handle.closed:
            self._handle.flush()
            self._handle.close()

    def __enter__(self) -> "AuditLogger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


class MetricsCollector:
    """Aggregate the run metrics.

    The collector keeps a small per-ticket record so that the derived
    metrics (deflection rate, MTTR proxy, estimated minutes saved) can be
    computed without keeping the whole pipeline in memory.
    """

    def __init__(self) -> None:
        self.run_id: str = ""
        self._tickets: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._approvals: Dict[str, int] = {
            "approved": 0,
            "rejected": 0,
            "expired": 0,
            "superseded": 0,
        }
        self._escalations: Dict[str, int] = {}
        self._errors: List[Dict[str, Any]] = []
        self._action_failures: int = 0

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #

    def _ticket_state(self, ticket_id: str) -> Dict[str, Any]:
        """Return (and create when needed) the per-ticket state."""
        if ticket_id not in self._tickets:
            self._tickets[ticket_id] = {
                "category": None,
                "priority": None,
                "confidence": 0.0,
                "triage_latency_ms": None,
                "resolution_latency_ms": None,
                "risk": None,
                "decision": None,
                "auto_ok": False,
                "estimated_minutes": None,
            }
            self._order.append(ticket_id)
        return self._tickets[ticket_id]

    def record_triage(
        self,
        ticket_id: str,
        category: str,
        priority: str,
        confidence: float,
        latency_ms: float,
        risk: str,
        decision: str,
    ) -> None:
        """Record the triage outcome of a ticket.

        Args:
            ticket_id: Ticket identifier.
            category: Detected category.
            priority: Final priority.
            confidence: Classification confidence.
            latency_ms: Time spent on the triage step.
            risk: Effective risk.
            decision: Policy decision.
        """
        state = self._ticket_state(ticket_id)
        state["category"] = category
        state["priority"] = priority
        state["confidence"] = float(confidence or 0.0)
        state["triage_latency_ms"] = float(latency_ms or 0.0)
        state["risk"] = risk
        state["decision"] = decision

    def record_action(
        self,
        ticket_id: str,
        action: str,
        status: str,
        latency_ms: float,
        estimated_minutes: float,
        risk: str,
    ) -> None:
        """Record the execution of an action.

        Args:
            ticket_id: Ticket identifier.
            action: Canonical action name.
            status: Execution status (``succeeded``, ``simulated``, ``failed``...).
            latency_ms: Time spent executing the action.
            estimated_minutes: Manual effort the action saves.
            risk: Risk of the action.
        """
        state = self._ticket_state(ticket_id)
        state["resolution_latency_ms"] = float(latency_ms or 0.0)
        state["estimated_minutes"] = estimated_minutes
        state["action_status"] = status
        if status in _FAILURE_STATUSES:
            self._action_failures += 1
        if state.get("decision") == "auto_execute" and status in _SUCCESS_STATUSES:
            state["auto_ok"] = True

    def record_approval(self, ticket_id: str, outcome: str, role: str) -> None:
        """Record the outcome of an approval.

        Args:
            ticket_id: Ticket identifier.
            outcome: ``approved``, ``rejected``, ``expired`` or ``superseded``.
            role: Role that owned the decision.
        """
        key = outcome if outcome in self._approvals else "approved"
        self._approvals[key] = self._approvals.get(key, 0) + 1

    def record_escalation(self, ticket_id: str, queue: str, reason: str) -> None:
        """Record an escalation.

        Args:
            ticket_id: Ticket identifier.
            queue: Target queue.
            reason: Why the ticket was escalated.
        """
        self._escalations[queue] = self._escalations.get(queue, 0) + 1

    def record_error(self, ticket_id: str, subsystem: str, message: str) -> None:
        """Record an integration error.

        Args:
            ticket_id: Ticket identifier (may be ``None``).
            subsystem: Component that failed.
            message: Error message.
        """
        self._errors.append(
            {"ticket_id": ticket_id, "subsystem": subsystem, "message": message}
        )

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    @staticmethod
    def _mean(values: List[float]) -> float:
        """Return the arithmetic mean of ``values`` (``0.0`` when empty)."""
        if not values:
            return 0.0
        return round(sum(values) / len(values), 4)

    def snapshot(self) -> Dict[str, Any]:
        """Return the aggregated metrics of the run.

        Returns:
            A dictionary with exactly the keys of :data:`METRIC_KEYS`.
        """
        tickets = [self._tickets[ticket_id] for ticket_id in self._order]
        tickets_processed = len(tickets)
        auto_resolved = sum(1 for state in tickets if state.get("auto_ok"))
        requires_approval = sum(
            1 for state in tickets if state.get("decision") == "require_approval"
        )
        escalated = sum(
            1 for state in tickets if state.get("decision") == "escalate"
        )
        unknown_category = sum(
            1 for state in tickets if state.get("category") == "unknown"
        )
        known = [state for state in tickets if state.get("category") != "unknown"]
        confidences = [float(state.get("confidence") or 0.0) for state in tickets]
        known_confidences = [
            float(state.get("confidence") or 0.0) for state in known
        ]
        triage_latencies = [
            float(state.get("triage_latency_ms") or 0.0)
            for state in tickets
            if state.get("triage_latency_ms") is not None
        ]
        resolution_latencies = [
            float(state.get("resolution_latency_ms") or 0.0)
            for state in tickets
            if state.get("resolution_latency_ms") is not None
        ]
        auto_states = [state for state in tickets if state.get("auto_ok")]
        mttr_values = [
            float(state.get("estimated_minutes") or 0.0) for state in auto_states
        ]
        minutes_saved = sum(
            max(0.0, float(state.get("estimated_minutes") or 0.0) - 1.0)
            for state in auto_states
        )

        category_distribution: Dict[str, int] = {}
        priority_distribution: Dict[str, int] = {}
        for state in tickets:
            category = str(state.get("category") or "unknown")
            priority = str(state.get("priority") or "unknown")
            category_distribution[category] = category_distribution.get(category, 0) + 1
            priority_distribution[priority] = priority_distribution.get(priority, 0) + 1

        deflection_rate = (
            round(auto_resolved / tickets_processed, 4) if tickets_processed else 0.0
        )
        return {
            "tickets_processed": tickets_processed,
            "auto_resolved": auto_resolved,
            "requires_approval": requires_approval,
            "escalated": escalated,
            "unknown_category": unknown_category,
            "human_overrides": self._approvals.get("rejected", 0),
            "approvals_approved": self._approvals.get("approved", 0),
            "approvals_rejected": self._approvals.get("rejected", 0),
            "approvals_expired": self._approvals.get("expired", 0),
            "approvals_superseded": self._approvals.get("superseded", 0),
            "average_confidence": self._mean(known_confidences),
            "average_confidence_all": self._mean(confidences),
            "average_triage_latency_ms": self._mean(triage_latencies),
            "average_resolution_latency_ms": self._mean(resolution_latencies),
            "mttr_minutes": self._mean(mttr_values),
            "deflection_rate": deflection_rate,
            "fcr_rate": deflection_rate,
            "estimated_minutes_saved": round(minutes_saved, 2),
            "action_failure_count": self._action_failures,
            "integration_error_count": len(self._errors),
            "escalation_reasons": dict(self._escalations),
            "category_distribution": category_distribution,
            "priority_distribution": priority_distribution,
            "cost_estimate_usd": 0.0,
            "run_id": self.run_id,
        }

    # ------------------------------------------------------------------ #
    # Writers
    # ------------------------------------------------------------------ #

    def write_json(self, path: str) -> None:
        """Write the snapshot to ``path`` as JSON."""
        save_json(path, self.snapshot())

    def write_markdown(self, path: str) -> None:
        """Write a human readable report to ``path``.

        Args:
            path: Destination file (usually ``outputs/metrics.md``).
        """
        snapshot = self.snapshot()
        lines: List[str] = []
        lines.append("# Help Desk Agent -- Run Metrics")
        lines.append("")
        lines.append(f"- **run_id**: `{snapshot['run_id']}`")
        lines.append(
            "- **Generated**: every timestamp is ISO-8601 UTC "
            "(see `outputs/audit.log` for the per-ticket trail)"
        )
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        for key in METRIC_KEYS:
            value = snapshot.get(key)
            if isinstance(value, dict):
                rendered = ", ".join(
                    f"{name}={count}" for name, count in sorted(value.items())
                ) or "-"
            elif isinstance(value, float):
                rendered = f"{value:.4f}"
            else:
                rendered = str(value)
            lines.append(f"| {key} | {rendered} |")
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        lines.append(
            "- `cost_estimate_usd` is `0.0`: the classification is deterministic, "
            "no LLM is called."
        )
        lines.append(
            "- `fcr_rate` equals `deflection_rate` in this pipeline: a ticket is "
            "either solved by the agent or handed over to a human."
        )
        lines.append(
            "- `mttr_minutes` is a proxy: the mean of the KB `estimated_minutes` "
            "of the automatically resolved tickets."
        )
        lines.append(
            "- `estimated_minutes_saved` subtracts one minute of agent handling "
            "from every auto-resolved ticket."
        )
        ensure_directory(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


__all__ = ["AUDIT_EVENTS", "AuditLogger", "METRIC_KEYS", "MetricsCollector"]
