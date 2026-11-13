"""Public API of the connector layer.

This package exposes the three factories consumed by ``agent.py``
(SPEC section 3.2.1)::

    build_itsm_adapter(kind=None, **kwargs) -> ITSMAdapter
    build_identity_adapter(kind=None, **kwargs) -> IdentityAdapter
    build_app_adapter(kind=None, **kwargs) -> AppAdapter

The backend is selected by an environment variable:

===================  ==========================  ==============================
Variable             Values                     Adapter
===================  ==========================  ==============================
``HELPDESK_ITSM_ADAPTER``    ``mock``/``servicenow``/``jira``     :class:`~adapters.itsm.MockITSM`, :class:`~adapters.itsm.ServiceNowITSM`, :class:`~adapters.itsm.JiraITSM`
``HELPDESK_IDENTITY_ADAPTER`` ``mock``/``entra``/``okta``          :class:`~adapters.identity.MockIdentity`, :class:`~adapters.identity.EntraIDIdentity`, :class:`~adapters.identity.OktaIdentity`
``HELPDESK_APP_ADAPTER``      ``mock``/``sap_fiori``/``vpn``/``shared_drive``  :class:`~adapters.apps.MockApps` and its variants
===================  ==========================  ==============================

The default is always ``mock``: the demo runs without a single credential.
Requesting a real backend **without** credentials fails fast, at build time,
with an actionable message (SPEC section 3.2.1).
"""

import os
from typing import Any, Dict, Optional, Tuple

from .base import (
    AdapterConfigError,
    AppAdapter,
    ITSMAdapter,
    IdentityAdapter,
    load_dotenv,
)
from .apps import MockApps, MockSAPFioriApp, MockSharedDriveApp, MockVPNApp
from .identity import EntraIDIdentity, MockIdentity, OktaIdentity
from .itsm import JiraITSM, MockITSM, ServiceNowITSM

#: Available ITSM backends.
ITSM_KINDS: Tuple[str, ...] = ("mock", "servicenow", "jira")

#: Available identity backends.
IDENTITY_KINDS: Tuple[str, ...] = ("mock", "entra", "okta")

#: Available application backends.
APP_KINDS: Tuple[str, ...] = ("mock", "sap_fiori", "vpn", "shared_drive")

#: Default paths of the mock data sources.
DEFAULT_TICKETS_PATH = "data/tickets.csv"
DEFAULT_USERS_PATH = "data/users.json"
DEFAULT_GROUPS_PATH = "data/groups.json"


def _resolve_kind(
    kind: Optional[str],
    env_var: str,
    alias_env_var: str,
    valid: Tuple[str, ...],
    label: str,
) -> str:
    """Resolve the backend to build.

    Args:
        kind: Explicit backend requested by the caller (may be ``None``).
        env_var: Environment variable holding the backend name.
        alias_env_var: Alternative environment variable, checked as fallback.
        valid: The accepted backend names.
        label: Human readable name of the family, used in the error message.

    Returns:
        The selected backend name.

    Raises:
        AdapterConfigError: When the requested backend is unknown.
    """
    selected = kind or os.environ.get(env_var) or os.environ.get(alias_env_var) or "mock"
    selected = str(selected).strip().lower()
    if selected not in valid:
        raise AdapterConfigError(
            "Unknown {0} adapter '{1}'. Valid options: {2}".format(
                label, selected, ", ".join(valid)
            )
        )
    return selected


def _pick(kwargs: Dict[str, Any], name: str, default: Any) -> Any:
    """Return a keyword argument when the caller provided it.

    Args:
        kwargs: The keyword arguments of the factory.
        name: Name of the argument.
        default: Value returned when the argument is absent.

    Returns:
        The value of the argument or the default.
    """
    value = kwargs.get(name)
    return default if value is None else value


