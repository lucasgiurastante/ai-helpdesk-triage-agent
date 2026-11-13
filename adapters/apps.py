"""Business application connectors (SAP Fiori, VPN, shared drive).

The adapters expose three primitives that the remediation planner uses
(SPEC section 3.1.3):

* ``check_status(app)`` -- is the application healthy right now?
* ``execute_action(action)`` -- apply (or simulate) a remediation;
* ``list_resources()`` -- which resources does this application expose?

Guardrails implemented here:

* an **allow-list** of canonical actions (SPEC section 2.5): anything else
  is skipped, never executed;
* the write kill switch: without ``HELPDESK_ALLOW_WRITES=1`` the action is
  *simulated* (``status="simulated"``) and never reported as executed;
* a deterministic ``action_id`` so replaying an action is idempotent.
"""

from typing import Any, Dict, List

from .base import (
    AppAdapter,
    action_id_for,
    utc_now_iso,
    writes_enabled,
)

#: Canonical actions the agent is allowed to execute (SPEC section 2.5).
ALLOWED_ACTIONS = (
    "send_kb_article",
    "password_reset_link",
    "unlock_account",
    "grant_role",
    "grant_folder_access",
    "request_access_approval",
    "assign_software_package",
    "restart_service",
    "escalate_network_team",
    "escalate_security_team",
    "disable_user",
    "human_review",
    "no_action",
)

#: Resources exposed by each mocked application.
MOCK_RESOURCES: Dict[str, List[Dict[str, Any]]] = {
    "SAP Fiori": [
        {"resource": "sap-fiori-basic", "type": "application", "sensitive": False, "owner": "IT Support"},
        {"resource": "sap-fiori-admin", "type": "application", "sensitive": True, "owner": "SAP Basis"},
    ],
    "VPN": [
        {"resource": "vpn-access", "type": "network", "sensitive": False, "owner": "Network Team"},
        {"resource": "vpn-client-package", "type": "package", "sensitive": False, "owner": "IT Support"},
    ],
    "Shared Drive": [
        {"resource": "shared-drive-team", "type": "storage", "sensitive": False, "owner": "IT Support"},
        {"resource": "finance-folder", "type": "storage", "sensitive": True, "owner": "Finance"},
    ],
}


class MockApps(AppAdapter):
    """In-memory application connector.

    Args:
        service: Service name (``SAP Fiori``, ``VPN``, ``Shared Drive``).
        status: Operational status reported by :meth:`check_status`.
    """

    name = "mock"

    def __init__(self, service: str = "SAP Fiori", status: str = "operational") -> None:
        self.service = service
        self.status = status

    # -- helpers ---------------------------------------------------------- #

    def write_enabled(self) -> bool:
        """Return ``True`` when real writes are allowed (kill switch)."""
        return writes_enabled()

    def _resources(self) -> List[Dict[str, Any]]:
        """Return the resources of the mocked service.

        Returns:
            The list of resource dictionaries (empty for unknown services).
        """
        return [dict(resource) for resource in MOCK_RESOURCES.get(self.service, [])]

    # -- API -------------------------------------------------------------- #

    def check_status(self, app: str) -> Dict[str, Any]:
        """Return the operational status of an application.

        Args:
            app: Name of the application.  When it matches the service of the
                adapter the configured status is returned.

        Returns:
            The status dictionary.
        """
        status = self.status if app == self.service else "operational"
        details = {
            "service": self.service,
            "checked_resource_count": len(self._resources()),
            "source": "mock",
        }
        incident_ref = None
        if status != "operational":
            incident_ref = "SIM-{0}".format(app.upper().replace(" ", "-"))
        return {
            "app": app,
            "status": status,
            "checked_at": utc_now_iso(),
            "details": details,
            "incident_ref": incident_ref,
        }

    def execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute (or simulate) a remediation action.

        Args:
            action: Mapping with ``action``, ``target``, ``resource``,
                ``user`` and ``params`` keys.

        Returns:
            The result dictionary with ``action_id``, ``action``, ``status``,
            ``detail``, ``executed_at`` and ``dry_run``.
        """
        payload = action or {}
        action_name = str(payload.get("action", "") or "")
        target = str(payload.get("target", "") or "")
        resource = str(payload.get("resource", "") or "")
        user = str(payload.get("user", "") or "")
        params = payload.get("params", {}) or {}
        action_id = action_id_for(user, action_name, resource)
        timestamp = utc_now_iso()

        if not action_name:
            return {
                "action_id": action_id,
                "action": action_name,
                "status": "failed",
                "reason": "invalid_action_payload",
                "detail": "The action payload does not contain an 'action' key",
                "executed_at": timestamp,
                "dry_run": not writes_enabled(),
            }

        if action_name not in ALLOWED_ACTIONS:
            return {
                "action_id": action_id,
                "action": action_name,
                "status": "skipped",
                "reason": "action_not_allowed",
                "detail": "Action '{0}' is not in the allow-list".format(action_name),
                "executed_at": timestamp,
                "dry_run": True,
            }

        if not writes_enabled():
            return {
                "action_id": action_id,
                "action": action_name,
                "status": "simulated",
                "detail": "Writes are disabled (HELPDESK_ALLOW_WRITES!=1): the action was "
                "simulated, not executed",
                "executed_at": timestamp,
                "dry_run": True,
                "target": target,
                "resource": resource,
                "user": user,
                "params": params,
            }

        return {
            "action_id": action_id,
            "action": action_name,
            "status": "succeeded",
            "detail": "Action '{0}' applied to {1}".format(action_name, target or resource or user),
            "executed_at": timestamp,
            "dry_run": False,
            "target": target,
            "resource": resource,
            "user": user,
            "params": params,
        }

    def list_resources(self) -> List[Dict[str, Any]]:
        """Return the resources exposed by the application.

        Returns:
            The list of resource dictionaries.
        """
        return self._resources()


class MockSAPFioriApp(MockApps):
    """Mocked SAP Fiori application."""

    name = "sap_fiori"

    def __init__(self, status: str = "operational") -> None:
        super().__init__(service="SAP Fiori", status=status)


class MockVPNApp(MockApps):
    """Mocked corporate VPN application."""

    name = "vpn"

    def __init__(self, status: str = "operational") -> None:
        super().__init__(service="VPN", status=status)


class MockSharedDriveApp(MockApps):
    """Mocked shared drive application."""

    name = "shared_drive"

    def __init__(self, status: str = "operational") -> None:
        super().__init__(service="Shared Drive", status=status)


__all__ = [
    "ALLOWED_ACTIONS",
    "MOCK_RESOURCES",
    "MockApps",
    "MockSAPFioriApp",
    "MockSharedDriveApp",
    "MockVPNApp",
]
