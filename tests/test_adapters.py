"""Tests of the connector layer: the mocks, the guardrails and the factories.

The tests never touch the network: they exercise the in-memory mocks, the
security guardrails of the identity adapter and the factory contract of
SPEC section 3.2.1.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from adapters import (  # noqa: E402
    APP_KINDS,
    IDENTITY_KINDS,
    ITSM_KINDS,
    build_app_adapter,
    build_identity_adapter,
    build_itsm_adapter,
)
from adapters.apps import MockApps, MockSAPFioriApp, MockSharedDriveApp, MockVPNApp  # noqa: E402
from adapters.base import (  # noqa: E402
    AdapterConfigError,
    AdapterNotFoundError,
    VerificationRequired,
    load_dotenv,
    require_requests,
    sanitize_secrets,
    utc_now_iso,
)
from adapters.identity import MockIdentity  # noqa: E402
from adapters.itsm import JiraITSM, MockITSM, ServiceNowITSM  # noqa: E402

TICKETS_CSV = (
    "ticket_id,requester,description,service,created_at\n"
    "TCK-001,juan.perez,No puedo entrar a SAP Fiori,SAP Fiori,2026-11-10T09:15:00Z\n"
    "TCK-002,maria.gomez,Se cayo el wifi del piso,Network,2026-11-10T10:00:00Z\n"
)

USERS_JSON = {
    "juan.perez": {
        "active": True,
        "locked": False,
        "locked_by_admin": False,
        "manager": "maria.gomez",
        "groups": ["sap-users"],
        "vip": False,
        "mfa_enrolled": True,
        "identity_verified": True,
        "password_last_set_days": 10,
        "email": "juan.perez@acme.com",
        "display_name": "Juan Perez",
        "department": "Finance",
    },
    "maria.gomez": {
        "active": True,
        "locked": False,
        "locked_by_admin": False,
        "manager": None,
        "groups": ["sap-users"],
        "vip": True,
        "mfa_enrolled": True,
        "identity_verified": True,
        "password_last_set_days": 20,
        "email": "maria.gomez@acme.com",
        "display_name": "Maria Gomez",
        "department": "IT",
    },
    "carlos.ruiz": {
        "active": True,
        "locked": True,
        "locked_by_admin": False,
        "manager": "maria.gomez",
        "groups": [],
        "vip": False,
        "mfa_enrolled": False,
        "identity_verified": False,
        "password_last_set_days": 120,
        "email": "carlos.ruiz@acme.com",
        "display_name": "Carlos Ruiz",
        "department": "Operations",
    },
    "locked.admin": {
        "active": True,
        "locked": True,
        "locked_by_admin": True,
        "manager": "maria.gomez",
        "groups": [],
        "vip": False,
        "mfa_enrolled": True,
        "identity_verified": True,
        "password_last_set_days": 5,
        "email": "locked.admin@acme.com",
        "display_name": "Locked Admin",
        "department": "IT",
    },
}

GROUPS_JSON = {
    "sap-users": {
        "display_name": "SAP Users",
        "members": ["juan.perez", "maria.gomez"],
        "sensitive": False,
        "owner": "SAP Basis",
        "requires_approval": False,
    },
    "finance-folder": {
        "display_name": "Finance Folder",
        "members": [],
        "sensitive": True,
        "owner": "Finance",
        "requires_approval": True,
    },
}

ACCESS_POLICIES_JSON = {
    "sap-fiori-basic": {
        "description": "Basic SAP Fiori access",
        "risk": "low",
        "action": "grant_role",
        "approver_role": "support_lead",
        "requires_approval": False,
        "sensitive": False,
        "allowed_groups": ["sap-users"],
        "allowed_roles": [],
    },
    "finance-folder": {
        "description": "Shared finance folder",
        "risk": "high",
        "action": "grant_folder_access",
        "approver_role": "manager",
        "requires_approval": True,
        "sensitive": True,
        "allowed_groups": ["finance-folder"],
        "allowed_roles": [],
    },
}


class TemporaryDataMixin(unittest.TestCase):
    """Create the mock data files needed by the tests in a temp directory."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.tickets_path = self.tmp_path / "tickets.csv"
        self.tickets_path.write_text(TICKETS_CSV, encoding="utf-8")
        self.users_path = self.tmp_path / "users.json"
        self.users_path.write_text(json.dumps(USERS_JSON), encoding="utf-8")
        self.groups_path = self.tmp_path / "groups.json"
        self.groups_path.write_text(json.dumps(GROUPS_JSON), encoding="utf-8")
        self.policies_path = self.tmp_path / "access_policies.json"
        self.policies_path.write_text(json.dumps(ACCESS_POLICIES_JSON), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def identity(self) -> MockIdentity:
        """Build a mock identity adapter over the temporary data files."""
        return MockIdentity(
            users_path=str(self.users_path),
            groups_path=str(self.groups_path),
            access_policies_path=str(self.policies_path),
        )


class TestBaseHelpers(unittest.TestCase):
    """Tests of the shared helpers of :mod:`adapters.base`."""

    def test_utc_now_iso_has_z_suffix_without_microseconds(self) -> None:
        stamp = utc_now_iso()
        self.assertTrue(stamp.endswith("Z"))
        self.assertNotIn("+00:00", stamp)
        self.assertNotIn(".", stamp)

    def test_load_dotenv_parses_quotes_comments_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text(
                "# a comment\n"
                "\n"
                "PLAIN=value\n"
                'DOUBLE="quoted value"\n'
                "SINGLE='single'\n"
                "export EXPORTED=exported\n"
                "EMPTY=\n"
                "NOEQUALS\n",
                encoding="utf-8",
            )
            loaded = load_dotenv(str(env_file), override=True)
            self.assertEqual(loaded["PLAIN"], "value")
            self.assertEqual(loaded["DOUBLE"], "quoted value")
            self.assertEqual(loaded["SINGLE"], "single")
            self.assertEqual(loaded["EXPORTED"], "exported")
            self.assertNotIn("EMPTY", loaded)
            self.assertNotIn("NOEQUALS", loaded)
            self.assertEqual(os.environ["PLAIN"], "value")

    def test_load_dotenv_does_not_override_existing_variables(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text("HELPDESK_TEST_KEEP=from_file\n", encoding="utf-8")
            os.environ["HELPDESK_TEST_KEEP"] = "from_env"
            try:
                load_dotenv(str(env_file))
                self.assertEqual(os.environ["HELPDESK_TEST_KEEP"], "from_env")
            finally:
                os.environ.pop("HELPDESK_TEST_KEEP", None)

    def test_sanitize_secrets_redacts_credentials(self) -> None:
        cleaned = sanitize_secrets(
            {
                "Authorization": "Bearer super-secret",
                "password": "hunter2",
                "nested": {"client_secret": "s3cr3t", "safe": "value"},
                "list": [{"access_token": "abc"}],
            }
        )
        self.assertEqual(cleaned["Authorization"], "***")
        self.assertEqual(cleaned["password"], "***")
        self.assertEqual(cleaned["nested"]["client_secret"], "***")
        self.assertEqual(cleaned["nested"]["safe"], "value")
        self.assertEqual(cleaned["list"][0]["access_token"], "***")

    def test_require_requests_is_lazy_and_actionable(self) -> None:
        try:
            import requests  # noqa: F401
        except ImportError:
            with self.assertRaises(AdapterConfigError) as context:
                require_requests("ServiceNowITSM")
            message = str(context.exception)
            self.assertIn("requests", message)
            self.assertIn("pip install requests", message)
            self.assertIn("ServiceNowITSM", message)
        else:  # pragma: no cover - depends on the machine
            self.assertIsNotNone(require_requests())


class TestMockITSM(TemporaryDataMixin):
    """Tests of the in-memory ITSM connector."""

    def test_reads_the_csv_instead_of_hardcoded_tickets(self) -> None:
        adapter = MockITSM(data_path=str(self.tickets_path))
        ticket = adapter.get_ticket("TCK-001")
        self.assertEqual(ticket["ticket_id"], "TCK-001")
        self.assertEqual(ticket["requester"], "juan.perez")
        self.assertEqual(ticket["service"], "SAP Fiori")
        self.assertEqual(ticket["source"], "mock")
        self.assertIn("SAP Fiori", ticket["description"])

    def test_unknown_ticket_raises_not_found(self) -> None:
        adapter = MockITSM(data_path=str(self.tickets_path))
        with self.assertRaises(AdapterNotFoundError):
            adapter.get_ticket("TCK-999")

    def test_missing_file_raises_config_error(self) -> None:
        adapter = MockITSM(data_path=str(self.tmp_path / "missing.csv"))
        with self.assertRaises(AdapterConfigError):
            adapter.get_ticket("TCK-001")

    def test_list_tickets_supports_limit_offset_and_query(self) -> None:
        adapter = MockITSM(data_path=str(self.tickets_path))
        self.assertEqual(len(adapter.list_tickets()), 2)
        self.assertEqual(len(adapter.list_tickets(limit=1)), 1)
        self.assertEqual(adapter.list_tickets(offset=1)[0]["ticket_id"], "TCK-002")
        filtered = adapter.list_tickets(query="wifi")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["ticket_id"], "TCK-002")

    def test_write_operations_update_the_in_memory_state(self) -> None:
        adapter = MockITSM(data_path=str(self.tickets_path))
        comment = adapter.add_comment("TCK-001", "KBA-001 enviado")
        self.assertEqual(comment["status"], "comment_added")
        self.assertEqual(comment["ticket_id"], "TCK-001")
        self.assertIn("timestamp", comment)

        assigned = adapter.assign("TCK-001", "support_lead")
        self.assertEqual(assigned["status"], "assigned")
        self.assertEqual(assigned["queue"], "support_lead")

        priority = adapter.set_priority("TCK-001", "P2")
        self.assertEqual(priority["status"], "priority_updated")
        self.assertEqual(priority["priority"], "P2")

        resolved = adapter.resolve("TCK-001", "Contraseña restablecida")
        self.assertEqual(resolved["status"], "resolved")

        ticket = adapter.get_ticket("TCK-001")
        self.assertEqual(ticket["priority"], "P2")
        self.assertEqual(ticket["status"], "resolved")


class TestMockIdentity(TemporaryDataMixin):
    """Tests of the identity mock and of its security guardrails."""

    def test_get_user_returns_the_canonical_shape(self) -> None:
        user = self.identity().get_user("juan.perez")
        for key in (
            "username",
            "email",
            "display_name",
            "department",
            "active",
            "locked",
            "locked_by_admin",
            "groups",
            "manager",
            "mfa_enrolled",
            "password_last_set_days",
            "identity_verified",
            "source",
            "raw",
        ):
            self.assertIn(key, user)
        self.assertEqual(user["username"], "juan.perez")
        self.assertTrue(user["active"])
        self.assertFalse(user["vip"])

    def test_unknown_user_raises_not_found(self) -> None:
        with self.assertRaises(AdapterNotFoundError):
            self.identity().get_user("ghost.user")

    def test_groups_is_active_and_manager(self) -> None:
        identity = self.identity()
        self.assertEqual(identity.get_groups("juan.perez"), ["sap-users"])
        self.assertTrue(identity.is_active("juan.perez"))
        self.assertEqual(identity.get_manager("juan.perez"), "maria.gomez")
        self.assertIsNone(identity.get_manager("maria.gomez"))

    def test_reset_password_requires_verified_identity(self) -> None:
        identity = self.identity()
        with self.assertRaises(VerificationRequired):
            identity.reset_password("juan.perez", verified=False)
        result = identity.reset_password("juan.perez", verified=True, verification_method="otp")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["user"], "juan.perez")
        self.assertEqual(len(result["action_id"]), 12)

    def test_reset_password_requires_mfa(self) -> None:
        with self.assertRaises(VerificationRequired):
            self.identity().reset_password("carlos.ruiz", verified=True)

    def test_reset_password_is_idempotent(self) -> None:
        identity = self.identity()
        first = identity.reset_password("juan.perez", verified=True)
        second = identity.reset_password("juan.perez", verified=True)
        self.assertEqual(first["action_id"], second["action_id"])

    def test_unlock_account_requires_verification(self) -> None:
        identity = self.identity()
        with self.assertRaises(VerificationRequired):
            identity.unlock_account("carlos.ruiz", verified=False)
        result = identity.unlock_account("carlos.ruiz", verified=True)
        self.assertEqual(result["status"], "success")

    def test_unlock_blocked_when_locked_by_admin(self) -> None:
        with self.assertRaises(VerificationRequired) as context:
            self.identity().unlock_account("locked.admin", verified=True)
        self.assertIn("administrator", str(context.exception))

    def test_request_access_enforces_the_allow_list(self) -> None:
        identity = self.identity()
        request = identity.request_access("juan.perez", "finance-folder", justification="project")
        self.assertEqual(request["status"], "pending_approval")
        self.assertEqual(request["resource"], "finance-folder")
        with self.assertRaises(AdapterConfigError) as context:
            identity.request_access("juan.perez", "root-folder")
        self.assertIn("finance-folder", str(context.exception))

    def test_request_access_fails_closed_without_policies(self) -> None:
        identity = MockIdentity(
            users_path=str(self.users_path),
            groups_path=str(self.groups_path),
            access_policies_path=str(self.tmp_path / "absent.json"),
        )
        with self.assertRaises(AdapterConfigError):
            identity.request_access("juan.perez", "finance-folder")


