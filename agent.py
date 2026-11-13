#!/usr/bin/env python3
"""AI Help Desk Triage & Remediation Agent -- end to end orchestrator.

The orchestrator wires the services together and exposes the CLI:

    python3 agent.py --demo
    python3 agent.py --input data/tickets.csv --dry-run --verbose

Every ticket goes through the same ten auditable steps (SPEC section 3.7):
receive, triage, identity check, policy decision, action proposal, approval or
execution, ITSM write-back and the run summary.

Safety model
------------
* the run is a dry run unless ``--write-back`` is given;
* even with ``--write-back`` nothing is written to a real ITSM unless
  ``HELPDESK_ALLOW_WRITES=1`` (the adapters enforce this too);
* an action with ``high`` or ``critical`` risk is never executed without an
  explicit and still valid human approval.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.common import (  # noqa: E402  (path bootstrap must run first)
    ApprovalRequest,
    load_json,
    read_csv,
    repo_path,
    save_json,
    short_hash,
    utc_now_iso,
    write_csv,
)
from services.approval import ApprovalQueue, apply_approved_action  # noqa: E402
from services.monitoring import AuditLogger, MetricsCollector  # noqa: E402
from services.policy import PolicyEngine  # noqa: E402
from services.triage import TriageService  # noqa: E402

LOGGER = logging.getLogger("agent")

#: Mapping between the canonical actions and the effect they produce.
#: ``none`` means "no side effect", ``unsupported`` means the action cannot be
#: executed by the current adapter contract (it is always escalated before).
ACTION_EXECUTORS: Dict[str, str] = {
    "send_kb_article": "itsm_comment",
    "password_reset_link": "identity_reset_password",
    "unlock_account": "identity_unlock",
    "grant_role": "identity_request_access",
    "grant_folder_access": "identity_request_access",
    "request_access_approval": "identity_request_access",
    "assign_software_package": "app_action",
    "restart_service": "app_action",
    "escalate_network_team": "itsm_assign",
    "escalate_security_team": "itsm_assign",
    "disable_user": "unsupported",
    "human_review": "none",
    "no_action": "none",
}

#: Queue used for the tickets that are waiting for a human decision.
DEFAULT_QUEUE = "itsm-l2"

#: Columns of ``outputs/triaged_tickets.csv``.
TRIAGED_CSV_COLUMNS: List[str] = [
    "ticket_id",
    "requester",
    "service",
    "category",
    "intent",
    "priority",
    "impact",
    "urgency",
    "confidence",
    "risk",
    "decision",
    "action",
    "status",
    "kb_id",
    "approval_id",
]


# --------------------------------------------------------------------------- #
# Fallback adapters
# --------------------------------------------------------------------------- #
# The `adapters` package is owned by the other implementer. Until it lands the
# CLI must still be runnable, so this module ships a minimal, clearly announced
# in-process implementation of the very same contract (SPEC section 3.2.1).
# It reads the very same data files and never touches the network.

class _FallbackError(Exception):
    """Base error of the fallback adapters."""


class _FallbackNotFound(_FallbackError):
    """Raised when a ticket or a user does not exist."""


class _FallbackVerificationRequired(_FallbackError):
    """Raised when an identity mutation lacks a verified identity."""


def _writes_enabled() -> bool:
    """Return ``True`` only when the write kill-switch is explicitly enabled."""
    return os.environ.get("HELPDESK_ALLOW_WRITES") == "1"


class _FallbackITSM:
    """In-memory ITSM used when the real ``adapters`` package is missing."""

    name = "mock"

    def __init__(self, tickets: Optional[List[dict]] = None) -> None:
        self._tickets: Dict[str, dict] = {}
        for ticket in tickets or []:
            ticket_id = ticket.get("ticket_id")
            if ticket_id:
                self._tickets[ticket_id] = dict(ticket)
        self._comments: List[dict] = []
        self._priority: Dict[str, str] = {}
        self._queue: Dict[str, str] = {}
        self._resolved: Dict[str, str] = {}

    def write_enabled(self) -> bool:
        """Return ``True`` when writes to the ITSM are allowed."""
        return _writes_enabled()

    def get_ticket(self, ticket_id: str) -> dict:
        """Return a ticket or raise :class:`_FallbackNotFound`."""
        if ticket_id not in self._tickets:
            raise _FallbackNotFound("Unknown ticket %r" % ticket_id)
        return dict(self._tickets[ticket_id])

    def list_tickets(
        self, limit: int = 50, offset: int = 0, query: Optional[str] = None
    ) -> List[dict]:
        """Return the tickets of the in-memory store."""
        tickets = [dict(ticket) for ticket in self._tickets.values()]
        if query:
            tickets = [
                ticket
                for ticket in tickets
                if query.lower() in (ticket.get("description") or "").lower()
            ]
        return tickets[offset : offset + limit]

    def add_comment(self, ticket_id: str, comment: str) -> dict:
        """Attach a comment to a ticket."""
        if not self.write_enabled():
            return {
                "ticket_id": ticket_id,
                "status": "skipped",
                "reason": "writes_disabled",
                "comment": comment,
                "timestamp": utc_now_iso(),
            }
        self._comments.append({"ticket_id": ticket_id, "comment": comment})
        return {
            "ticket_id": ticket_id,
            "status": "comment_added",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "comment": comment,
        }

    def assign(self, ticket_id: str, queue: str) -> dict:
        """Assign a ticket to a queue."""
        if not self.write_enabled():
            return {
                "ticket_id": ticket_id,
                "status": "skipped",
                "reason": "writes_disabled",
                "queue": queue,
                "timestamp": utc_now_iso(),
            }
        self._queue[ticket_id] = queue
        return {
            "ticket_id": ticket_id,
            "status": "assigned",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "queue": queue,
        }

    def set_priority(self, ticket_id: str, priority: str) -> dict:
        """Update the priority of a ticket."""
        if not self.write_enabled():
            return {
                "ticket_id": ticket_id,
                "status": "skipped",
                "reason": "writes_disabled",
                "priority": priority,
                "timestamp": utc_now_iso(),
            }
        self._priority[ticket_id] = priority
        return {
            "ticket_id": ticket_id,
            "status": "priority_updated",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "priority": priority,
        }

    def resolve(self, ticket_id: str, resolution: str) -> dict:
        """Resolve a ticket."""
        if not self.write_enabled():
            return {
                "ticket_id": ticket_id,
                "status": "skipped",
                "reason": "writes_disabled",
                "resolution": resolution,
                "timestamp": utc_now_iso(),
            }
        self._resolved[ticket_id] = resolution
        return {
            "ticket_id": ticket_id,
            "status": "resolved",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "resolution": resolution,
        }


class _FallbackIdentity:
    """In-memory directory used when the real ``adapters`` package is missing."""

    name = "mock"

    def __init__(self, users: Dict[str, dict], groups: Dict[str, dict]) -> None:
        self._users = users
        self._groups = groups

    def get_user(self, username: str) -> dict:
        """Return the directory record of a user."""
        if username not in self._users:
            raise _FallbackNotFound("Unknown user %r" % username)
        record = dict(self._users[username])
        record["source"] = "mock"
        record["raw"] = {}
        return record

    def get_groups(self, username: str) -> List[str]:
        """Return the groups of a user."""
        self.get_user(username)
        return list(self._users[username].get("groups") or [])

    def is_active(self, username: str) -> bool:
        """Return whether the account is active."""
        return bool(self.get_user(username).get("active"))

    def get_manager(self, username: str) -> Optional[str]:
        """Return the manager of a user."""
        return self.get_user(username).get("manager")

    def request_access(
        self, username: str, resource: str, justification: Optional[str] = None
    ) -> dict:
        """Register an access request."""
        self.get_user(username)
        return {
            "status": "pending_approval",
            "action_id": short_hash("%s|request_access|%s" % (username, resource)),
            "user": username,
            "resource": resource,
            "timestamp": utc_now_iso(),
            "justification": justification,
        }

    def reset_password(
        self,
        username: str,
        *,
        verified: bool = False,
        verification_method: Optional[str] = None,
    ) -> dict:
        """Reset a password, enforcing the identity verification guardrails."""
        user = self.get_user(username)
        if not verified or not user.get("mfa_enrolled") or not user.get("active"):
            raise _FallbackVerificationRequired(
                "Password reset requires verified identity with MFA"
            )
        return {
            "status": "success",
            "action_id": short_hash("%s|reset_password|" % username),
            "user": username,
            "timestamp": utc_now_iso(),
            "verification_method": verification_method,
        }

    def unlock_account(self, username: str, *, verified: bool = False) -> dict:
        """Unlock an account, enforcing the identity verification guardrails."""
        user = self.get_user(username)
        if not verified:
            raise _FallbackVerificationRequired(
                "Account unlock requires a verified identity"
            )
        if user.get("locked_by_admin"):
            raise _FallbackVerificationRequired(
                "locked by administrator; requires security approval"
            )
        return {
            "status": "success",
            "action_id": short_hash("%s|unlock_account|" % username),
            "user": username,
            "timestamp": utc_now_iso(),
        }


class _FallbackApps:
    """In-memory application connector used without the ``adapters`` package."""

    name = "mock"

    def __init__(self, service: str = "SAP Fiori") -> None:
        self.service = service

    def check_status(self, app: str) -> dict:
        """Return the operational status of an application."""
        return {
            "app": app,
            "status": "operational",
            "checked_at": utc_now_iso(),
            "details": {},
            "incident_ref": None,
        }

    def execute_action(self, action: dict) -> dict:
        """Simulate the execution of an application action."""
        action_name = action.get("action")
        status = "simulated" if not _writes_enabled() else "succeeded"
        return {
            "action_id": short_hash(
                "%s|%s|%s" % (action.get("target"), action_name, action.get("resource"))
            ),
            "action": action_name,
            "status": status,
            "detail": "executed by the in-process fallback connector",
            "executed_at": utc_now_iso(),
            "dry_run": not _writes_enabled(),
        }

    def list_resources(self) -> List[dict]:
        """Return the resources exposed by the connector."""
        return []


def _fallback_adapters(tickets: Optional[List[dict]] = None) -> dict:
    """Build the in-process fallback adapters."""
    users = load_json(repo_path("data", "users.json"))
    groups = load_json(repo_path("data", "groups.json"))
    return {
        "itsm": _FallbackITSM(tickets),
        "identity": _FallbackIdentity(users, groups),
        "apps": _FallbackApps(),
    }


# --------------------------------------------------------------------------- #
# Adapter factory
# --------------------------------------------------------------------------- #

def build_adapters(
    itsm_kind: Optional[str] = None,
    identity_kind: Optional[str] = None,
    app_kind: Optional[str] = None,
    tickets: Optional[List[dict]] = None,
) -> dict:
    """Build the three connectors of the pipeline.

    The import of the ``adapters`` package is lazy and local to this function
    so that ``import agent`` never fails when the package is not installed
    yet. When the package is missing a clearly announced in-process fallback
    is used instead (see :func:`_fallback_adapters`).

    Args:
        itsm_kind: ``mock`` | ``servicenow`` | ``jira``. ``None`` lets the
            factory read ``HELPDESK_ITSM_ADAPTER``.
        identity_kind: ``mock`` | ``entra`` | ``okta``.
        app_kind: ``mock`` | ``sap_fiori`` | ``vpn`` | ``shared_drive``.
        tickets: Optional ticket list used to seed the fallback ITSM store.

    Returns:
        ``{"itsm": ..., "identity": ..., "apps": ...}``.
    """
    try:
        from adapters import (  # noqa: WPS433 - intentional lazy import
            build_itsm_adapter,
            build_identity_adapter,
            build_app_adapter,
        )
    except ImportError as error:
        LOGGER.warning(
            "The 'adapters' package is not installed (%s). Falling back to the "
            "in-process mock connectors defined in agent.py. Install the "
            "adapters package to use ServiceNow / Jira / Entra / Okta.",
            error,
        )
        return _fallback_adapters(tickets)

    return {
        "itsm": build_itsm_adapter(itsm_kind),
        "identity": build_identity_adapter(identity_kind),
        "apps": build_app_adapter(app_kind),
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _new_run_id() -> str:
    """Return a run identifier of the form ``RUN-<UTC stamp>-<hash4>``."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "RUN-{stamp}-{hash4}".format(stamp=stamp, hash4=short_hash(stamp, 4))


