"""Human-in-the-loop approval queue.

The queue owns the lifecycle of an approval request: creation, decision,
expiry, supersession and the (only) path that allows a risky action to be
executed. The invariant enforced by :func:`apply_approved_action` is simple
and absolute: an action with ``high`` or ``critical`` risk is never executed
without a recorded, explicit human decision (SPEC section 3.5).
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.common import (
    APPROVAL_TIMEOUT_MINUTES,
    ensure_directory,
    load_json,
    save_json,
    sha256_hex,
    shift_iso,
    short_hash,
    utc_now_iso,
)

LOGGER = logging.getLogger(__name__)

#: Full life-cycle of an approval record -- SPEC section 2.9 / 3.5.
APPROVAL_STATUSES: Tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "expired",
    "superseded",
    "executed",
    "failed",
)

#: Suffix used by the simulated approver of the ``--approve-all`` demo flag.
AUTO_APPROVER_SUFFIX = "_auto"


def split_approver(approver: str) -> Tuple[str, str]:
    """Split an approver identifier into ``(role, name)``.

    Three notations are accepted:

    * ``"support_lead:ana.gomez"`` -- explicit role and name;
    * ``"support_lead_auto"`` -- simulated approval of the demo flag;
    * ``"support_lead"`` -- the role is used as the name.

    Args:
        approver: The approver identifier.

    Returns:
        A ``(role, name)`` tuple. The role is empty when nothing could be
        extracted.
    """
    if not approver:
        return "", ""
    if ":" in approver:
        role, _, name = approver.partition(":")
        return role.strip(), name.strip() or role.strip()
    if approver.endswith(AUTO_APPROVER_SUFFIX):
        role = approver[: -len(AUTO_APPROVER_SUFFIX)]
        return role.strip(), approver
    return approver.strip(), approver.strip()


class ApprovalQueue:
    """Persistent queue of approval requests.

    Args:
        path: Path of the JSON file backing the queue (usually
            ``outputs/approvals.json``).
        policy: The risk matrix. It provides the per level timeouts and may
            override the role timeouts with a ``role_timeouts`` section.
    """

    def __init__(self, path: str, policy: dict) -> None:
        self.path = path
        self.policy: Dict[str, Any] = dict(policy or {})
        self._records: List[Dict[str, Any]] = []
        self.load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def load(self) -> List[dict]:
        """(Re)load the queue from disk.

        Returns:
            The list of approval records. A missing file yields an empty queue.
        """
        try:
            data = load_json(self.path)
        except FileNotFoundError:
            self._records = []
            return self._records
        except ValueError as error:
            LOGGER.warning("Ignoring unreadable approval queue %s: %s", self.path, error)
            self._records = []
            return self._records
        self._records = list(data) if isinstance(data, list) else []
        return self._records

    def save(self) -> None:
        """Persist the queue to disk (JSON array, indent 2)."""
        ensure_directory(self.path)
        save_json(self.path, self._records)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _timeout_for(self, role: Optional[str], fallback: Optional[int] = None) -> int:
        """Return the timeout (minutes) applicable to an approver role."""
        role_timeouts = self.policy.get("role_timeouts") or {}
        if role and role in role_timeouts:
            return int(role_timeouts[role])
        if role and role in APPROVAL_TIMEOUT_MINUTES:
            return APPROVAL_TIMEOUT_MINUTES[role]
        if fallback:
            return int(fallback)
        levels = self.policy.get("levels") or {}
        for candidate in ("support_lead", "manager", "security", "on_call"):
            level = levels.get(candidate)
            if isinstance(level, dict) and isinstance(level.get("timeout_minutes"), int):
                return int(level["timeout_minutes"])
        return 60

    @staticmethod
    def build_approval_id(ticket_id: str, action: str, resource: Optional[str]) -> str:
        """Build the deterministic approval id (SPEC section 3.5.4).

        Args:
            ticket_id: Ticket identifier.
            action: Canonical action name.
            resource: Requested resource (may be ``None``).

        Returns:
            ``APR-<ticket_id>-<HASH8>``.
        """
        digest = sha256_hex(f"{ticket_id}|{action}|{resource or ''}")[:8].upper()
        return f"APR-{ticket_id}-{digest}"

    @staticmethod
    def build_idempotency_key(
        ticket_id: str, action: str, resource: Optional[str], target: str
    ) -> str:
        """Build the idempotency key of an approval request (SPEC 3.5.5)."""
        return sha256_hex(f"{ticket_id}|{action}|{resource or ''}|{target}")[:16]

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def create(self, request: dict) -> Dict[str, Any]:
        """Register an approval request. This method never executes anything.

        When a pending request with the same idempotency key already exists it
        is returned as a duplicate instead of creating a second record. Any
        other pending request for the same ticket *and* the same action is
        superseded by the new one.

        Args:
            request: Mapping with at least ``ticket_id``, ``action`` and
                ``approver_role``.

        Returns:
            The approval record (a copy) with a ``duplicate`` flag.
        """
        request = request or {}
        ticket_id = request.get("ticket_id") or ""
        action = request.get("action") or ""
        resource = request.get("resource")
        target = request.get("target") or request.get("requester") or ""
        approval_id = self.build_approval_id(ticket_id, action, resource)
        idempotency_key = self.build_idempotency_key(
            ticket_id, action, resource, target
        )

        for record in self._records:
            if (
                record.get("idempotency_key") == idempotency_key
                and record.get("status") == "pending"
            ):
                duplicate = dict(record)
                duplicate["duplicate"] = True
                return duplicate

        created_at = utc_now_iso()
        timeout = self._timeout_for(
            request.get("approver_role"), request.get("timeout_minutes")
        )

        for record in self._records:
            if (
                record.get("status") == "pending"
                and record.get("ticket_id") == ticket_id
                and record.get("action") == action
            ):
                record["status"] = "superseded"
                record["superseded_by"] = approval_id
                record["decided_at"] = created_at
                record["reason"] = "superseded_by_new_request"

        record = {
            "approval_id": approval_id,
            "idempotency_key": idempotency_key,
            "run_id": request.get("run_id") or "",
            "ticket_id": ticket_id,
            "requester": request.get("requester") or "",
            "action": action,
            "resource": resource,
            "params": request.get("params") or {},
            "risk": request.get("risk") or "low",
            "decision": request.get("decision") or "require_approval",
            "approver_role": request.get("approver_role") or "support_lead",
            "approver": None,
            "status": "pending",
            "reason": request.get("reason") or "",
            "reasons": list(request.get("reasons") or []),
            "confidence": float(request.get("confidence") or 0.0),
            "created_at": created_at,
            "expires_at": shift_iso(created_at, timeout),
            "decided_at": None,
            "decided_by": None,
            "decision_note": None,
            "superseded_by": None,
            "action_id": request.get("action_id")
            or short_hash(f"{ticket_id}|{action}|{target}"),
            "duplicate": False,
        }
        self._records.append(record)
        self.save()
        return dict(record)

    def get(self, approval_id: str) -> Optional[dict]:
        """Return the record with the given id, or ``None``."""
        for record in self._records:
            if record.get("approval_id") == approval_id:
                return record
        return None

    def records(self) -> List[dict]:
        """Return a copy of every record of the queue."""
        return [dict(record) for record in self._records]

    def pending(self, ticket_id: Optional[str] = None) -> List[dict]:
        """Return the pending approvals, optionally filtered by ticket."""
        return [
            record
            for record in self._records
            if record.get("status") == "pending"
            and (ticket_id is None or record.get("ticket_id") == ticket_id)
        ]

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def _authorize(self, record: Dict[str, Any], approver: str) -> Optional[Dict[str, Any]]:
        """Validate the approver and its role against the record."""
        if not approver or not approver.strip():
            return {"status": "error", "error": "approver_required"}
        role, _ = split_approver(approver)
        expected = record.get("approver_role")
        if expected and role != expected:
            return {
                "status": "error",
                "error": "approver_role_mismatch",
                "expected_role": expected,
                "provided_role": role,
            }
        return None

    def approve(self, approval_id: str, approver: str, note: str = "") -> Dict[str, Any]:
        """Approve a pending request.

        Args:
            approval_id: Identifier of the approval.
            approver: Approver identifier (role encoded, see
                :func:`split_approver`).
            note: Optional note stored as ``decision_note``.

        Returns:
            ``{"status": "approved", "approval": {...}}`` or an error dictionary.
        """
        record = self.get(approval_id)
        if record is None:
            return {"status": "error", "error": "approval_not_found", "approval_id": approval_id}
        if record.get("status") != "pending":
            return {
                "status": "error",
                "error": "not_pending",
                "current_status": record.get("status"),
                "approval_id": approval_id,
            }
        error = self._authorize(record, approver)
        if error:
            error["approval_id"] = approval_id
            return error

        _, name = split_approver(approver)
        record["status"] = "approved"
        record["approver"] = approver
        record["decided_by"] = name
        record["decided_at"] = utc_now_iso()
        record["decision_note"] = note
        self.save()
        return {"status": "approved", "approval": dict(record)}

    def reject(self, approval_id: str, approver: str, note: str = "") -> Dict[str, Any]:
        """Reject a pending request. A decision note is mandatory.

        Args:
            approval_id: Identifier of the approval.
            approver: Approver identifier.
            note: Mandatory justification.

        Returns:
            ``{"status": "rejected", "approval": {...}}`` or an error dictionary.
        """
        record = self.get(approval_id)
        if record is None:
            return {"status": "error", "error": "approval_not_found", "approval_id": approval_id}
        if record.get("status") != "pending":
            return {
                "status": "error",
                "error": "not_pending",
                "current_status": record.get("status"),
                "approval_id": approval_id,
            }
        if not note or not note.strip():
            return {
                "status": "error",
                "error": "decision_note_required",
                "approval_id": approval_id,
            }
        error = self._authorize(record, approver)
        if error:
            error["approval_id"] = approval_id
            return error

        _, name = split_approver(approver)
        record["status"] = "rejected"
        record["approver"] = approver
        record["decided_by"] = name
        record["decided_at"] = utc_now_iso()
        record["decision_note"] = note
        self.save()
        return {"status": "rejected", "approval": dict(record)}

    def expire_due(self, now: Optional[str] = None) -> List[dict]:
        """Expire every pending approval whose deadline has passed.

        Args:
            now: Reference timestamp (ISO-8601 UTC). Defaults to "now".

        Returns:
            The list of expired records.
        """
        reference = now or utc_now_iso()
        expired: List[dict] = []
        for record in self._records:
            if record.get("status") != "pending":
                continue
            expires_at = record.get("expires_at")
            if not expires_at or expires_at >= reference:
                continue
            record["status"] = "expired"
            record["decided_at"] = reference
            record["reason"] = "expired_timeout"
            expired.append(record)
        if expired:
            self.save()
        return expired

    def supersede(self, approval_id: str, reason: str) -> Dict[str, Any]:
        """Supersede a pending approval manually.

        Args:
            approval_id: Identifier of the approval.
            reason: Why the request is superseded.

        Returns:
            The updated record or an error dictionary.
        """
        record = self.get(approval_id)
        if record is None:
            return {"status": "error", "error": "approval_not_found", "approval_id": approval_id}
        if record.get("status") != "pending":
            return {
                "status": "error",
                "error": "not_pending",
                "current_status": record.get("status"),
                "approval_id": approval_id,
            }
        record["status"] = "superseded"
        record["decided_at"] = utc_now_iso()
        record["reason"] = reason or "superseded"
        self.save()
        return {"status": "superseded", "approval": dict(record)}

    def stats(self) -> Dict[str, Any]:
        """Return the counters of the queue."""
        by_status: Dict[str, int] = {}
        for record in self._records:
            status = str(record.get("status", "unknown"))
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total": len(self._records),
            "by_status": by_status,
            "pending": by_status.get("pending", 0),
            "approved": by_status.get("approved", 0),
            "rejected": by_status.get("rejected", 0),
            "expired": by_status.get("expired", 0),
            "superseded": by_status.get("superseded", 0),
        }


def apply_approved_action(
    approval: dict, executor: Callable[[dict], dict]
) -> Dict[str, Any]:
    """Execute an action, but only behind a valid human approval.

    The guardrails are:

    * the approval must be in the ``approved`` state;
    * for ``high`` and ``critical`` risk actions a recorded human decision
      (``decided_by``) is mandatory;
    * the executor is never called otherwise.

    Args:
        approval: The approval record.
        executor: Callable receiving the approval and returning the execution
            result.

    Returns:
        ``{"executed": True, "status": ..., "result": ...}`` or
        ``{"executed": False, "reason": ...}``.
    """
    status = (approval or {}).get("status")
    if status != "approved":
        reason = "approval_required" if status == "pending" else str(status)
        return {"executed": False, "reason": reason}

    risk = (approval or {}).get("risk")
    if risk in ("high", "critical") and not approval.get("decided_by"):
        return {"executed": False, "reason": "no_human_decision"}

    try:
        result = executor(approval)
    except Exception as error:  # noqa: BLE001 - a failing action must not kill the batch
        LOGGER.warning(
            "Action %s failed: %s", approval.get("action_id"), error, exc_info=True
        )
        approval["status"] = "failed"
        approval["reason"] = "execution_error: %s" % error
        return {"executed": False, "reason": "execution_error", "error": str(error)}

    approval["status"] = "executed"
    approval["decided_at"] = approval.get("decided_at") or utc_now_iso()
    return {
        "executed": True,
        "status": "executed",
        "approval_id": approval.get("approval_id"),
        "result": result,
    }


__all__ = ["APPROVAL_STATUSES", "ApprovalQueue", "apply_approved_action", "split_approver"]