class TestMockApps(unittest.TestCase):
    """Tests of the mocked business applications."""

    def test_check_status_shape(self) -> None:
        status = MockSAPFioriApp().check_status("SAP Fiori")
        self.assertEqual(status["status"], "operational")
        self.assertEqual(status["app"], "SAP Fiori")
        self.assertIn("checked_at", status)
        self.assertIn("details", status)
        self.assertIn("incident_ref", status)

    def test_execute_action_is_simulated_when_writes_are_disabled(self) -> None:
        previous = os.environ.pop("HELPDESK_ALLOW_WRITES", None)
        try:
            result = MockApps().execute_action(
                {"action": "restart_service", "target": "vpn", "user": "juan.perez"}
            )
            self.assertEqual(result["status"], "simulated")
            self.assertTrue(result["dry_run"])
        finally:
            if previous is not None:
                os.environ["HELPDESK_ALLOW_WRITES"] = previous

    def test_execute_action_succeeds_in_write_mode(self) -> None:
        os.environ["HELPDESK_ALLOW_WRITES"] = "1"
        try:
            result = MockApps().execute_action(
                {"action": "restart_service", "target": "vpn", "user": "juan.perez"}
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertFalse(result["dry_run"])
            self.assertEqual(len(result["action_id"]), 12)
        finally:
            os.environ.pop("HELPDESK_ALLOW_WRITES", None)

    def test_unknown_action_is_skipped(self) -> None:
        os.environ["HELPDESK_ALLOW_WRITES"] = "1"
        try:
            result = MockApps().execute_action({"action": "drop_database", "user": "x"})
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "action_not_allowed")
        finally:
            os.environ.pop("HELPDESK_ALLOW_WRITES", None)

    def test_list_resources_of_each_service(self) -> None:
        for adapter in (MockSAPFioriApp(), MockVPNApp(), MockSharedDriveApp()):
            resources = adapter.list_resources()
            self.assertTrue(resources)
            for resource in resources:
                for key in ("resource", "type", "sensitive", "owner"):
                    self.assertIn(key, resource)

    def test_service_names(self) -> None:
        self.assertEqual(MockSAPFioriApp().service, "SAP Fiori")
        self.assertEqual(MockVPNApp().service, "VPN")
        self.assertEqual(MockSharedDriveApp().service, "Shared Drive")