def _resolve_path(path: str) -> str:
    """Resolve a path relative to the repository root."""
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate)
    return str(REPO_ROOT / candidate)


def _load_tickets(path: str) -> List[dict]:
    """Load the tickets of a CSV file into the ITSM ticket shape."""
    tickets: List[dict] = []
    for row in read_csv(path):
        description = row.get("description", "")
        tickets.append(
            {
                "ticket_id": row.get("ticket_id", ""),
                "title": description[:80],
                "description": description,
                "requester": row.get("requester", ""),
                "service": row.get("service", ""),
                "priority": row.get("priority", "P3"),
                "status": row.get("status", "new"),
                "created_at": row.get("created_at", ""),
                "source": "mock",
                "raw": {},
            }
        )
    return tickets


def _known_usernames() -> List[str]:
    """Return the usernames of the directory of record."""
    try:
        users = load_json(repo_path("data", "users.json"))
    except (OSError, ValueError):
        return []
    return sorted(users.keys())


def _directory_profiles() -> Dict[str, dict]:
    """Return the directory of record keyed by username."""
    try:
        return load_json(repo_path("data", "users.json"))
    except (OSError, ValueError):
        return {}


def _existing_action_ids(outputs_dir: str) -> set:
    """Return the action ids already present in ``outputs/actions.json``."""
    path = os.path.join(outputs_dir, "actions.json")
    try:
        payload = load_json(path)
    except (OSError, ValueError):
        return set()
    if not isinstance(payload, list):
        return set()
    return {
        str(item.get("action_id"))
        for item in payload
        if isinstance(item, dict) and item.get("action_id")
    }


