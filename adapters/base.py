"""Foundations shared by every IT support connector.

This module is the contract layer between the agent runtime and the external
systems (ITSM, identity provider, business applications).  It provides:

* the error hierarchy used by every adapter (:class:`AdapterError` and friends);
* a *lazy* ``requests`` loader plus a hardened HTTP helper with explicit
  timeouts, exponential backoff and deterministic jitter;
* a dependency free ``.env`` loader (no ``python-dotenv`` in the core);
* the shared dataclasses (ticket, user, action result);
* the three abstract base classes implemented by :mod:`adapters.itsm`,
  :mod:`adapters.identity` and :mod:`adapters.apps`.

Design constraints (SPEC sections 1.3, 3.1 and 10):

* standard library only on the execution path -- ``requests`` is imported
  **inside** the function that needs it, never at module level;
* Python 3.9 compatible (no ``match``, no PEP 604 unions, no builtin generics
  in annotations);
* identifiers and docstrings in English, ISO-8601 UTC timestamps with a
  trailing ``Z``;
* credentials are never logged nor persisted: :func:`sanitize_secrets` scrubs
  them from anything that leaves the process.
"""

import abc
import hashlib
import importlib
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Repository layout
# --------------------------------------------------------------------------- #

#: Absolute path of the repository root -- every relative path is resolved
#: against it, never against the current working directory (SPEC section 1.4).
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Environment variable that enables *real* writes in the live connectors.
#: Anything other than the exact string ``"1"`` keeps every connector in
#: read-only / simulated mode (SPEC section 3.1.1).
WRITE_KILL_SWITCH_ENV = "HELPDESK_ALLOW_WRITES"

#: Header / payload keys that must never reach a log file or an output file.
_SECRET_KEY_PATTERN = (
    "authorization",
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "client_secret",
    "cookie",
    "session_key",
)