class TestFactories(unittest.TestCase):
    """Tests of the public factory API (SPEC section 3.2.1)."""

    def setUp(self) -> None:
        self._env_backup = {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("HELPDESK_", "SERVICENOW_", "JIRA_", "ENTRA_", "OKTA_", "GRAPH_"))
        }
        for key in self._env_backup:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith(("HELPDESK_", "SERVICENOW_", "JIRA_", "ENTRA_", "OKTA_", "GRAPH_")):
                os.environ.pop(key, None)
        os.environ.update(self._env_backup)

    def test_kind_constants(self) -> None:
        self.assertEqual(ITSM_KINDS, ("mock", "servicenow", "jira"))
        self.assertEqual(IDENTITY_KINDS, ("mock", "entra", "okta"))
        self.assertEqual(APP_KINDS, ("mock", "sap_fiori", "vpn", "shared_drive"))

    def test_default_is_mock(self) -> None:
        self.assertIsInstance(build_itsm_adapter(), MockITSM)
        self.assertIsInstance(build_identity_adapter(), MockIdentity)
        self.assertIsInstance(build_app_adapter(), MockApps)

    def test_environment_variable_selects_the_backend(self) -> None:
        os.environ["HELPDESK_APP_ADAPTER"] = "vpn"
        adapter = build_app_adapter()
        self.assertIsInstance(adapter, MockVPNApp)
        self.assertEqual(adapter.service, "VPN")

    def test_explicit_kind_wins_over_environment(self) -> None:
        os.environ["HELPDESK_APP_ADAPTER"] = "vpn"
        adapter = build_app_adapter("shared_drive")
        self.assertIsInstance(adapter, MockSharedDriveApp)

    def test_unknown_kind_raises_config_error(self) -> None:
        with self.assertRaises(AdapterConfigError) as context:
            build_itsm_adapter("not-a-backend")
        message = str(context.exception)
        self.assertIn("Unknown ITSM adapter", message)
        self.assertIn("mock, servicenow, jira", message)

    def test_real_backend_without_credentials_fails_fast(self) -> None:
        with self.assertRaises(AdapterConfigError):
            build_itsm_adapter("servicenow")
        with self.assertRaises(AdapterConfigError):
            build_itsm_adapter("jira")
        with self.assertRaises(AdapterConfigError):
            build_identity_adapter("entra")
        with self.assertRaises(AdapterConfigError):
            build_identity_adapter("okta")

    def test_real_backend_builds_when_credentials_are_present(self) -> None:
        os.environ["SERVICENOW_INSTANCE"] = "acme"
        os.environ["SERVICENOW_ACCESS_TOKEN"] = "token"
        adapter = build_itsm_adapter("servicenow")
        self.assertIsInstance(adapter, ServiceNowITSM)
        self.assertEqual(adapter.base_url, "https://acme.service-now.com")

        os.environ["JIRA_URL"] = "https://acme.atlassian.net"
        os.environ["JIRA_EMAIL"] = "bot@acme.com"
        os.environ["JIRA_API_TOKEN"] = "token"
        jira = build_itsm_adapter("jira")
        self.assertIsInstance(jira, JiraITSM)
        self.assertEqual(jira.url, "https://acme.atlassian.net")

    def test_write_kill_switch_blocks_the_live_connectors(self) -> None:
        os.environ["SERVICENOW_INSTANCE"] = "acme"
        os.environ["SERVICENOW_ACCESS_TOKEN"] = "token"
        adapter = build_itsm_adapter("servicenow")
        self.assertFalse(adapter.write_enabled())
        result = adapter.add_comment("TCK-001", "hello")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "writes_disabled")

        os.environ["HELPDESK_ALLOW_WRITES"] = "1"
        try:
            self.assertTrue(adapter.write_enabled())
        finally:
            os.environ.pop("HELPDESK_ALLOW_WRITES", None)


if __name__ == "__main__":  # pragma: no cover - script entry point
    unittest.main()