def _build_comment(triage: Dict[str, Any], decision: Dict[str, Any]) -> str:
    """Build the comment posted back to the ITSM."""
    lines = [
        "[AI Help Desk Agent] Clasificación automática",
        "Categoría: {category} | Intención: {intent} | Prioridad: {priority}".format(
            category=triage.get("category"),
            intent=triage.get("intent"),
            priority=triage.get("priority"),
        ),
        "KB: {kb} | Confianza: {confidence}".format(
            kb=triage.get("kb_id") or "n/a", confidence=triage.get("confidence")
        ),
        "Decisión: {decision} | Riesgo: {risk}".format(
            decision=decision.get("decision"), risk=decision.get("risk")
        ),
        "Motivos: {reasons}".format(
            reasons="; ".join(decision.get("reasons") or []) or "n/a"
        ),
    ]
    if triage.get("suggested_resolution"):
        lines.append("Resolución propuesta: %s" % triage["suggested_resolution"])
    return "\n".join(lines)


def _perform(
    kind: str,
    action_name: str,
    payload: Dict[str, Any],
    adapters: Dict[str, Any],
    dry_run: bool,
) -> Dict[str, Any]:
    """Execute the effect associated with an action.

    Args:
        kind: One of the values of :data:`ACTION_EXECUTORS`.
        action_name: Canonical action name.
        payload: Action payload (``action``, ``target``, ``resource``,
            ``user``, ``params``).
        adapters: The connectors of the run.
        dry_run: When ``True`` the side effect is simulated, never applied.

    Returns:
        A result dictionary with at least a ``status`` key.
    """
    itsm = adapters.get("itsm")
    identity = adapters.get("identity")
    apps = adapters.get("apps")
    params = payload.get("params") or {}
    ticket_id = params.get("ticket_id")

    if kind == "none":
        return {"status": "skipped", "detail": "no side effect for this action"}
    if kind == "unsupported":
        return {
            "status": "failed",
            "detail": "action %s is not supported by the adapter contract" % action_name,
        }

    if kind == "itsm_comment":
        if dry_run:
            return {"status": "simulated", "detail": "comment not sent (dry run)"}
        result = itsm.add_comment(ticket_id, params.get("comment", ""))
        return {"status": "succeeded", "detail": result}

    if kind == "itsm_assign":
        queue = params.get("escalation_queue") or DEFAULT_QUEUE
        if dry_run:
            return {"status": "simulated", "detail": "assignment not sent (dry run)"}
        result = itsm.assign(ticket_id, queue)
        return {"status": "succeeded", "detail": result}

    if kind == "app_action":
        action_payload = dict(payload)
        action_payload["params"] = dict(params)
        result = apps.execute_action(action_payload)
        status = result.get("status", "failed")
        if dry_run and status == "succeeded":
            status = "simulated"
        return {"status": status, "detail": result}

    if kind == "identity_reset_password":
        try:
            result = identity.reset_password(
                payload.get("user"),
                verified=bool(params.get("verified")),
                verification_method=params.get("verification_method"),
            )
        except Exception as error:  # noqa: BLE001 - identity guardrails raise
            return {"status": "failed", "detail": str(error)}
        return {"status": "succeeded", "detail": result}

    if kind == "identity_unlock":
        try:
            result = identity.unlock_account(
                payload.get("user"), verified=bool(params.get("verified"))
            )
        except Exception as error:  # noqa: BLE001 - identity guardrails raise
            return {"status": "failed", "detail": str(error)}
        return {"status": "succeeded", "detail": result}

    if kind == "identity_request_access":
        try:
            result = identity.request_access(
                payload.get("user"),
                payload.get("resource"),
                justification=params.get("justification"),
            )
        except Exception as error:  # noqa: BLE001 - adapter guardrails raise
            return {"status": "failed", "detail": str(error)}
        return {"status": "succeeded", "detail": result}

    return {"status": "failed", "detail": "unknown executor %r" % kind}