def build_itsm_adapter(kind: Optional[str] = None, **kwargs: Any) -> ITSMAdapter:
    """Build the ITSM connector selected by ``kind`` or by the environment.

    Args:
        kind: ``mock``, ``servicenow`` or ``jira``.  When ``None`` the
            ``HELPDESK_ITSM_ADAPTER`` variable decides (default ``mock``).
        **kwargs: Optional ``data_path``, ``base_url`` and ``timeout``.

    Returns:
        The ready to use :class:`~adapters.base.ITSMAdapter`.

    Raises:
        AdapterConfigError: When the backend is unknown or when a real
            backend is requested without credentials.
    """
    load_dotenv()
    selected = _resolve_kind(
        kind, "HELPDESK_ITSM_ADAPTER", "HELPDESK_ITSM", ITSM_KINDS, "ITSM"
    )
    if selected == "mock":
        return MockITSM(data_path=_pick(kwargs, "data_path", DEFAULT_TICKETS_PATH))
    if selected == "servicenow":
        return ServiceNowITSM(
            instance=kwargs.get("instance"),
            username=kwargs.get("username"),
            password=kwargs.get("password"),
            access_token=kwargs.get("access_token"),
            table=_pick(kwargs, "table", "incident"),
            assignment_group=_pick(kwargs, "assignment_group", "Service Desk"),
            base_url=kwargs.get("base_url"),
            timeout=_pick(kwargs, "timeout", (5, 15)),
            retries=int(_pick(kwargs, "retries", 3)),
        )
    return JiraITSM(
        url=kwargs.get("url"),
        email=kwargs.get("email"),
        api_token=kwargs.get("api_token"),
        project_key=kwargs.get("project_key"),
        issue_type=_pick(kwargs, "issue_type", "Service Request"),
        resolve_transition_id=_pick(kwargs, "resolve_transition_id", "31"),
        timeout=_pick(kwargs, "timeout", (5, 15)),
        retries=int(_pick(kwargs, "retries", 3)),
    )


def build_identity_adapter(kind: Optional[str] = None, **kwargs: Any) -> IdentityAdapter:
    """Build the identity connector selected by ``kind`` or by the environment.

    Args:
        kind: ``mock``, ``entra`` or ``okta``.  When ``None`` the
            ``HELPDESK_IDENTITY_ADAPTER`` variable decides (default ``mock``).
        **kwargs: Optional ``users_path``, ``groups_path``, ``base_url`` and
            ``timeout``.

    Returns:
        The ready to use :class:`~adapters.base.IdentityAdapter`.

    Raises:
        AdapterConfigError: When the backend is unknown or when a real
            backend is requested without credentials.
    """
    load_dotenv()
    selected = _resolve_kind(
        kind, "HELPDESK_IDENTITY_ADAPTER", "HELPDESK_IDENTITY", IDENTITY_KINDS, "identity"
    )
    if selected == "mock":
        return MockIdentity(
            users_path=_pick(kwargs, "users_path", DEFAULT_USERS_PATH),
            groups_path=_pick(kwargs, "groups_path", DEFAULT_GROUPS_PATH),
            access_policies_path=_pick(
                kwargs, "access_policies_path", "policies/access_policies.json"
            ),
        )
    if selected == "entra":
        return EntraIDIdentity(
            tenant_id=kwargs.get("tenant_id"),
            client_id=kwargs.get("client_id"),
            client_secret=kwargs.get("client_secret"),
            access_token=kwargs.get("access_token"),
            base_url=_pick(kwargs, "base_url", "https://graph.microsoft.com/v1.0"),
            timeout=_pick(kwargs, "timeout", (5, 15)),
            retries=int(_pick(kwargs, "retries", 3)),
        )
    return OktaIdentity(
        org=kwargs.get("org"),
        api_token=kwargs.get("api_token"),
        base_url=kwargs.get("base_url"),
        timeout=_pick(kwargs, "timeout", (5, 15)),
        retries=int(_pick(kwargs, "retries", 3)),
    )


def build_app_adapter(kind: Optional[str] = None, **kwargs: Any) -> AppAdapter:
    """Build the application connector selected by ``kind`` or the environment.

    Args:
        kind: ``mock``, ``sap_fiori``, ``vpn`` or ``shared_drive``.  When
            ``None`` the ``HELPDESK_APP_ADAPTER`` variable decides (default
            ``mock``).
        **kwargs: Optional ``status`` and ``timeout`` placeholders.

    Returns:
        The ready to use :class:`~adapters.base.AppAdapter`.

    Raises:
        AdapterConfigError: When the backend is unknown.
    """
    load_dotenv()
    selected = _resolve_kind(
        kind, "HELPDESK_APP_ADAPTER", "HELPDESK_APP", APP_KINDS, "application"
    )
    status = _pick(kwargs, "status", "operational")
    if selected == "mock":
        return MockApps(service=_pick(kwargs, "service", "SAP Fiori"), status=status)
    if selected == "sap_fiori":
        return MockSAPFioriApp(status=status)
    if selected == "vpn":
        return MockVPNApp(status=status)
    return MockSharedDriveApp(status=status)


__all__ = [
    "APP_KINDS",
    "EntraIDIdentity",
    "IDENTITY_KINDS",
    "ITSM_KINDS",
    "JiraITSM",
    "MockApps",
    "MockIdentity",
    "MockITSM",
    "MockSAPFioriApp",
    "MockSharedDriveApp",
    "MockVPNApp",
    "OktaIdentity",
    "ServiceNowITSM",
    "build_app_adapter",
    "build_identity_adapter",
    "build_itsm_adapter",
]
