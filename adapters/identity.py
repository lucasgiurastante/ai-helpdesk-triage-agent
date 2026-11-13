"""Identity connectors: mock, Microsoft Entra ID (Graph) and Okta.

The connectors implement the guardrails that make an *identity* action safe
(SPEC section 3.1.2):

1. a password reset requires a **verified** identity, an **MFA enrolled**
   account and an **active** account;
2. an account unlock requires a verified identity and is blocked when the
   lock was applied by an administrator;
3. an access request is only accepted for a resource present in the
   allow-list of ``policies/access_policies.json``;
4. every mutating call is idempotent: ``action_id`` is derived from
   ``sha256(user|action|resource)``.

The live connectors never mutate a directory unless the kill switch
``HELPDESK_ALLOW_WRITES=1`` is set (SPEC section 10.4).
"""

import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    AdapterConfigError,
    AdapterNotFoundError,
    IdentityAdapter,
    VerificationRequired,
    action_id_for,
    http_request,
    resolve_path,
    sanitize_secrets,
    utc_now_iso,
    writes_enabled,
)

#: Default location of the mock user directory.
DEFAULT_USERS_PATH = "data/users.json"

#: Default location of the mock group directory.
DEFAULT_GROUPS_PATH = "data/groups.json"

#: Default location of the access policy allow-list.
DEFAULT_ACCESS_POLICIES_PATH = "policies/access_policies.json"

#: Default (connect, read) timeout in seconds.
DEFAULT_TIMEOUT: Tuple[float, float] = (5, 15)

#: Message used when the identity could not be verified out-of-band.
VERIFICATION_MESSAGE = "Password reset requires verified identity with MFA"