def _make_executor(
    action_name: str,
    payload: Dict[str, Any],
    adapters: Dict[str, Any],
    dry_run: bool,
) -> Callable[[dict], dict]:
    """Return the callable that performs the effect of an action."""
    kind = ACTION_EXECUTORS.get(action_name, "none")

    def executor(approval: dict) -> dict:
        return _perform(kind, action_name, payload, adapters, dry_run)

    return executor


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def run_pipeline(
    tickets: List[dict],
    adapters: dict,
    *,
    dry_run: bool = True,
    verbose: bool = False,
    approve_all: bool = False,
    run_id: Optional[str] = None,
    outputs_dir: Optional[str] = None,
) -> dict:
    """Run the triage and remediation pipeline over a batch of tickets.

    Args:
        tickets: Tickets in the ITSM shape (see ``adapters.base``).
        adapters: ``{"itsm": ..., "identity": ..., "apps": ...}``.
        dry_run: When ``True`` no side effect is applied to the ITSM.
        verbose: Print a per-ticket narration.
        approve_all: Simulate the human approval of every pending request.
            Only meant for demos and tests: the audit log marks the decision
            with ``simulated_approval``.
        run_id: Identifier of the run (generated when omitted).
        outputs_dir: Directory where the artefacts are written.

    Returns:
        A summary dictionary with the run id, the per-ticket results, the
        metrics snapshot and the list of actions.
    """
    outputs_dir = outputs_dir or repo_path("outputs")
    os.makedirs(outputs_dir, exist_ok=True)
    run_id = run_id or _new_run_id()

    kb = load_json(repo_path("data", "kb.json"))
    risk_matrix = load_json(repo_path("policies", "risk_matrix.json"))
    access_policies = load_json(repo_path("policies", "access_policies.json"))
    escalation_rules = load_json(repo_path("policies", "escalation_rules.json"))

    triage_service = TriageService(
        kb,
        risk_matrix.get("priority_matrix"),
        risk_matrix.get("category_overrides"),
    )
    policy_engine = PolicyEngine(risk_matrix, access_policies, escalation_rules)
    approval_queue = ApprovalQueue(
        os.path.join(outputs_dir, "approvals.json"), risk_matrix
    )
    audit = AuditLogger(os.path.join(outputs_dir, "audit.log"), run_id)
    metrics = MetricsCollector()
    metrics.run_id = run_id

    kb_by_id = {article.get("id"): article for article in kb}
    directory = _directory_profiles()
    known_usernames = set(directory.keys()) or set(_known_usernames())
    known_action_ids = _existing_action_ids(outputs_dir)

    audit.log(
        "run_started",
        dry_run=dry_run,
        approve_all=approve_all,
        tickets=len(tickets),
        outputs_dir=outputs_dir,
        policy_version=policy_engine.policy_version,
    )

    results: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []

    for ticket in tickets:
        result = _process_ticket(
            ticket=ticket,
            triage_service=triage_service,
            policy_engine=policy_engine,
            approval_queue=approval_queue,
            audit=audit,
            metrics=metrics,
            adapters=adapters,
            dry_run=dry_run,
            verbose=verbose,
            directory=directory,
            known_usernames=known_usernames,
            known_action_ids=known_action_ids,
            kb_by_id=kb_by_id,
        )
        results.append(result)
        actions.extend(result.pop("actions", []))

    if approve_all:
        actions.extend(
            _approve_and_execute(
                approval_queue=approval_queue,
                audit=audit,
                metrics=metrics,
                adapters=adapters,
                dry_run=dry_run,
                verbose=verbose,
                results=results,
            )
        )

    _write_outputs(
        outputs_dir=outputs_dir,
        results=results,
        actions=actions,
        approval_queue=approval_queue,
        metrics=metrics,
    )

    snapshot = metrics.snapshot()
    audit.log(
        "run_completed",
        tickets=len(tickets),
        auto_resolved=snapshot["auto_resolved"],
        requires_approval=snapshot["requires_approval"],
        escalated=snapshot["escalated"],
    )
    audit.close()

    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "approve_all": approve_all,
        "outputs_dir": outputs_dir,
        "tickets": results,
        "actions": actions,
        "approvals": approval_queue.records(),
        "metrics": snapshot,
    }