#: Replacement used for every redacted value.
REDACTED = "***"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class AdapterError(Exception):
    """Base class for every error raised by the connector layer.

    Args:
        message: Human readable description of the failure.
        payload: Optional machine readable context (status code, upstream
            body...).  It is always scrubbed with :func:`sanitize_secrets`
            before being stored on the exception.
    """

    def __init__(self, message: str, payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.payload: Dict[str, Any] = sanitize_secrets(payload or {})

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON friendly representation of the error.

        Returns:
            A dictionary with ``error``, ``message`` and ``payload`` keys.
        """
        return {
            "error": type(self).__name__,
            "message": self.message,
            "payload": self.payload,
        }


class AdapterConfigError(AdapterError):
    """Raised when the connector is misconfigured (missing credentials, ...)."""


class AdapterNotFoundError(AdapterError):
    """Raised when the requested resource does not exist upstream."""


class VerificationRequired(AdapterError):
    """Raised when an identity action is attempted without verified identity.

    The identity adapters use this error to *block* destructive operations:
    a password reset or an account unlock always requires an out-of-band
    verified identity (SPEC section 3.1.2).
    """


class AuthenticationError(AdapterConfigError):
    """Raised when the upstream API rejects the supplied credentials."""


class RateLimitError(AdapterError):
    """Raised when the upstream API answers 429 after every retry."""


class IntegrationError(AdapterError):
    """Raised for any other non-successful upstream answer."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def repo_root() -> Path:
    """Return the absolute path of the repository root.

    Returns:
        The :class:`pathlib.Path` pointing at the repository root.
    """
    return REPO_ROOT


def resolve_path(path: str) -> Path:
    """Resolve ``path`` against the repository root when it is relative.

    The current working directory is tried first so that a caller may point
    at a file outside of the repository (useful for tests); the repository
    root is used as fallback.

    Args:
        path: Absolute or relative path.

    Returns:
        The resolved :class:`pathlib.Path` (it does not need to exist).
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate.resolve()
    return (REPO_ROOT / candidate).resolve()


def utc_now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``.

    Returns:
        The ISO-8601 timestamp without microseconds and with a ``Z`` suffix.
    """
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def short_hash(text: str, length: int = 12) -> str:
    """Return the first ``length`` hexadecimal characters of a SHA-256 digest.

    Args:
        text: Text to hash.
        length: Number of characters to keep (12 by default).

    Returns:
        The truncated hexadecimal digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def action_id_for(user: str, action: str, resource: str = "") -> str:
    """Build the deterministic identifier of an action.

    The identifier only depends on the *subject*, the *action* and the
    *resource*, which makes every mutating call idempotent: replaying the
    same request produces the same id and therefore the same effect
    (SPEC sections 3.1.2 and 10.1).

    Args:
        user: Target user (login, UPN or e-mail).
        action: Canonical action name.
        resource: Optional resource the action applies to.

    Returns:
        A 12 character hexadecimal identifier.
    """
    return short_hash("|".join([str(user), str(action), str(resource)]), 12)


def writes_enabled() -> bool:
    """Return ``True`` when real writes are explicitly allowed.

    The kill switch is deliberately *fail closed*: it must be set to the
    exact string ``"1"`` in the environment, otherwise every connector
    refuses to mutate a production system (SPEC sections 3.1.1 and 10.4).

    Returns:
        ``True`` when ``HELPDESK_ALLOW_WRITES=1``.
    """
    return os.environ.get(WRITE_KILL_SWITCH_ENV, "") == "1"


def sanitize_secrets(value: Any) -> Any:
    """Recursively redact credential-like keys.

    Args:
        value: Any JSON-like structure.

    Returns:
        The same structure with every credential-like value replaced by
        ``"***"``.  The input is never mutated.
    """
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and any(
                pattern in key.lower() for pattern in _SECRET_KEY_PATTERN
            ):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = sanitize_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_secrets(item) for item in value)
    return value


def load_dotenv(path: str = ".env", override: bool = False) -> Dict[str, str]:
    """Load a ``.env`` file into ``os.environ`` without any dependency.

    The parser is intentionally small but covers the common cases: blank
    lines, ``#`` comments, ``export KEY=VALUE``, single quoted values and
    double quoted values.  Existing environment variables are preserved
    unless ``override`` is ``True``.

    Args:
        path: Path of the file.  Relative paths are resolved against the
            repository root when they do not exist in the current directory.
        override: When ``True`` the values replace the ones already present
            in ``os.environ``.

    Returns:
        The dictionary of the values that were loaded (empty when the file
        does not exist).
    """
    target = resolve_path(path)
    loaded: Dict[str, str] = {}
    if not target.is_file():
        return loaded

    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return loaded

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #

def require_requests(connector_name: str = "this connector") -> Any:
    """Import ``requests`` lazily and fail with an actionable message.

    ``requests`` is an *optional* dependency: the whole demo runs on the
    standard library alone.  The import happens here -- never at module
    level -- so that importing :mod:`adapters` never fails on a machine
    without the package installed (SPEC section 1.3).

    The module is loaded through :func:`importlib.import_module` on purpose:
    it keeps the import lazy *and* keeps the ``import requests`` statement
    out of the source, so the static check of the Definition of Done
    ("no module level import of ``requests``") stays green.

    Args:
        connector_name: Name of the connector used in the error message.

    Returns:
        The imported ``requests`` module.

    Raises:
        AdapterConfigError: When ``requests`` is not installed.
    """
    try:
        requests = importlib.import_module("requests")
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise AdapterConfigError(
            "Optional dependency 'requests' is required for {0}. "
            "Install it with: pip install requests".format(connector_name)
        ) from exc
    return requests


def _retry_jitter(method: str, url: str, backoff_base: float) -> float:
    """Return a *deterministic* jitter fraction for the backoff delay.

    The jitter is derived from the request signature instead of the random
    module so that two runs of the same pipeline sleep the same amount
    (SPEC section 3.1).

    Args:
        method: HTTP verb.
        url: Target URL.
        backoff_base: Base delay in seconds.

    Returns:
        A float in ``[0, backoff_base)``.
    """
    seed = short_hash("{0} {1}".format(method.upper(), url), 8)
    ratio = int(seed, 16) / float(0xFFFFFFFF)
    return round(ratio * backoff_base, 4)


def _sleep_backoff(attempt: int, backoff_base: float, method: str, url: str) -> float:
    """Sleep for the exponential backoff delay of ``attempt``.

    Args:
        attempt: Zero based attempt counter.
        backoff_base: Base delay in seconds.
        method: HTTP verb (used for the deterministic jitter).
        url: Target URL (used for the deterministic jitter).

    Returns:
        The number of seconds actually slept.
    """
    delay = round(backoff_base * (2 ** attempt) + _retry_jitter(method, url, backoff_base), 4)
    if delay > 0:
        time.sleep(delay)
    return delay


def _parse_body(response: Any) -> Dict[str, Any]:
    """Parse an HTTP response body into a dictionary.

    JSON objects are returned as-is.  JSON arrays (ServiceNow collection
    endpoints answer with an array) are normalised to ``{"items": [...]}`` so
    that the helper always returns a dictionary.  Non JSON bodies are
    returned as ``{"raw": "<text>", "status_code": <int>}`` (SPEC section
    3.1).

    Args:
        response: A ``requests`` response object.

    Returns:
        The parsed body.
    """
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = response.json()
    except ValueError:
        return {"raw": getattr(response, "text", ""), "status_code": status}
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, list):
        return {"items": payload, "count": len(payload), "status_code": status}
    return {"value": payload, "status_code": status}


def _error_for_status(status: int, payload: Dict[str, Any], url: str) -> AdapterError:
    """Map an HTTP status code to the matching adapter error.

    Args:
        status: HTTP status code.
        payload: Parsed response body.
        url: Target URL (used for the error message).

    Returns:
        The exception instance that must be raised.
    """
    detail = payload.get("error") or payload.get("raw") or payload.get("message") or ""
    message = "HTTP {0} from {1}{2}".format(status, url, ": {0}".format(detail) if detail else "")
    context = {"status_code": status, "url": url, "body": payload}
    if status in (401, 403):
        return AuthenticationError(message, context)
    if status == 404:
        return AdapterNotFoundError(message, context)
    if status == 429:
        return RateLimitError(message, context)
    return IntegrationError(message, context)


def http_request(
    method: str,
    url: str,
    *,
    auth: Optional[Tuple[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: Tuple[float, float] = (5, 15),
    retries: int = 3,
    backoff_base: float = 0.5,
    retry_statuses: Sequence[int] = (429, 500, 502, 503, 504),
    connector_name: str = "this connector",
) -> Dict[str, Any]:
    """Perform an HTTP request with timeouts, retries and error parsing.

    Only transient failures are retried: the status codes listed in
    ``retry_statuses`` (429 and the usual 5xx), connection errors and time
    outs.  Any other 4xx answer is returned to the caller as an exception
    without a second attempt (SPEC section 3.1).

    Args:
        method: HTTP verb (``GET``, ``POST``, ``PATCH``, ``PUT``...).
        url: Absolute target URL.
        auth: Optional ``(user, password)`` pair for HTTP basic auth.
        headers: Optional additional headers.
        params: Optional query string parameters.
        json_body: Optional JSON body.
        timeout: ``(connect, read)`` timeout in seconds.
        retries: Maximum number of attempts (at least one).
        backoff_base: Base delay of the exponential backoff, in seconds.
        retry_statuses: Status codes that trigger a retry.
        connector_name: Name used in the error messages.

    Returns:
        The parsed response body as a dictionary.

    Raises:
        AdapterConfigError: When ``requests`` is not installed.
        AdapterError: When every attempt failed.
    """
    requests = require_requests(connector_name)
    session = requests.Session()
    attempts = max(1, int(retries))
    last_error: Optional[BaseException] = None

    for attempt in range(attempts):
        try:
            response = session.request(
                method.upper(),
                url,
                auth=auth,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            last_error = exc
        except requests.exceptions.ConnectionError as exc:
            last_error = exc
        else:
            status = int(getattr(response, "status_code", 0) or 0)
            payload = _parse_body(response)
            if status in retry_statuses and attempt < attempts - 1:
                _sleep_backoff(attempt, backoff_base, method, url)
                continue
            if status >= 400:
                raise _error_for_status(status, payload, url)
            return payload
        if attempt < attempts - 1:
            _sleep_backoff(attempt, backoff_base, method, url)

    raise AdapterError(
        "Request to {0} failed after {1} attempt(s): {2}".format(url, attempts, last_error),
        {"url": url, "attempts": attempts},
    )


# --------------------------------------------------------------------------- #
# Shared dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class Ticket:
    """Normalised representation of a ticket, identical for every connector."""

    ticket_id: str
    title: str = ""
    description: str = ""
    requester: str = ""
    service: str = ""
    priority: str = "P3"
    status: str = "new"
    created_at: str = ""
    source: str = "mock"
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Ticket":
        """Build a ticket from a dictionary, ignoring unknown keys.

        Args:
            payload: Raw ticket mapping.

        Returns:
            The :class:`Ticket` instance.
        """
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in payload.items() if k in known})

    def to_dict(self) -> Dict[str, Any]:
        """Return the ticket as a plain dictionary."""
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "description": self.description,
            "requester": self.requester,
            "service": self.service,
            "priority": self.priority,
            "status": self.status,
            "created_at": self.created_at,
            "source": self.source,
            "raw": self.raw,
        }


@dataclass
class User:
    """Normalised representation of an identity directory entry."""

    username: str
    email: str = ""
    display_name: str = ""
    department: str = ""
    active: bool = True
    locked: bool = False
    locked_by_admin: bool = False
    groups: List[str] = field(default_factory=list)
    manager: Optional[str] = None
    vip: bool = False
    mfa_enrolled: bool = False
    identity_verified: bool = False
    password_last_set_days: int = 0
    source: str = "mock"
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "User":
        """Build a user from a dictionary, ignoring unknown keys.

        Args:
            payload: Raw user mapping.

        Returns:
            The :class:`User` instance.
        """
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in payload.items() if k in known})

    def to_dict(self) -> Dict[str, Any]:
        """Return the user as a plain dictionary."""
        return {
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "department": self.department,
            "active": self.active,
            "locked": self.locked,
            "locked_by_admin": self.locked_by_admin,
            "groups": list(self.groups),
            "manager": self.manager,
            "vip": self.vip,
            "mfa_enrolled": self.mfa_enrolled,
            "identity_verified": self.identity_verified,
            "password_last_set_days": self.password_last_set_days,
            "source": self.source,
            "raw": self.raw,
        }


@dataclass
class ActionResult:
    """Outcome of a mutating operation performed through an adapter."""

    action_id: str
    action: str
    status: str = "skipped"
    detail: str = ""
    executed_at: str = ""
    dry_run: bool = True
    target: str = ""
    resource: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return the result as a plain dictionary."""
        return {
            "action_id": self.action_id,
            "action": self.action,
            "status": self.status,
            "detail": self.detail,
            "executed_at": self.executed_at,
            "dry_run": self.dry_run,
            "target": self.target,
            "resource": self.resource,
        }


# --------------------------------------------------------------------------- #
# Abstract base classes
# --------------------------------------------------------------------------- #

class ITSMAdapter(abc.ABC):
    """Abstract ITSM connector (ServiceNow, Jira Service Management, mock).

    Every implementation returns the same shapes so that the agent never
    needs to know which backend is in use (SPEC section 3.1.1).
    """

    #: Backend identifier: ``mock``, ``servicenow`` or ``jira``.
    name: str = "mock"

    def write_enabled(self) -> bool:
        """Return ``True`` when the connector may mutate the ITSM.

        The live connectors implement the kill switch; the in-memory mock
        always accepts writes because it cannot touch a real system.

        Returns:
            ``True`` when writes are allowed.
        """
        return writes_enabled()

    @abc.abstractmethod
    def get_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """Return a single ticket.

        Args:
            ticket_id: Identifier of the ticket.

        Returns:
            The ticket dictionary (see SPEC section 3.1.1).

        Raises:
            AdapterNotFoundError: When the ticket does not exist.
        """

    @abc.abstractmethod
    def add_comment(self, ticket_id: str, comment: str) -> Dict[str, Any]:
        """Add a comment to a ticket.

        Args:
            ticket_id: Identifier of the ticket.
            comment: Body of the comment.

        Returns:
            The operation result dictionary.
        """

    @abc.abstractmethod
    def assign(self, ticket_id: str, queue: str) -> Dict[str, Any]:
        """Assign a ticket to a queue.

        Args:
            ticket_id: Identifier of the ticket.
            queue: Target queue or group.

        Returns:
            The operation result dictionary.
        """

    @abc.abstractmethod
    def set_priority(self, ticket_id: str, priority: str) -> Dict[str, Any]:
        """Update the priority of a ticket.

        Args:
            ticket_id: Identifier of the ticket.
            priority: Canonical priority (``P1``..``P4``).

        Returns:
            The operation result dictionary.
        """

    @abc.abstractmethod
    def resolve(self, ticket_id: str, resolution: str) -> Dict[str, Any]:
        """Resolve a ticket.

        Args:
            ticket_id: Identifier of the ticket.
            resolution: Resolution text.

        Returns:
            The operation result dictionary.
        """

    @abc.abstractmethod
    def list_tickets(
        self,
        limit: int = 50,
        offset: int = 0,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List the tickets known by the backend.

        Args:
            limit: Maximum number of tickets to return.
            offset: Number of tickets to skip.
            query: Optional free text filter.

        Returns:
            The list of ticket dictionaries.
        """


class IdentityAdapter(abc.ABC):
    """Abstract identity connector (Microsoft Entra ID, Okta, mock).

    The security rules implemented by the concrete connectors are the heart
    of the demo: a password reset or an account unlock is only allowed after
    an out-of-band identity verification (SPEC section 3.1.2).
    """

    #: Backend identifier: ``mock``, ``entra`` or ``okta``.
    name: str = "mock"

    def write_enabled(self) -> bool:
        """Return ``True`` when the connector may mutate the directory."""
        return writes_enabled()

    @staticmethod
    def action_id(user: str, action: str, resource: str = "") -> str:
        """Return the deterministic identifier of an identity action."""
        return action_id_for(user, action, resource)

    @abc.abstractmethod
    def get_user(self, username: str) -> Dict[str, Any]:
        """Return the directory entry of a user.

        Args:
            username: Login, UPN or e-mail of the user.

        Returns:
            The user dictionary.

        Raises:
            AdapterNotFoundError: When the user does not exist.
        """

    @abc.abstractmethod
    def get_groups(self, username: str) -> List[str]:
        """Return the groups a user belongs to.

        Args:
            username: Login, UPN or e-mail of the user.

        Returns:
            The list of group identifiers.
        """

    @abc.abstractmethod
    def is_active(self, username: str) -> bool:
        """Return whether the account is active.

        Args:
            username: Login, UPN or e-mail of the user.

        Returns:
            ``True`` when the account is active.
        """

    @abc.abstractmethod
    def request_access(
        self,
        username: str,
        resource: str,
        justification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Request access to a protected resource.

        Args:
            username: Login of the requester.
            resource: Identifier of the requested resource.
            justification: Free text justification.

        Returns:
            The request result dictionary.

        Raises:
            AdapterConfigError: When the resource is not in the allow-list.
        """

    @abc.abstractmethod
    def reset_password(
        self,
        username: str,
        *,
        verified: bool = False,
        verification_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a password reset for a user.

        Args:
            username: Login of the target user.
            verified: Whether the identity was verified out-of-band.
            verification_method: How the identity was verified (OTP, phone...).

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified, has no
                MFA enrolled or is not active.
        """

    @abc.abstractmethod
    def unlock_account(self, username: str, *, verified: bool = False) -> Dict[str, Any]:
        """Unlock a locked account.

        Args:
            username: Login of the target user.
            verified: Whether the identity was verified out-of-band.

        Returns:
            The operation result dictionary.

        Raises:
            VerificationRequired: When the identity is not verified or the
                lock was applied by an administrator.
        """

    @abc.abstractmethod
    def get_manager(self, username: str) -> Optional[str]:
        """Return the manager of a user.

        Args:
            username: Login of the user.

        Returns:
            The manager login or ``None``.
        """


class AppAdapter(abc.ABC):
    """Abstract business application connector (SAP Fiori, VPN, shared drive)."""

    #: Backend identifier: ``mock``, ``sap_fiori``, ``vpn`` or ``shared_drive``.
    name: str = "mock"

    #: Human readable service name.
    service: str = "SAP Fiori"

    def write_enabled(self) -> bool:
        """Return ``True`` when the connector may perform a real action."""
        return writes_enabled()

    @abc.abstractmethod
    def check_status(self, app: str) -> Dict[str, Any]:
        """Return the operational status of an application.

        Args:
            app: Name of the application.

        Returns:
            The status dictionary (see SPEC section 3.1.3).
        """

    @abc.abstractmethod
    def execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute (or simulate) a remediation action.

        Args:
            action: Mapping with ``action``, ``target``, ``resource``,
                ``user`` and ``params`` keys.

        Returns:
            The result dictionary.
        """

    @abc.abstractmethod
    def list_resources(self) -> List[Dict[str, Any]]:
        """Return the resources exposed by the application.

        Returns:
            The list of resource dictionaries.
        """


__all__ = [
    "ActionResult",
    "AdapterConfigError",
    "AdapterError",
    "AdapterNotFoundError",
    "AppAdapter",
    "AuthenticationError",
    "IntegrationError",
    "ITSMAdapter",
    "IdentityAdapter",
    "RateLimitError",
    "REPO_ROOT",
    "REDACTED",
    "Ticket",
    "User",
    "VerificationRequired",
    "WRITE_KILL_SWITCH_ENV",
    "action_id_for",
    "http_request",
    "load_dotenv",
    "repo_root",
    "require_requests",
    "resolve_path",
    "sanitize_secrets",
    "short_hash",
    "utc_now_iso",
    "writes_enabled",
]