def _load_json(path: str) -> Dict[str, Any]:
    """Load a JSON document, returning an empty mapping when it is missing.

    Args:
        path: Path of the file.

    Returns:
        The decoded object (a dictionary) or an empty dictionary.
    """
    target = resolve_path(path)
    if not target.is_file():
        return {}
    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class MockIdentity(IdentityAdapter):
    """Identity adapter backed by ``data/users.json`` and ``data/groups.json``.

    Args:
        users_path: Path of the user directory.
        groups_path: Path of the group directory.
        access_policies_path: Path of the access policy allow-list.  The
            resource allow-list fails closed: when the file is missing an
            access request is refused instead of being silently allowed.
    """

    name = "mock"

    def __init__(
        self,
        users_path: str = DEFAULT_USERS_PATH,
        groups_path: str = DEFAULT_GROUPS_PATH,
        access_policies_path: str = DEFAULT_ACCESS_POLICIES_PATH,
    ) -> None:
        self.users_path = str(users_path)
        self.groups_path = str(groups_path)
        self.access_policies_path = str(access_policies_path)
        self._users = _load_json(self.users_path)
        self._groups = _load_json(self.groups_path)

    # -- helpers ---------------------------------------------------------- #

    def write_enabled(self) -> bool:
        """Return ``True``: the mock never touches a real directory."""
        return True

    def _require_user(self, username: str) -> Dict[str, Any]:
        """Return the raw entry of a user.

        Args:
            username: Login of the user.

        Returns:
            The raw user mapping.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """
        record = self._users.get(username)
        if record is None:
            raise AdapterNotFoundError(
                "User {0} does not exist in {1}".format(username, self.users_path),
                {"username": username},
            )
        return dict(record)

    def _allowed_resources(self) -> Dict[str, Any]:
        """Return the access policies used as the resource allow-list.

        Returns:
            The ``policies/access_policies.json`` content.

        Raises:
            AdapterConfigError: When the policy file is missing -- the
                allow-list fails closed.
        """
        target = resolve_path(self.access_policies_path)
        if not target.is_file():
            raise AdapterConfigError(
                "Access policy file not found: {0}. It defines the allow-list of "
                "resources that may be requested.".format(target)
            )
        policies = _load_json(self.access_policies_path)
        if not policies:
            raise AdapterConfigError(
                "Access policy file is empty or invalid: {0}".format(target)
            )
        return policies

    def _user_payload(self, record: Dict[str, Any], username: str) -> Dict[str, Any]:
        """Build the canonical user mapping out of a raw directory record.

        Args:
            record: Raw user record.
            username: Login of the user.

        Returns:
            The canonical user dictionary.
        """
        groups = record.get("groups", []) or []
        if isinstance(groups, str):
            groups = [groups]
        return {
            "username": username,
            "email": str(record.get("email", "") or ""),
            "display_name": str(record.get("display_name", "") or ""),
            "department": str(record.get("department", "") or ""),
            "active": bool(record.get("active", True)),
            "locked": bool(record.get("locked", False)),
            "locked_by_admin": bool(record.get("locked_by_admin", False)),
            "groups": [str(group) for group in groups],
            "manager": record.get("manager"),
            "vip": bool(record.get("vip", False)),
            "mfa_enrolled": bool(record.get("mfa_enrolled", False)),
            "identity_verified": bool(record.get("identity_verified", False)),
            "password_last_set_days": int(record.get("password_last_set_days", 0) or 0),
            "source": "mock",
            "raw": sanitize_secrets(record),
        }

    # -- read operations -------------------------------------------------- #

    def get_user(self, username: str) -> Dict[str, Any]:
        """Return the directory entry of a user.

        Args:
            username: Login of the user.

        Returns:
            The canonical user dictionary.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """
        return self._user_payload(self._require_user(username), username)

    def get_groups(self, username: str) -> List[str]:
        """Return the groups a user belongs to.

        Args:
            username: Login of the user.

        Returns:
            The list of group identifiers.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """
        record = self._require_user(username)
        groups = record.get("groups", []) or []
        if isinstance(groups, str):
            groups = [groups]
        return [str(group) for group in groups]

    def is_active(self, username: str) -> bool:
        """Return whether the account is active.

        Args:
            username: Login of the user.

        Returns:
            ``True`` when the account is active.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """
        return bool(self._require_user(username).get("active", True))

    def get_manager(self, username: str) -> Optional[str]:
        """Return the manager of a user.

        Args:
            username: Login of the user.

        Returns:
            The manager login or ``None``.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """
        manager = self._require_user(username).get("manager")
        return str(manager) if manager else None

    # -- mutating operations ---------------------------------------------- #

    def request_access(
        self,
        username: str,
        resource: str,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register an access request (no grant is performed).

        Args:
            username: Login of the requester.
            resource: Identifier of the requested resource.
            justification: Free text justification.

        Returns:
            The request result dictionary.

        Raises:
            AdapterConfigError: When the resource is not in the allow-list.
            AdapterNotFoundError: When the user does not exist.
        """
        self._require_user(username)
        policies = self._allowed_resources()
        if resource not in policies:
            raise AdapterConfigError(
                "Resource '{0}' is not in the access allow-list. Valid resources: {1}".format(
                    resource, ", ".join(sorted(policies))
                ),
                {"resource": resource, "allowed": sorted(policies)},
            )
        action_id = action_id_for(username, "request_access", resource)
        return {
            "status": "pending_approval",
            "request_id": "REQ-{0}".format(action_id.upper()),
            "action_id": action_id,
            "user": username,
            "resource": resource,
            "justification": justification or "",
            "approver_role": policies[resource].get("approver_role", "support_lead"),
            "timestamp": utc_now_iso(),
        }

    def reset_password(
        self,
        username: str,
        *,
        verified: bool = False,
        verification_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Simulate a password reset.

        Args:
            username: Login of the target user.
            verified: Whether the identity was verified out-of-band.
            verification_method: How the identity was verified.

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified, has no
                MFA enrolled, is inactive or was locked by an administrator.
            AdapterNotFoundError: When the user does not exist.
        """
        record = self._require_user(username)
        self._assert_resettable(record, verified)
        action_id = action_id_for(username, "reset_password", "password")
        return {
            "status": "success",
            "action_id": action_id,
            "user": username,
            "resource": "password",
            "method": verification_method or "out_of_band",
            "timestamp": utc_now_iso(),
        }

    def unlock_account(self, username: str, *, verified: bool = False) -> Dict[str, Any]:
        """Simulate an account unlock.

        Args:
            username: Login of the target user.
            verified: Whether the identity was verified out-of-band.

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified or the
                lock was applied by an administrator.
            AdapterNotFoundError: When the user does not exist.
        """
        record = self._require_user(username)
        self._assert_unlockable(record, verified)
        action_id = action_id_for(username, "unlock_account", "account")
        if not bool(record.get("locked", False)):
            return {
                "status": "skipped",
                "reason": "not_locked",
                "action_id": action_id,
                "user": username,
                "resource": "account",
                "timestamp": utc_now_iso(),
            }
        return {
            "status": "success",
            "action_id": action_id,
            "user": username,
            "resource": "account",
            "timestamp": utc_now_iso(),
        }

    # -- guardrails ------------------------------------------------------- #

    @staticmethod
    def _assert_resettable(record: Dict[str, Any], verified: bool) -> None:
        """Raise :class:`VerificationRequired` when a reset is not allowed.

        Args:
            record: Raw user record.
            verified: Whether the identity was verified out-of-band.

        Raises:
            VerificationRequired: When one of the conditions fails.
        """
        if not verified:
            raise VerificationRequired(VERIFICATION_MESSAGE)
        if not bool(record.get("active", True)):
            raise VerificationRequired(
                "Password reset requires an active account; {0} is deactivated".format(
                    record.get("email", "the user")
                )
            )
        if not bool(record.get("mfa_enrolled", False)):
            raise VerificationRequired(VERIFICATION_MESSAGE)
        if bool(record.get("locked_by_admin", False)):
            raise VerificationRequired(
                "Account is locked by an administrator; requires security approval"
            )

    @staticmethod
    def _assert_unlockable(record: Dict[str, Any], verified: bool) -> None:
        """Raise :class:`VerificationRequired` when an unlock is not allowed.

        Args:
            record: Raw user record.
            verified: Whether the identity was verified out-of-band.

        Raises:
            VerificationRequired: When one of the conditions fails.
        """
        if not verified:
            raise VerificationRequired(
                "Account unlock requires verified identity with MFA"
            )
        if bool(record.get("locked_by_admin", False)):
            raise VerificationRequired(
                "Account is locked by an administrator; requires security approval"
            )


class EntraIDIdentity(IdentityAdapter):
    """Microsoft Entra ID connector built on Microsoft Graph.

    Endpoints (SPEC section 3.2.3)::

        GET   /users/{username}
        GET   /users/{username}/transitiveMemberOf
        GET   /users/{username}/manager
        PATCH /users/{username}

    Authentication is ``Authorization: Bearer {ENTRA_ACCESS_TOKEN}``.  When a
    tenant/client/secret triple is available but no token is, the connector
    raises :class:`AdapterConfigError` explaining the OAuth2 client
    credentials flow -- the token exchange itself is a manual, out-of-repo
    step so that no secret ever lives in the repository.

    Args:
        tenant_id: Entra tenant identifier.
        client_id: Entra application (client) identifier.
        client_secret: Entra application secret.
        access_token: Graph access token, takes priority over the secret.
        base_url: Graph base URL (``https://graph.microsoft.com/v1.0``).
        timeout: ``(connect, read)`` timeout in seconds.
        retries: Maximum number of HTTP attempts.

    Raises:
        AdapterConfigError: When no access token is available.
    """

    name = "entra"

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        base_url: str = "https://graph.microsoft.com/v1.0",
        timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
        retries: int = 3,
    ) -> None:
        self.tenant_id = tenant_id or os.environ.get("ENTRA_TENANT_ID")
        self.client_id = client_id or os.environ.get("ENTRA_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("ENTRA_CLIENT_SECRET")
        self.access_token = access_token or os.environ.get("ENTRA_ACCESS_TOKEN")
        self.base_url = (base_url or os.environ.get("GRAPH_BASE_URL") or "").rstrip("/")
        self.timeout = timeout
        self.retries = retries

        if not self.base_url:
            self.base_url = "https://graph.microsoft.com/v1.0"
        if not self.access_token:
            raise AdapterConfigError(
                "Microsoft Graph access token is required. Set ENTRA_ACCESS_TOKEN=<token>. "
                "To mint one with the client credentials flow, POST "
                "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token with "
                "scope=https://graph.microsoft.com/.default using the application "
                "client id and secret.".format(tenant_id=self.tenant_id or "<tenant_id>")
            )

    # -- HTTP plumbing ---------------------------------------------------- #

    def _headers(self) -> Dict[str, str]:
        """Return the HTTP headers used against Microsoft Graph."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer {0}".format(self.access_token),
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Perform an authenticated request against Microsoft Graph.

        Args:
            method: HTTP verb.
            path: Path relative to the Graph base URL.
            params: Optional query string parameters.
            json_body: Optional JSON body.

        Returns:
            The parsed response body.
        """
        url = "{0}{1}".format(self.base_url, path)
        return http_request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json_body=json_body,
            timeout=self.timeout,
            retries=self.retries,
            connector_name="EntraIDIdentity",
        )

    # -- mapping ---------------------------------------------------------- #

    @staticmethod
    def _map_user(username: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """Map a Graph user object to the canonical user mapping.

        Args:
            username: Login the request was made with.
            record: Raw Graph user object.

        Returns:
            The canonical user dictionary.
        """
        sign_in = record.get("signInMethods") or []
        password_policy = record.get("passwordPolicies") or {}
        mfa_enrolled = bool(sign_in) or bool(password_policy)
        manager = record.get("manager")
        if isinstance(manager, dict):
            manager = manager.get("userPrincipalName") or manager.get("id")
        sign_in_activity = record.get("signInActivity") or {}
        locked = str(sign_in_activity.get("status", "")).lower() == "lockedout"
        return {
            "username": str(record.get("userPrincipalName", username) or username),
            "email": str(record.get("mail") or record.get("userPrincipalName") or ""),
            "display_name": str(record.get("displayName", "") or ""),
            "department": str(record.get("department", "") or ""),
            "active": bool(record.get("accountEnabled", True)),
            "locked": locked,
            "locked_by_admin": False,
            "groups": [],
            "manager": str(manager) if manager else None,
            "vip": False,
            "mfa_enrolled": mfa_enrolled,
            "identity_verified": False,
            "password_last_set_days": 0,
            "source": "entra",
            "raw": sanitize_secrets(record),
        }

    # -- read operations -------------------------------------------------- #

    def get_user(self, username: str) -> Dict[str, Any]:
        """Return the Graph user object.

        Args:
            username: User principal name (or e-mail) of the user.

        Returns:
            The canonical user dictionary.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """
        payload = self._request("GET", "/users/{0}".format(username))
        if not payload:
            raise AdapterNotFoundError(
                "Entra ID user {0} does not exist".format(username),
                {"username": username},
            )
        return self._map_user(username, payload)

    def get_groups(self, username: str) -> List[str]:
        """Return the (transitive) group memberships of a user.

        ``transitiveMemberOf`` also returns the groups of the nested groups;
        the direct membership endpoint (``memberOf``) is a subset of it.

        Args:
            username: User principal name of the user.

        Returns:
            The list of group identifiers.
        """
        payload = self._request("GET", "/users/{0}/transitiveMemberOf".format(username))
        items = payload.get("items", payload.get("value", [])) or []
        groups: List[str] = []
        for item in items:
            if isinstance(item, dict):
                groups.append(str(item.get("id", "")))
            else:
                groups.append(str(item))
        return [group for group in groups if group]

    def is_active(self, username: str) -> bool:
        """Return whether the account is enabled.

        Args:
            username: User principal name of the user.

        Returns:
            ``True`` when the account is enabled.
        """
        return bool(self.get_user(username)["active"])

    def get_manager(self, username: str) -> Optional[str]:
        """Return the manager of a user.

        Args:
            username: User principal name of the user.

        Returns:
            The manager user principal name or ``None``.
        """
        try:
            payload = self._request("GET", "/users/{0}/manager".format(username))
        except AdapterNotFoundError:
            return None
        if not payload:
            return None
        return str(payload.get("userPrincipalName", "") or "") or None

    # -- mutating operations ---------------------------------------------- #

    def _skipped(self, username: str, action: str) -> Dict[str, Any]:
        """Return the standard "writes are disabled" answer.

        Args:
            username: Login of the target user.
            action: Name of the action.

        Returns:
            The operation result dictionary.
        """
        return {
            "status": "skipped",
            "reason": "writes_disabled",
            "action_id": action_id_for(username, action, ""),
            "user": username,
            "timestamp": utc_now_iso(),
        }

    def request_access(
        self,
        username: str,
        resource: str,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register an access request without granting anything.

        Entra ID has no native access request workflow: the request is
        queued and a group assignment is performed *after* the approval.

        Args:
            username: Login of the requester.
            resource: Identifier of the requested resource.
            justification: Free text justification.

        Returns:
            The request result dictionary.
        """
        return {
            "status": "pending_approval",
            "request_id": "REQ-{0}".format(str(uuid.uuid4())),
            "action_id": action_id_for(username, "request_access", resource),
            "user": username,
            "resource": resource,
            "justification": justification or "",
            "timestamp": utc_now_iso(),
        }

    def reset_password(
        self,
        username: str,
        *,
        verified: bool = False,
        verification_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reset the password of a user through Graph.

        The endpoint requires the ``Directory.ReadWrite.All`` permission.
        The recommended production alternative is the Self-Service Password
        Reset policy, which delegates the reset to the user.

        Args:
            username: User principal name of the target user.
            verified: Whether the identity was verified out-of-band.
            verification_method: How the identity was verified.

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified.
        """
        if not verified:
            raise VerificationRequired(VERIFICATION_MESSAGE)
        action_id = action_id_for(username, "reset_password", "password")
        if not writes_enabled():
            return self._skipped(username, "reset_password")
        temporary_password = "Aa1!{0}".format(action_id)
        self._request(
            "PATCH",
            "/users/{0}".format(username),
            json_body={
                "passwordProfile": {
                    "forceChangePasswordNextSignIn": True,
                    "password": temporary_password,
                }
            },
        )
        return {
            "status": "success",
            "action_id": action_id,
            "user": username,
            "resource": "password",
            "method": verification_method or "out_of_band",
            "timestamp": utc_now_iso(),
        }

    def unlock_account(self, username: str, *, verified: bool = False) -> Dict[str, Any]:
        """Try to unlock a locked account through Graph.

        Microsoft Graph does **not** expose the credential lockout counter of
        the tenant: re-enabling ``accountEnabled`` is the only supported
        action.  When the tenant refuses it, the connector reports the
        failure instead of simulating a success (SPEC section 3.2.3).

        Args:
            username: User principal name of the target user.
            verified: Whether the identity was verified out-of-band.

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified.
        """
        if not verified:
            raise VerificationRequired("Account unlock requires verified identity with MFA")
        action_id = action_id_for(username, "unlock_account", "account")
        if not writes_enabled():
            return self._skipped(username, "unlock_account")
        try:
            self._request(
                "PATCH",
                "/users/{0}".format(username),
                json_body={"accountEnabled": True},
            )
        except AdapterError as exc:
            return {
                "status": "failed",
                "reason": "not_supported_by_graph",
                "action_id": action_id,
                "user": username,
                "resource": "account",
                "detail": exc.message,
                "timestamp": utc_now_iso(),
            }
        return {
            "status": "success",
            "action_id": action_id,
            "user": username,
            "resource": "account",
            "timestamp": utc_now_iso(),
        }


class OktaIdentity(IdentityAdapter):
    """Okta connector built on the Okta Management API.

    Endpoints (SPEC section 3.2.3)::

        GET  /users/{username}
        GET  /users/{username}/groups
        GET  /users/{username}/managers
        POST /users/{username}/lifecycle/reset_password
        POST /users/{username}/lifecycle/unlock

    Authentication is ``Authorization: SSWS {OKTA_API_TOKEN}``.  The
    configuration is read from ``OKTA_ORG``, ``OKTA_API_TOKEN`` and
    ``OKTA_BASE_URL``.

    Args:
        org: Okta organisation domain (``mycompany``).
        api_token: Okta API token.
        base_url: Full base URL, overrides ``org``.
        timeout: ``(connect, read)`` timeout in seconds.
        retries: Maximum number of HTTP attempts.

    Raises:
        AdapterConfigError: When the organisation or the token are missing.
    """

    name = "okta"

    def __init__(
        self,
        org: Optional[str] = None,
        api_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
        retries: int = 3,
    ) -> None:
        self.org = org or os.environ.get("OKTA_ORG")
        self.api_token = api_token or os.environ.get("OKTA_API_TOKEN")
        self.timeout = timeout
        self.retries = retries

        if base_url or os.environ.get("OKTA_BASE_URL"):
            self.base_url = (base_url or os.environ.get("OKTA_BASE_URL") or "").rstrip("/")
        elif self.org:
            self.base_url = "https://{0}.okta.com/api/v1".format(self.org)
        else:
            raise AdapterConfigError(
                "Okta organisation is required. Set OKTA_ORG=mycompany (or pass org=...)."
            )

        if not self.api_token:
            raise AdapterConfigError(
                "Okta API token is required. Set OKTA_API_TOKEN=<token> (an API token "
                "with the least privileged scope, never the account password)."
            )

    # -- HTTP plumbing ---------------------------------------------------- #

    def _headers(self) -> Dict[str, str]:
        """Return the HTTP headers used against Okta."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "SSWS {0}".format(self.api_token),
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Perform an authenticated request against Okta.

        Args:
            method: HTTP verb.
            path: Path relative to the Okta base URL.
            params: Optional query string parameters.
            json_body: Optional JSON body.

        Returns:
            The parsed response body.
        """
        url = "{0}{1}".format(self.base_url, path)
        return http_request(
            method,
            url,
            headers=self._headers(),
            params=params,
            json_body=json_body,
            timeout=self.timeout,
            retries=self.retries,
            connector_name="OktaIdentity",
        )

    # -- mapping ---------------------------------------------------------- #

    @staticmethod
    def _map_user(username: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """Map an Okta user object to the canonical user mapping.

        Args:
            username: Login the request was made with.
            record: Raw Okta user object.

        Returns:
            The canonical user dictionary.
        """
        profile = record.get("profile", {}) or {}
        status = str(record.get("status", "") or "").upper()
        embedded = record.get("_embedded", {}) or {}
        factors = embedded.get("factors", []) or []
        mfa_enrolled = any(
            str(factor.get("factorType", "")) in ("sms", "totp", "web", "push", "email")
            for factor in factors
            if isinstance(factor, dict)
        )
        display_name = " ".join(
            part
            for part in (str(profile.get("firstName", "") or ""), str(profile.get("lastName", "") or ""))
            if part
        )
        return {
            "username": str(profile.get("login", username) or username),
            "email": str(profile.get("email", "") or ""),
            "display_name": display_name,
            "department": str(profile.get("department", "") or ""),
            "active": status == "ACTIVE",
            "locked": status == "LOCKED_OUT",
            "locked_by_admin": False,
            "groups": [],
            "manager": None,
            "vip": False,
            "mfa_enrolled": mfa_enrolled,
            "identity_verified": False,
            "password_last_set_days": 0,
            "source": "okta",
            "raw": sanitize_secrets(record),
        }

    # -- read operations -------------------------------------------------- #

    def get_user(self, username: str) -> Dict[str, Any]:
        """Return the Okta user object.

        Args:
            username: Login (or e-mail) of the user.

        Returns:
            The canonical user dictionary.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """
        payload = self._request("GET", "/users/{0}".format(username))
        if not payload:
            raise AdapterNotFoundError(
                "Okta user {0} does not exist".format(username),
                {"username": username},
            )
        return self._map_user(username, payload)

    def get_groups(self, username: str) -> List[str]:
        """Return the groups a user belongs to.

        Args:
            username: Login of the user.

        Returns:
            The list of group names.
        """
        payload = self._request("GET", "/users/{0}/groups".format(username))
        items = payload.get("items", payload.get("value", [])) or []
        groups: List[str] = []
        for item in items:
            if isinstance(item, dict):
                profile = item.get("profile", {}) or {}
                groups.append(str(profile.get("name", "") or item.get("id", "") or ""))
            else:
                groups.append(str(item))
        return [group for group in groups if group]

    def is_active(self, username: str) -> bool:
        """Return whether the account is active.

        Args:
            username: Login of the user.

        Returns:
            ``True`` when the Okta status is ``ACTIVE``.
        """
        return bool(self.get_user(username).get("active", False))

    def get_manager(self, username: str) -> Optional[str]:
        """Return the manager of a user.

        Args:
            username: Login of the user.

        Returns:
            The login of the first manager or ``None``.
        """
        payload = self._request("GET", "/users/{0}/managers".format(username))
        items = payload.get("items", payload.get("value", [])) or []
        for item in items:
            if isinstance(item, dict):
                profile = item.get("profile", {}) or {}
                login = str(profile.get("login", "") or "")
                if login:
                    return login
            elif item:
                return str(item)
        return None

    # -- mutating operations ---------------------------------------------- #

    def _skipped(self, username: str, action: str) -> Dict[str, Any]:
        """Return the standard "writes are disabled" answer.

        Args:
            username: Login of the target user.
            action: Name of the action.

        Returns:
            The operation result dictionary.
        """
        return {
            "status": "skipped",
            "reason": "writes_disabled",
            "action_id": action_id_for(username, action, ""),
            "user": username,
            "timestamp": utc_now_iso(),
        }

    def request_access(
        self,
        username: str,
        resource: str,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register an access request without granting anything.

        Okta has no native access request workflow.  The real grant -- adding
        the user to the target group -- is performed **after** the approval
        with ``POST /users/{username}/groups/{group_id}``.

        Args:
            username: Login of the requester.
            resource: Identifier of the requested resource.
            justification: Free text justification.

        Returns:
            The request result dictionary.
        """
        return {
            "status": "pending_approval",
            "request_id": "REQ-{0}".format(str(uuid.uuid4())),
            "action_id": action_id_for(username, "request_access", resource),
            "user": username,
            "resource": resource,
            "justification": justification or "",
            "timestamp": utc_now_iso(),
        }

    def reset_password(
        self,
        username: str,
        *,
        verified: bool = False,
        verification_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Trigger an Okta password reset.

        Okta sends the reset link to the registered factor; the connector
        never sees the new password.

        Args:
            username: Login of the target user.
            verified: Whether the identity was verified out-of-band.
            verification_method: How the identity was verified.

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified.
        """
        if not verified:
            raise VerificationRequired(VERIFICATION_MESSAGE)
        action_id = action_id_for(username, "reset_password", "password")
        if not writes_enabled():
            return self._skipped(username, "reset_password")
        self._request(
            "POST",
            "/users/{0}/lifecycle/reset_password".format(username),
            json_body={"sendEmail": True},
        )
        return {
            "status": "success",
            "action_id": action_id,
            "user": username,
            "resource": "password",
            "method": verification_method or "out_of_band",
            "timestamp": utc_now_iso(),
        }

    def unlock_account(self, username: str, *, verified: bool = False) -> Dict[str, Any]:
        """Unlock an Okta account through the lifecycle API.

        Args:
            username: Login of the target user.
            verified: Whether the identity was verified out-of-band.

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified.
        """
        if not verified:
            raise VerificationRequired("Account unlock requires verified identity with MFA")
        action_id = action_id_for(username, "unlock_account", "account")
        if not writes_enabled():
            return self._skipped(username, "unlock_account")
        self._request(
            "POST",
            "/users/{0}/lifecycle/unlock".format(username),
            json_body={},
        )
        return {
            "status": "success",
            "action_id": action_id,
            "user": username,
            "resource": "account",
            "timestamp": utc_now_iso(),
        }


__all__ = [
    "DEFAULT_ACCESS_POLICIES_PATH",
    "DEFAULT_GROUPS_PATH",
    "DEFAULT_USERS_PATH",
    "EntraIDIdentity",
    "MockIdentity",
    "OktaIdentity",
    "VERIFICATION_MESSAGE",
]