def _process_ticket(
    *,
    ticket: dict,
    triage_service: TriageService,
    policy_engine: PolicyEngine,
    approval_queue: ApprovalQueue,
    audit: AuditLogger,
    metrics: MetricsCollector,
    adapters: dict,
    dry_run: bool,
    verbose: bool,
    directory: Dict[str, dict],
    known_usernames: set,
    known_action_ids: set,
    kb_by_id: Dict[str, dict],
) -> Dict[str, Any]:
    """Run the ten pipeline steps over a single ticket.

    A failure in one step never aborts the batch: the error is logged, the
    ticket is routed to a human and the run continues.
    """
    ticket_id = ticket.get("ticket_id", "")
    requester = ticket.get("requester", "")
    service = ticket.get("service", "")
    description = ticket.get("description", "")

    audit.log(
        "ticket_received",
        ticket_id,
        requester=requester,
        service=service,
        created_at=ticket.get("created_at"),
        description_present=bool(description),
    )

    # --- step 2: triage ------------------------------------------------- #
    started = time.perf_counter()
    try:
        triage = triage_service.classify(ticket)
    except Exception as error:  # noqa: BLE001 - a bad ticket must not kill the batch
        LOGGER.exception("Triage failed for %s", ticket_id)
        audit.log("integration_error", ticket_id, subsystem="triage", message=str(error))
        metrics.record_error(ticket_id, "triage", str(error))
        return {
            "ticket_id": ticket_id,
            "status": "error",
            "decision": "require_approval",
            "risk": "medium",
            "action": "human_review",
            "error": str(error),
            "actions": [],
        }
    triage_latency_ms = (time.perf_counter() - started) * 1000.0

    # The directory is the source of truth for the VIP flag and for the
    # mentioned users; the ticket text is never trusted for them.
    profile = directory.get(requester) or {}
    triage["vip"] = bool(profile.get("vip"))
    mentioned = triage.get("entities", {}).get("mentioned_users") or []
    triage["entities"]["mentioned_users"] = [
        name for name in mentioned if name in known_usernames
    ]

    audit.log(
        "triage_completed",
        ticket_id,
        category=triage.get("category"),
        intent=triage.get("intent"),
        priority=triage.get("priority"),
        impact=triage.get("impact"),
        urgency=triage.get("urgency"),
        confidence=triage.get("confidence"),
        kb_id=triage.get("kb_id"),
        matched_keywords=triage.get("matched_keywords"),
        matched_strong_keywords=triage.get("matched_strong_keywords"),
        score_breakdown=triage.get("score_breakdown"),
        suggested_action=triage.get("suggested_action"),
        latency_ms=round(triage_latency_ms, 3),
    )

    # --- step 3: identity ------------------------------------------------ #
    user: Dict[str, Any] = {}
    identity = adapters.get("identity")
    try:
        user = identity.get_user(requester) or {}
    except Exception as error:  # noqa: BLE001 - unknown user is a business case
        LOGGER.warning("Identity lookup failed for %s: %s", requester, error)
        audit.log(
            "integration_error", ticket_id, subsystem="identity", message=str(error)
        )
        metrics.record_error(ticket_id, "identity", str(error))

    audit.log(
        "identity_checked",
        ticket_id,
        user_found=bool(user),
        active=bool(user.get("active")),
        locked=bool(user.get("locked")),
        locked_by_admin=bool(user.get("locked_by_admin")),
        mfa_enrolled=bool(user.get("mfa_enrolled")),
        identity_verified=bool(user.get("identity_verified")),
        vip=bool(user.get("vip")),
    )

    identity_block: Optional[str] = None
    if not user:
        identity_block = "El solicitante no existe en el directorio"
    elif not user.get("active"):
        identity_block = "El solicitante está inactivo"

    if identity_block:
        # Never act automatically on an unknown or disabled account.
        triage["suggested_action"] = "human_review"
        triage["base_risk"] = "medium"

    # --- step 4: policy -------------------------------------------------- #
    resource = (triage.get("entities") or {}).get("resource")
    request = {"resource": resource} if resource else {}
    decision = policy_engine.evaluate(triage, user, request)
    if identity_block:
        decision["reasons"].insert(
            0,
            "%s: el ticket va a human_review y no se ejecuta ninguna acción "
            "automática" % identity_block,
        )
        decision["rule_ids"].insert(0, "IDENTITY-001")

    metrics.record_triage(
        ticket_id=ticket_id,
        category=triage.get("category"),
        priority=triage.get("priority"),
        confidence=triage.get("confidence"),
        latency_ms=triage_latency_ms,
        risk=decision.get("risk"),
        decision=decision.get("decision"),
    )

    audit.log(
        "policy_decided",
        ticket_id,
        decision=decision.get("decision"),
        risk=decision.get("risk"),
        base_risk=decision.get("base_risk"),
        approver_role=decision.get("approver_role"),
        escalation_queue=decision.get("escalation_queue"),
        effective_action=decision.get("effective_action"),
        risk_factors=decision.get("risk_factors"),
        reasons=decision.get("reasons"),
        rule_ids=decision.get("rule_ids"),
        confidence=decision.get("confidence"),
        timeout_minutes=decision.get("timeout_minutes"),
    )

    # --- step 5: action payload ----------------------------------------- #
    action_name = decision.get("effective_action") or "human_review"
    payload = {
        "action": action_name,
        "target": requester,
        "resource": resource,
        "user": requester,
        "params": {
            "ticket_id": ticket_id,
            "kb_id": triage.get("kb_id"),
            "justification": triage.get("suggested_resolution") or requester,
            "escalation_queue": decision.get("escalation_queue") or DEFAULT_QUEUE,
            "verified": bool(user.get("identity_verified")),
            "verification_method": "mfa" if user.get("mfa_enrolled") else None,
            "comment": _build_comment(triage, decision),
        },
    }
    audit.log(
        "action_proposed",
        ticket_id,
        action=action_name,
        target=requester,
        resource=resource,
        risk=decision.get("risk"),
        dry_run=dry_run,
    )

    article = kb_by_id.get(triage.get("kb_id")) or {}
    estimated_minutes = float(article.get("estimated_minutes") or 0.0)
    action_records: List[Dict[str, Any]] = []
    approval_id: Optional[str] = None
    status = "pending"

    if decision.get("decision") in ("require_approval", "escalate"):
        if decision.get("decision") == "escalate":
            queue = decision.get("escalation_queue") or "itsm-l2"
            metrics.record_escalation(
                ticket_id, queue, "; ".join(decision.get("reasons") or [])
            )
            audit.log(
                "escalation_raised",
                ticket_id,
                queue=queue,
                reasons=decision.get("reasons"),
                rule_ids=decision.get("rule_ids"),
            )
            status = "escalated"

        approval_request = ApprovalRequest(
            ticket_id=ticket_id,
            requester=requester,
            action=action_name,
            resource=resource,
            params=payload["params"],
            risk=decision.get("risk", "medium"),
            decision=decision.get("decision", "require_approval"),
            approver_role=decision.get("approver_role") or "support_lead",
            confidence=float(decision.get("confidence") or 0.0),
            reasons=list(decision.get("reasons") or []),
            run_id=audit.run_id,
            target=requester,
        )
        record = approval_queue.create(approval_request.to_dict())
        approval_id = record.get("approval_id")
        audit.log(
            "approval_created",
            ticket_id,
            approval_id=approval_id,
            action=action_name,
            approver_role=record.get("approver_role"),
            risk=record.get("risk"),
            expires_at=record.get("expires_at"),
            duplicate=record.get("duplicate", False),
        )
        if status != "escalated":
            status = "awaiting_approval"
        # The proposed action is recorded even when it is not executed: the
        # file shows what the agent decided to do with every ticket.
        action_records.append(
            _action_record(
                action_id=short_hash("%s|%s|%s" % (ticket_id, action_name, requester)),
                ticket_id=ticket_id,
                action=action_name,
                target=requester,
                resource=resource,
                risk=decision.get("risk"),
                status=status,
                dry_run=dry_run,
                result={
                    "detail": "waiting for the approval %s" % approval_id,
                    "approver_role": record.get("approver_role"),
                    "expires_at": record.get("expires_at"),
                },
                approval_id=approval_id,
                executed=False,
            )
        )
    else:
        # --- step 8: automatic execution -------------------------------- #
        risk = decision.get("risk")
        if risk in ("high", "critical"):
            # Defensive: the decision matrix never reaches this branch, but a
            # high or critical action is never executed automatically.
            LOGGER.warning(
                "Blocking automatic execution of a %s risk action on %s", risk, ticket_id
            )
            status = "blocked_high_risk"
            audit.log(
                "action_failed",
                ticket_id,
                action=action_name,
                risk=risk,
                reason="high_or_critical_risk_requires_approval",
            )
        else:
            action_id = short_hash("%s|%s|%s" % (ticket_id, action_name, requester))
            if action_id in known_action_ids:
                status = "duplicate_skipped"
                action_records.append(
                    _action_record(
                        action_id=action_id,
                        ticket_id=ticket_id,
                        action=action_name,
                        target=requester,
                        resource=resource,
                        risk=risk,
                        status="duplicate_skipped",
                        dry_run=dry_run,
                        result={"detail": "action already recorded in outputs/actions.json"},
                    )
                )
                metrics.record_action(
                    ticket_id=ticket_id,
                    action=action_name,
                    status="duplicate_skipped",
                    latency_ms=0.0,
                    estimated_minutes=estimated_minutes,
                    risk=risk,
                )
                audit.log(
                    "action_executed",
                    ticket_id,
                    action=action_name,
                    action_id=action_id,
                    status="duplicate_skipped",
                    dry_run=dry_run,
                )
            else:
                started = time.perf_counter()
                executor = _make_executor(action_name, payload, adapters, dry_run)
                result = executor({})
                latency_ms = (time.perf_counter() - started) * 1000.0
                result_status = result.get("status", "failed")
                known_action_ids.add(action_id)
                action_records.append(
                    _action_record(
                        action_id=action_id,
                        ticket_id=ticket_id,
                        action=action_name,
                        target=requester,
                        resource=resource,
                        risk=risk,
                        status=result_status,
                        dry_run=dry_run,
                        result=result,
                        approval_id=approval_id,
                    )
                )
                metrics.record_action(
                    ticket_id=ticket_id,
                    action=action_name,
                    status=result_status,
                    latency_ms=latency_ms,
                    estimated_minutes=estimated_minutes,
                    risk=risk,
                )
                if result_status in ("failed", "error"):
                    status = "action_failed"
                    audit.log(
                        "action_failed",
                        ticket_id,
                        action=action_name,
                        action_id=action_id,
                        risk=risk,
                        reason=str(result.get("detail"))[:500],
                    )
                else:
                    status = "auto_executed"
                    audit.log(
                        "action_executed",
                        ticket_id,
                        action=action_name,
                        action_id=action_id,
                        risk=risk,
                        status=result_status,
                        dry_run=dry_run,
                        latency_ms=round(latency_ms, 3),
                    )

    # --- step 9: ITSM write-back ---------------------------------------- #
    _write_back(
        ticket_id=ticket_id,
        triage=triage,
        decision=decision,
        adapters=adapters,
        audit=audit,
        dry_run=dry_run,
        auto_executed=status == "auto_executed",
    )

    if verbose:
        _print_ticket(
            triage=triage,
            decision=decision,
            status=status,
            approval_id=approval_id,
            action_records=action_records,
        )

    result = dict(triage)
    result.update(
        {
            "decision": decision.get("decision"),
            "risk": decision.get("risk"),
            "status": status,
            "run_id": audit.run_id,
            "approval_id": approval_id,
            "approver_role": decision.get("approver_role"),
            "escalation_queue": decision.get("escalation_queue"),
            "reasons": decision.get("reasons"),
            "rule_ids": decision.get("rule_ids"),
            "dry_run": dry_run,
        }
    )
    result["actions"] = action_records
    return result


def _action_record(
    *,
    action_id: str,
    ticket_id: str,
    action: str,
    target: str,
    resource: Optional[str],
    risk: str,
    status: str,
    dry_run: bool,
    result: dict,
    approval_id: Optional[str] = None,
    executed: bool = True,
) -> Dict[str, Any]:
    """Build the record written to ``outputs/actions.json``."""
    return {
        "action_id": action_id,
        "ticket_id": ticket_id,
        "action": action,
        "target": target,
        "resource": resource,
        "risk": risk,
        "status": status,
        "dry_run": dry_run,
        "created_at": utc_now_iso(),
        "executed_at": utc_now_iso() if executed else None,
        "result": result,
        # Extra field (not in the minimal SPEC shape) that makes the
        # "no high/critical action without approval" check verifiable straight
        # from the actions file.
        "approval_id": approval_id,
    }


def _write_back(
    *,
    ticket_id: str,
    triage: Dict[str, Any],
    decision: Dict[str, Any],
    adapters: dict,
    audit: AuditLogger,
    dry_run: bool,
    auto_executed: bool,
) -> None:
    """Post the outcome back to the ITSM, honouring the write kill-switch.

    Every individual ITSM operation is audited on its own (``add_comment``,
    ``set_priority``, ``assign`` and, only for automatically resolved tickets,
    ``resolve``) so that the trail shows exactly what was written.
    """
    itsm = adapters.get("itsm")
    queue = decision.get("escalation_queue") or DEFAULT_QUEUE
    comment = _build_comment(triage, decision)

    write_enabled = True
    if hasattr(itsm, "write_enabled"):
        try:
            write_enabled = bool(itsm.write_enabled())
        except Exception as error:  # noqa: BLE001 - defensive
            LOGGER.warning("write_enabled() failed: %s", error)
            write_enabled = False

    reason = None
    if dry_run:
        reason = "dry_run"
    elif not write_enabled:
        reason = "writes_disabled"

    operations = [
        ("add_comment", lambda: itsm.add_comment(ticket_id, comment)),
        ("set_priority", lambda: itsm.set_priority(ticket_id, triage.get("priority"))),
        ("assign", lambda: itsm.assign(ticket_id, queue)),
    ]
    for operation, call in operations:
        _write_back_operation(
            audit=audit,
            ticket_id=ticket_id,
            operation=operation,
            call=call,
            reason=reason,
            triage=triage,
            queue=queue,
        )

    # The ticket is only resolved when the agent fixed it on its own.
    _write_back_operation(
        audit=audit,
        ticket_id=ticket_id,
        operation="resolve",
        call=lambda: itsm.resolve(
            ticket_id, triage.get("suggested_resolution") or comment
        ),
        reason=reason if reason is not None else (
            None if auto_executed else "not_auto_executed"
        ),
        triage=triage,
        queue=queue,
    )


def _write_back_operation(
    *,
    audit: AuditLogger,
    ticket_id: str,
    operation: str,
    call,
    reason: Optional[str],
    triage: Dict[str, Any],
    queue: str,
) -> None:
    """Audit a single ITSM write-back operation."""
    if reason is not None:
        audit.log(
            "itsm_writeback",
            ticket_id,
            operation=operation,
            status="skipped",
            reason=reason,
            priority=triage.get("priority"),
            queue=queue,
        )
        return
    try:
        result = call()
        audit.log(
            "itsm_writeback",
            ticket_id,
            operation=operation,
            status="written",
            priority=triage.get("priority"),
            queue=queue,
            result=str(result)[:300],
        )
    except Exception as error:  # noqa: BLE001 - write-back must not kill the run
        LOGGER.warning("ITSM write-back failed for %s: %s", ticket_id, error)
        audit.log(
            "integration_error",
            ticket_id,
            subsystem="itsm.%s" % operation,
            message=str(error),
        )


def _approve_and_execute(
    *,
    approval_queue: ApprovalQueue,
    audit: AuditLogger,
    metrics: MetricsCollector,
    adapters: dict,
    dry_run: bool,
    verbose: bool,
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Simulate the human approval of every pending request (demo only)."""
    action_records: List[Dict[str, Any]] = []
    for approval in list(approval_queue.pending()):
        role = approval.get("approver_role") or "support_lead"
        approver = "%s_auto" % role
        outcome = approval_queue.approve(
            approval.get("approval_id"), approver, "simulated approval (--approve-all)"
        )
        if outcome.get("status") != "approved":
            audit.log(
                "integration_error",
                approval.get("ticket_id"),
                subsystem="approval",
                message=str(outcome.get("error")),
            )
            continue
        audit.log(
            "approval_resolved",
            approval.get("ticket_id"),
            actor="human",
            approval_id=approval.get("approval_id"),
            approver=approver,
            approver_role=role,
            outcome="approved",
            simulated_approval=True,
        )
        metrics.record_approval(approval.get("ticket_id"), "approved", role)

        payload = {
            "action": approval.get("action"),
            "target": approval.get("requester"),
            "resource": approval.get("resource"),
            "user": approval.get("requester"),
            "params": approval.get("params") or {},
        }
        executor = _make_executor(
            approval.get("action"), payload, adapters, dry_run
        )
        execution = apply_approved_action(approval, executor)
        action_id = approval.get("action_id") or short_hash(
            "%s|%s|%s"
            % (approval.get("ticket_id"), approval.get("action"), approval.get("requester"))
        )
        status = "executed" if execution.get("executed") else "not_executed"
        action_records.append(
            _action_record(
                action_id=action_id,
                ticket_id=approval.get("ticket_id"),
                action=approval.get("action"),
                target=approval.get("requester"),
                resource=approval.get("resource"),
                risk=approval.get("risk"),
                status=status,
                dry_run=dry_run,
                result=execution,
                approval_id=approval.get("approval_id"),
            )
        )
        if execution.get("executed"):
            audit.log(
                "action_executed",
                approval.get("ticket_id"),
                action=approval.get("action"),
                action_id=action_id,
                approval_id=approval.get("approval_id"),
                risk=approval.get("risk"),
                status="executed",
                dry_run=dry_run,
                simulated_approval=True,
            )
            for result in results:
                if result.get("ticket_id") == approval.get("ticket_id"):
                    result["status"] = "approved_and_executed"
        else:
            audit.log(
                "action_failed",
                approval.get("ticket_id"),
                action=approval.get("action"),
                approval_id=approval.get("approval_id"),
                reason=str(execution.get("reason")),
            )
    return action_records


def _write_outputs(
    *,
    outputs_dir: str,
    results: List[Dict[str, Any]],
    actions: List[Dict[str, Any]],
    approval_queue: ApprovalQueue,
    metrics: MetricsCollector,
) -> None:
    """Write every artefact of the run into ``outputs_dir``."""
    approval_queue.save()
    save_json(os.path.join(outputs_dir, "triaged_tickets.json"), results)

    csv_rows: List[Dict[str, Any]] = []
    for result in results:
        csv_rows.append(
            {
                "ticket_id": result.get("ticket_id"),
                "requester": result.get("requester"),
                "service": result.get("service"),
                "category": result.get("category"),
                "intent": result.get("intent"),
                "priority": result.get("priority"),
                "impact": result.get("impact"),
                "urgency": result.get("urgency"),
                "confidence": result.get("confidence"),
                "risk": result.get("risk"),
                "decision": result.get("decision"),
                "action": result.get("effective_action") or result.get("suggested_action"),
                "status": result.get("status"),
                "kb_id": result.get("kb_id"),
                "approval_id": result.get("approval_id"),
            }
        )
    write_csv(
        os.path.join(outputs_dir, "triaged_tickets.csv"),
        csv_rows,
        TRIAGED_CSV_COLUMNS,
    )
    save_json(os.path.join(outputs_dir, "actions.json"), actions)
    metrics.write_json(os.path.join(outputs_dir, "metrics.json"))
    metrics.write_markdown(os.path.join(outputs_dir, "metrics.md"))


def _print_ticket(
    *,
    triage: Dict[str, Any],
    decision: Dict[str, Any],
    status: str,
    approval_id: Optional[str],
    action_records: List[Dict[str, Any]],
) -> None:
    """Print the per-ticket narration of the verbose/demo modes."""
    breakdown = triage.get("score_breakdown") or {}
    print(
        "[{ticket_id}] {service} | category={category} | intent={intent} | "
        "priority={priority} | impact={impact} | urgency={urgency}".format(
            ticket_id=triage.get("ticket_id"),
            service=triage.get("service"),
            category=triage.get("category"),
            intent=triage.get("intent"),
            priority=triage.get("priority"),
            impact=triage.get("impact"),
            urgency=triage.get("urgency"),
        )
    )
    print(
        "         KB={kb} | strong=[{strong}] | weak=[{weak}]".format(
            kb=triage.get("kb_id") or "none",
            strong=", ".join(triage.get("matched_strong_keywords") or []) or "-",
            weak=", ".join(triage.get("matched_keywords") or []) or "-",
        )
    )
    print(
        "         confidence={confidence:.2f} = 0.50*coverage({coverage:.3f}) + "
        "0.30*evidence({evidence:.2f}) + 0.20*specificity({specificity:.2f}) + "
        "service_bonus({bonus:.2f})".format(
            confidence=float(triage.get("confidence") or 0.0),
            coverage=float(breakdown.get("coverage") or 0.0),
            evidence=float(breakdown.get("evidence") or 0.0),
            specificity=float(breakdown.get("specificity") or 0.0),
            bonus=float(breakdown.get("service_bonus") or 0.0),
        )
    )
    print(
        "         risk={risk} | decision={decision} | action={action} | "
        "approver={approver} | status={status}".format(
            risk=decision.get("risk"),
            decision=decision.get("decision"),
            action=decision.get("effective_action"),
            approver=decision.get("approver_role") or "-",
            status=status,
        )
    )
    for reason in decision.get("reasons") or []:
        print("         reason: %s" % reason)
    if decision.get("rule_ids"):
        print("         rule_ids: %s" % ", ".join(decision["rule_ids"]))
    if approval_id:
        print("         approval: %s" % approval_id)
    for record in action_records:
        print(
            "         action_result: %s -> %s"
            % (record.get("action"), record.get("status"))
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description=(
            "AI Help Desk Triage & Remediation Agent: classifies tickets, "
            "applies the risk policy, asks for human approval when needed and "
            "leaves a full audit trail."
        ),
        epilog=(
            "Examples:\n"
            "  python3 agent.py --demo\n"
            "  python3 agent.py --input data/tickets.csv --dry-run --verbose\n"
            "  python3 agent.py --input data/tickets.csv --verbose --write-back\n"
            "  python3 -m unittest discover -s tests -t . -v\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        default="data/tickets.csv",
        help="CSV file with the tickets to process (default: data/tickets.csv)",
    )
    parser.add_argument(
        "--adapter",
        choices=["mock", "servicenow", "jira"],
        default=None,
        help="ITSM connector (default: the HELPDESK_ITSM_ADAPTER env var, else mock)",
    )
    parser.add_argument(
        "--identity-adapter",
        choices=["mock", "entra", "okta"],
        default=None,
        help="Directory connector (default: the HELPDESK_IDENTITY_ADAPTER env var, else mock)",
    )
    parser.add_argument(
        "--app-adapter",
        choices=["mock", "sap_fiori", "vpn", "shared_drive"],
        default=None,
        help="Application connector (default: the HELPDESK_APP_ADAPTER env var, else mock)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never write to the ITSM (this is the default behaviour)",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help=(
            "Allow the write-back to the ITSM. It still requires the "
            "HELPDESK_ALLOW_WRITES=1 kill-switch to have an effect."
        ),
    )
    parser.add_argument("--verbose", action="store_true", help="Narrate every ticket")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Demo mode: dry run, verbose narration and the final metrics table",
    )
    parser.add_argument(
        "--approve-all",
        action="store_true",
        help="Simulate the human approval of every pending request (demo only)",
    )
    parser.add_argument(
        "--outputs-dir",
        default="outputs",
        help="Directory where the artefacts are written (default: outputs)",
    )
    parser.add_argument("--run-id", default=None, help="Identifier of the run")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point of the CLI.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when the run completed (even with per-ticket errors) and ``1``
        on a fatal configuration error.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)

    dry_run = not args.write_back
    if args.dry_run:
        dry_run = True
    if args.demo:
        dry_run = True
        args.verbose = True

    input_path = _resolve_path(args.input)
    if not os.path.isfile(input_path):
        print("ERROR: input file not found: %s" % input_path, file=sys.stderr)
        return 1

    try:
        tickets = _load_tickets(input_path)
    except (OSError, ValueError) as error:
        print("ERROR: cannot read %s: %s" % (input_path, error), file=sys.stderr)
        return 1

    try:
        adapters = build_adapters(
            args.adapter, args.identity_adapter, args.app_adapter, tickets
        )
    except Exception as error:  # noqa: BLE001 - configuration errors are fatal
        print("ERROR: cannot build the adapters: %s" % error, file=sys.stderr)
        return 1

    if args.write_back and os.environ.get("HELPDESK_ALLOW_WRITES") != "1":
        print(
            "WARNING: --write-back was requested but HELPDESK_ALLOW_WRITES is not "
            "set to 1: the real connectors stay read-only (the in-memory mock "
            "still records the operations).",
            file=sys.stderr,
        )

    outputs_dir = _resolve_path(args.outputs_dir)
    summary = run_pipeline(
        tickets,
        adapters,
        dry_run=dry_run,
        verbose=args.verbose,
        approve_all=args.approve_all,
        run_id=args.run_id,
        outputs_dir=outputs_dir,
    )

    metrics = summary["metrics"]
    print("")
    print("Run %s finished: %s tickets processed" % (summary["run_id"], len(tickets)))
    print(
        "  auto_resolved=%s  requires_approval=%s  escalated=%s  unknown=%s"
        % (
            metrics["auto_resolved"],
            metrics["requires_approval"],
            metrics["escalated"],
            metrics["unknown_category"],
        )
    )
    print(
        "  deflection_rate=%.2f  average_confidence=%.2f  "
        "estimated_minutes_saved=%.1f"
        % (
            metrics["deflection_rate"],
            metrics["average_confidence"],
            metrics["estimated_minutes_saved"],
        )
    )
    print("  artefacts: %s" % summary["outputs_dir"])
    if args.demo:
        print("")
        print("| Metric | Value |")
        print("| --- | --- |")
        for key in sorted(metrics):
            value = metrics[key]
            if isinstance(value, dict):
                value = ", ".join(
                    "%s=%s" % (name, count) for name, count in sorted(value.items())
                )
            print("| %s | %s |" % (key, value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
