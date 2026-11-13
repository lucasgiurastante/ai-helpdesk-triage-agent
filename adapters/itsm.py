"""ITSM connectors: an in-memory mock plus the ServiceNow and Jira clients.

The three implementations share the exact same shapes so the agent runtime is
backend agnostic (SPEC section 3.1.1):

* ``get_ticket`` / ``list_tickets`` return the canonical *ticket* mapping;
* ``add_comment`` / ``assign`` / ``set_priority`` / ``resolve`` return the
  canonical *operation result* mapping.

The live connectors are written against the real APIs (ServiceNow Table API
and Jira REST v3).  They can be imported -- and unit tested -- without
``requests`` installed and without credentials; the credentials are only
required when the connector is *built* with a real backend, and any write
needs both credentials **and** ``HELPDESK_ALLOW_WRITES=1``.
"""

import csv
import os
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    AdapterConfigError,
    AdapterNotFoundError,
    ITSMAdapter,
    http_request,
    resolve_path,
    sanitize_secrets,
    utc_now_iso,
    writes_enabled,
)

#: Default location of the mock ticket backlog.
DEFAULT_TICKETS_PATH = "data/tickets.csv"

#: Canonical priority -> ServiceNow numeric priority.
SERVICENOW_PRIORITY: Dict[str, str] = {"P1": "1", "P2": "2", "P3": "3", "P4": "4"}

#: ServiceNow numeric priority -> canonical priority.
SERVICENOW_PRIORITY_REVERSE: Dict[str, str] = {
    "1": "P1",
    "2": "P2",
    "3": "P3",
    "4": "P4",
    "5": "P4",
}

#: Default (connect, read) timeout in seconds.
DEFAULT_TIMEOUT: Tuple[float, float] = (5, 15)


def _ticket_title(description: str, limit: int = 80) -> str:
    """Build a short human readable title out of a description.

    Args:
        description: Free text description of the ticket.
        limit: Maximum length of the generated title.

    Returns:
        The truncated first line of the description.
    """
    if not description:
        return ""
    first_line = description.strip().splitlines()[0]
    if len(first_line) <= limit:
        return first_line
    return first_line[: limit - 1].rstrip() + "…"


class MockITSM(ITSMAdapter):
    """In-memory ITSM backed by ``data/tickets.csv``.

    The mock owns no hardcoded ticket: it reads the CSV produced by the
    runtime and keeps the write operations in memory, which makes the demo
    reproducible and side-effect free (SPEC section 3.2.2).

    Args:
        data_path: Path of the CSV file with the tickets.
    """

    name = "mock"

    def __init__(self, data_path: str = DEFAULT_TICKETS_PATH) -> None:
        self.data_path = str(data_path)
        self._tickets: Dict[str, Dict[str, Any]] = {}
        self._comments: Dict[str, List[str]] = {}
        self._priority: Dict[str, str] = {}
        self._queue: Dict[str, str] = {}
        self._resolved: Dict[str, str] = {}
        self._loaded = False

    # -- loading ---------------------------------------------------------- #

    def _ensure_loaded(self) -> None:
        """Read the CSV backlog into memory once.

        The file is read lazily so that building the adapter never fails:
        a missing backlog is reported when the tickets are actually used.

        Raises:
            AdapterConfigError: When the file does not exist.
        """
        if self._loaded:
            return
        path = resolve_path(self.data_path)
        if not path.is_file():
            raise AdapterConfigError(
                "ITSM ticket file not found: {0}. Create it or point "
                "MockITSM(data_path=...) to an existing CSV "
                "(header: ticket_id,requester,description,service,created_at).".format(path)
            )
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                clean = {
                    (key or "").strip(): (value or "").strip()
                    for key, value in row.items()
                    if key
                }
                ticket_id = clean.get("ticket_id", "")
                if not ticket_id:
                    continue
                description = clean.get("description", "")
                self._tickets[ticket_id] = {
                    "ticket_id": ticket_id,
                    "title": _ticket_title(description),
                    "description": description,
                    "requester": clean.get("requester", ""),
                    "service": clean.get("service", ""),
                    "priority": "P3",
                    "status": "new",
                    "created_at": clean.get("created_at", ""),
                    "source": "mock",
                    "raw": dict(clean),
                }
        self._loaded = True

    # -- helpers ---------------------------------------------------------- #

    def write_enabled(self) -> bool:
        """Return ``True``: the mock is in-memory and cannot harm a system."""
        return True

    def _require_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """Return a ticket, applying the in-memory overrides.

        Args:
            ticket_id: Identifier of the ticket.

        Returns:
            The ticket dictionary.

        Raises:
            AdapterNotFoundError: When the ticket does not exist.
        """
        self._ensure_loaded()
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise AdapterNotFoundError(
                "Ticket {0} does not exist in {1}".format(ticket_id, self.data_path),
                {"ticket_id": ticket_id},
            )
        updated = dict(ticket)
        if ticket_id in self._priority:
            updated["priority"] = self._priority[ticket_id]
        if ticket_id in self._queue:
            updated["queue"] = self._queue[ticket_id]
        if ticket_id in self._resolved:
            updated["status"] = "resolved"
            updated["resolution"] = self._resolved[ticket_id]
        if ticket_id in self._comments:
            updated["comments"] = list(self._comments[ticket_id])
        return updated

    # -- read operations -------------------------------------------------- #

    def get_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """Return a single ticket from the in-memory backlog.

        Args:
            ticket_id: Identifier of the ticket.

        Returns:
            The ticket dictionary.

        Raises:
            AdapterNotFoundError: When the ticket does not exist.
        """
        return self._require_ticket(ticket_id)

    def list_tickets(
        self,
        limit: int = 50,
        offset: int = 0,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the tickets of the backlog, with the in-memory overrides.

        Args:
            limit: Maximum number of tickets to return.
            offset: Number of tickets to skip.
            query: Optional free text filter matched against the ticket id,
                the requester, the service and the description.

        Returns:
            The list of ticket dictionaries.
        """
        self._ensure_loaded()
        tickets = [self._require_ticket(ticket_id) for ticket_id in self._tickets]
        if query:
            needle = query.strip().lower()
            tickets = [
                ticket
                for ticket in tickets
                if needle in " ".join(
                    str(ticket.get(field, ""))
                    for field in ("ticket_id", "requester", "service", "description")
                ).lower()
            ]
        start = max(0, offset)
        return tickets[start : start + max(0, limit)]

    # -- write operations ------------------------------------------------- #

    def add_comment(self, ticket_id: str, comment: str) -> Dict[str, Any]:
        """Record a comment in memory.

        Args:
            ticket_id: Identifier of the ticket.
            comment: Body of the comment.

        Returns:
            The operation result dictionary.

        Raises:
            AdapterNotFoundError: When the ticket does not exist.
        """
        self._require_ticket(ticket_id)
        self._comments.setdefault(ticket_id, []).append(comment)
        return {
            "ticket_id": ticket_id,
            "status": "comment_added",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "comment": comment,
        }

    def assign(self, ticket_id: str, queue: str) -> Dict[str, Any]:
        """Record a queue assignment in memory.

        Args:
            ticket_id: Identifier of the ticket.
            queue: Target queue.

        Returns:
            The operation result dictionary.

        Raises:
            AdapterNotFoundError: When the ticket does not exist.
        """
        self._require_ticket(ticket_id)
        self._queue[ticket_id] = queue
        return {
            "ticket_id": ticket_id,
            "status": "assigned",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "queue": queue,
        }

    def set_priority(self, ticket_id: str, priority: str) -> Dict[str, Any]:
        """Record a priority change in memory.

        Args:
            ticket_id: Identifier of the ticket.
            priority: Canonical priority (``P1``..``P4``).

        Returns:
            The operation result dictionary.

        Raises:
            AdapterNotFoundError: When the ticket does not exist.
        """
        self._require_ticket(ticket_id)
        self._priority[ticket_id] = priority
        return {
            "ticket_id": ticket_id,
            "status": "priority_updated",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "priority": priority,
        }

    def resolve(self, ticket_id: str, resolution: str) -> Dict[str, Any]:
        """Record a resolution in memory.

        Args:
            ticket_id: Identifier of the ticket.
            resolution: Resolution text.

        Returns:
            The operation result dictionary.

        Raises:
            AdapterNotFoundError: When the ticket does not exist.
        """
        self._require_ticket(ticket_id)
        self._resolved[ticket_id] = resolution
        return {
            "ticket_id": ticket_id,
            "status": "resolved",
            "external_id": None,
            "timestamp": utc_now_iso(),
            "resolution": resolution,
        }


class ServiceNowITSM(ITSMAdapter):
    """ServiceNow connector built on the Table API.

    Endpoints (SPEC section 3.2.2)::

        GET   /api/now/table/{table}?number={id}&sysparm_limit=1
        GET   /api/now/table/{table}?sysparm_limit={limit}&sysparm_offset={offset}
        POST  /api/now/table/sys_journal_field
        PATCH /api/now/table/{table}/{sys_id}

    Authentication is HTTP basic (``username``/``password``) or a bearer
    token (``access_token``); the token has priority.  The instance and the
    credentials are read from the environment when they are not given:
    ``SERVICENOW_INSTANCE``, ``SERVICENOW_USERNAME``, ``SERVICENOW_PASSWORD``
    and ``SERVICENOW_ACCESS_TOKEN``.

    Args:
        instance: ServiceNow instance host name (``mycompany``).
        username: Account used for basic authentication.
        password: Password used for basic authentication.
        access_token: OAuth / REST token, takes priority over basic auth.
        table: Table to read from (``incident`` by default).
        assignment_group: Default group used when the caller does not pass one.
        base_url: Full base URL, overrides ``instance``.
        timeout: ``(connect, read)`` timeout in seconds.
        retries: Maximum number of HTTP attempts.

    Raises:
        AdapterConfigError: When the instance or the credentials are missing.
    """

    name = "servicenow"

    def __init__(
        self,
        instance: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        access_token: Optional[str] = None,
        table: str = "incident",
        assignment_group: str = "Service Desk",
        base_url: Optional[str] = None,
        timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
        retries: int = 3,
    ) -> None:
        self.instance = instance or os.environ.get("SERVICENOW_INSTANCE")
        self.username = username or os.environ.get("SERVICENOW_USERNAME")
        self.password = password or os.environ.get("SERVICENOW_PASSWORD")
        self.access_token = access_token or os.environ.get("SERVICENOW_ACCESS_TOKEN")
        self.table = table
        self.assignment_group = assignment_group
        self.timeout = timeout
        self.retries = retries
        self._sys_id_cache: Dict[str, str] = {}

        if base_url:
            self.base_url = base_url.rstrip("/")
        elif self.instance:
            self.base_url = "https://{0}.service-now.com".format(self.instance)
        else:
            raise AdapterConfigError(
                "ServiceNow instance is required. Set SERVICENOW_INSTANCE=mycompany "
                "(or pass instance=...)."
            )

        if not self.access_token and not (self.username and self.password):
            raise AdapterConfigError(
                "ServiceNow credentials are missing. Provide an access token "
                "(SERVICENOW_ACCESS_TOKEN) or a username/password pair "
                "(SERVICENOW_USERNAME / SERVICENOW_PASSWORD)."
            )

    # -- HTTP plumbing ---------------------------------------------------- #

    def _headers(self) -> Dict[str, str]:
        """Return the HTTP headers used against ServiceNow."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = "Bearer {0}".format(self.access_token)
        return headers

    def _auth(self) -> Optional[Tuple[str, str]]:
        """Return the basic auth pair, or ``None`` when a token is used."""
        if self.access_token:
            return None
        return (self.username or "", self.password or "")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Perform an authenticated request against ServiceNow.

        Args:
            method: HTTP verb.
            path: Path relative to the base URL.
            params: Optional query string parameters.
            json_body: Optional JSON body.

        Returns:
            The parsed response body.
        """
        url = "{0}{1}".format(self.base_url, path)
        return http_request(
            method,
            url,
            auth=self._auth(),
            headers=self._headers(),
            params=params,
            json_body=json_body,
            timeout=self.timeout,
            retries=self.retries,
            connector_name="ServiceNowITSM",
        )

    def _records(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract the record list out of a Table API answer.

        Args:
            payload: Parsed response body.

        Returns:
            The list of records (empty when the answer holds none).
        """
        result = payload.get("result")
        if isinstance(result, list):
            return [record for record in result if isinstance(record, dict)]
        return []

    def _sys_id(self, ticket_id: str) -> str:
        """Resolve and cache the ``sys_id`` of a ticket.

        Args:
            ticket_id: Business identifier (``number``) of the incident.

        Returns:
            The ``sys_id`` of the record.

        Raises:
            AdapterNotFoundError: When the incident does not exist.
        """
        if ticket_id in self._sys_id_cache:
            return self._sys_id_cache[ticket_id]
        payload = self._request(
            "GET",
            "/api/now/table/{0}".format(self.table),
            params={"number": ticket_id, "sysparm_limit": 1},
        )
        records = self._records(payload)
        if not records:
            raise AdapterNotFoundError(
                "ServiceNow incident {0} does not exist".format(ticket_id),
                {"ticket_id": ticket_id},
            )
        sys_id = str(records[0].get("sys_id", ""))
        if sys_id:
            self._sys_id_cache[ticket_id] = sys_id
        return sys_id

    @staticmethod
    def _map_ticket(record: Dict[str, Any]) -> Dict[str, Any]:
        """Map a ServiceNow record to the canonical ticket shape.

        Args:
            record: Raw ServiceNow record.

        Returns:
            The canonical ticket dictionary.
        """
        requested_by = record.get("requested_by")
        if isinstance(requested_by, dict):
            requester = str(
                requested_by.get("user_name")
                or requested_by.get("email")
                or requested_by.get("display_value")
                or ""
            )
        else:
            requester = str(requested_by or "")
        priority = str(record.get("priority", "") or "")
        return {
            "ticket_id": str(record.get("number", "") or record.get("sys_id", "")),
            "title": str(record.get("short_description", "") or ""),
            "description": str(record.get("description", "") or ""),
            "requester": requester,
            "service": str(record.get("category", "") or record.get("contact_type", "")),
            "priority": SERVICENOW_PRIORITY_REVERSE.get(priority, priority or "P3"),
            "status": str(record.get("state", "") or "new"),
            "created_at": str(record.get("sys_created_on", "") or ""),
            "source": "servicenow",
            "raw": sanitize_secrets(record),
        }

    # -- read operations -------------------------------------------------- #

    def get_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """Return a ServiceNow incident.

        Args:
            ticket_id: Business identifier (``number``) of the incident.

        Returns:
            The canonical ticket dictionary.

        Raises:
            AdapterNotFoundError: When the incident does not exist.
        """
        payload = self._request(
            "GET",
            "/api/now/table/{0}".format(self.table),
            params={"number": ticket_id, "sysparm_limit": 1},
        )
        records = self._records(payload)
        if not records:
            raise AdapterNotFoundError(
                "ServiceNow incident {0} does not exist".format(ticket_id),
                {"ticket_id": ticket_id},
            )
        sys_id = str(records[0].get("sys_id", "") or "")
        if sys_id:
            self._sys_id_cache[ticket_id] = sys_id
        return self._map_ticket(records[0])

    def list_tickets(
        self,
        limit: int = 50,
        offset: int = 0,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List the ServiceNow incidents.

        Args:
            limit: Maximum number of records.
            offset: Number of records to skip.
            query: Optional free text filter (``sysparm_query``).

        Returns:
            The list of canonical ticket dictionaries.
        """
        params: Dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_offset": offset,
        }
        if query:
            params["sysparm_query"] = query
        payload = self._request("GET", "/api/now/table/{0}".format(self.table), params=params)
        return [self._map_ticket(record) for record in self._records(payload)]

    # -- write operations ------------------------------------------------- #

    def _skipped(self, ticket_id: str, operation: str) -> Dict[str, Any]:
        """Return the standard "writes are disabled" answer.

        Args:
            ticket_id: Identifier of the ticket.
            operation: Name of the operation.

        Returns:
            The operation result dictionary.
        """
        return {
            "ticket_id": ticket_id,
            "status": "skipped",
            "reason": "writes_disabled",
            "operation": operation,
            "external_id": None,
            "timestamp": utc_now_iso(),
        }

    def add_comment(self, ticket_id: str, comment: str) -> Dict[str, Any]:
        """Add a comment to a ServiceNow incident.

        Args:
            ticket_id: Business identifier (``number``) of the incident.
            comment: Body of the comment.

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "add_comment")
        sys_id = self._sys_id(ticket_id)
        payload = self._request(
            "POST",
            "/api/now/table/sys_journal_field",
            json_body={"sys_id": sys_id, "article": comment, "element_id": "comments"},
        )
        return {
            "ticket_id": ticket_id,
            "status": "comment_added",
            "external_id": str(payload.get("result", {}).get("sys_id", "")) or None,
            "timestamp": utc_now_iso(),
            "comment": comment,
        }

    def assign(self, ticket_id: str, queue: str) -> Dict[str, Any]:
        """Assign a ServiceNow incident to an assignment group.

        Args:
            ticket_id: Business identifier (``number``) of the incident.
            queue: Target assignment group.

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "assign")
        sys_id = self._sys_id(ticket_id)
        self._request(
            "PATCH",
            "/api/now/table/{0}/{1}".format(self.table, sys_id),
            json_body={"assignment_group": queue or self.assignment_group},
        )
        return {
            "ticket_id": ticket_id,
            "status": "assigned",
            "external_id": sys_id,
            "timestamp": utc_now_iso(),
            "queue": queue or self.assignment_group,
        }

    def set_priority(self, ticket_id: str, priority: str) -> Dict[str, Any]:
        """Update the priority of a ServiceNow incident.

        Args:
            ticket_id: Business identifier (``number``) of the incident.
            priority: Canonical priority (``P1``..``P4``).

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "set_priority")
        sys_id = self._sys_id(ticket_id)
        numeric = SERVICENOW_PRIORITY.get(priority, priority)
        self._request(
            "PATCH",
            "/api/now/table/{0}/{1}".format(self.table, sys_id),
            json_body={"priority": numeric},
        )
        return {
            "ticket_id": ticket_id,
            "status": "priority_updated",
            "external_id": sys_id,
            "timestamp": utc_now_iso(),
            "priority": priority,
        }

    def resolve(self, ticket_id: str, resolution: str) -> Dict[str, Any]:
        """Close a ServiceNow incident.

        Args:
            ticket_id: Business identifier (``number``) of the incident.
            resolution: Resolution text (stored in ``close_notes``).

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "resolve")
        sys_id = self._sys_id(ticket_id)
        self._request(
            "PATCH",
            "/api/now/table/{0}/{1}".format(self.table, sys_id),
            json_body={
                "close_code": "Solved",
                "close_notes": resolution,
                "state": "6",
                "resolved_at": utc_now_iso(),
            },
        )
        return {
            "ticket_id": ticket_id,
            "status": "resolved",
            "external_id": sys_id,
            "timestamp": utc_now_iso(),
            "resolution": resolution,
        }


class JiraITSM(ITSMAdapter):
    """Jira Service Management connector built on REST API v3.

    Endpoints (SPEC section 3.2.2)::

        GET  /rest/api/3/issue/{id}
        GET  /rest/api/3/search?jql=project={key}&maxResults={limit}&startAt={offset}
        POST /rest/api/3/issue/{id}/comment
        PUT  /rest/api/3/issue/{id}/assignee
        PUT  /rest/api/3/issue/{id}
        POST /rest/api/3/issue/{id}/transitions

    Authentication is HTTP basic with an **API token** (never a password).
    The configuration is read from ``JIRA_URL``, ``JIRA_EMAIL``,
    ``JIRA_API_TOKEN`` and ``JIRA_PROJECT_KEY``.

    Args:
        url: Base URL of the Jira tenant (without trailing slash).
        email: E-mail of the service account.
        api_token: Jira API token.
        project_key: Key of the service management project.
        issue_type: Issue type used when creating issues.
        resolve_transition_id: Identifier of the "Done" transition.
        timeout: ``(connect, read)`` timeout in seconds.
        retries: Maximum number of HTTP attempts.

    Raises:
        AdapterConfigError: When the URL or the credentials are missing.
    """

    name = "jira"

    def __init__(
        self,
        url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        project_key: Optional[str] = None,
        issue_type: str = "Service Request",
        resolve_transition_id: str = "31",
        timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
        retries: int = 3,
    ) -> None:
        self.url = (url or os.environ.get("JIRA_URL") or "").rstrip("/")
        self.email = email or os.environ.get("JIRA_EMAIL")
        self.api_token = api_token or os.environ.get("JIRA_API_TOKEN")
        self.project_key = project_key or os.environ.get("JIRA_PROJECT_KEY")
        self.issue_type = issue_type
        self.resolve_transition_id = resolve_transition_id
        self.timeout = timeout
        retries = retries

        if not self.url:
            raise AdapterConfigError(
                "Jira base URL is required. Set JIRA_URL=https://yourcompany.atlassian.net "
                "(or pass url=...)."
            )
        if not self.email or not self.api_token:
            raise AdapterConfigError(
                "Jira credentials are missing. Provide JIRA_EMAIL and JIRA_API_TOKEN "
                "(an API token -- never the account password)."
            )

    # -- HTTP plumbing ---------------------------------------------------- #

    def _headers(self) -> Dict[str, str]:
        """Return the HTTP headers used against Jira."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _auth(self) -> Tuple[str, str]:
        """Return the basic auth pair (e-mail + API token)."""
        return (self.email or "", self.api_token or "")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Perform an authenticated request against Jira.

        Args:
            method: HTTP verb.
            path: Path relative to the base URL.
            params: Optional query string parameters.
            json_body: Optional JSON body.

        Returns:
            The parsed response body.
        """
        url = "{0}{1}".format(self.url, path)
        return http_request(
            method,
            url,
            auth=self._auth(),
            headers=self._headers(),
            params=params,
            json_body=json_body,
            timeout=self.timeout,
            retries=self.retries,
            connector_name="JiraITSM",
        )

    # -- mapping ---------------------------------------------------------- #

    @staticmethod
    def adf_to_text(node: Any) -> str:
        """Flatten an Atlassian Document Format node into plain text.

        Args:
            node: Any ADF node (dictionary, list or scalar).

        Returns:
            The concatenated text of the document.
        """
        if node is None:
            return ""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return " ".join(JiraITSM.adf_to_text(item) for item in node).strip()
        if isinstance(node, dict):
            if "text" in node and isinstance(node["text"], str):
                return node["text"]
            if "content" in node:
                return JiraITSM.adf_to_text(node["content"])
        return ""

    def _map_ticket(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        """Map a Jira issue to the canonical ticket shape.

        Args:
            issue: Raw Jira issue.

        Returns:
            The canonical ticket dictionary.
        """
        fields = issue.get("fields", {}) or {}
        reporter = fields.get("reporter", {}) or {}
        requester = str(reporter.get("emailAddress", "") or "")
        if requester.lower().startswith("upn:"):
            requester = requester[4:]
        labels = fields.get("labels", []) or []
        service = labels[0] if labels else ""
        priority = fields.get("priority", {}) or {}
        status = fields.get("status", {}) or {}
        return {
            "ticket_id": str(issue.get("key", "") or issue.get("id", "")),
            "title": str(fields.get("summary", "") or ""),
            "description": self.adf_to_text(fields.get("description")),
            "requester": requester,
            "service": str(service),
            "priority": str(priority.get("name", "") or "P3"),
            "status": str(status.get("name", "") or "new"),
            "created_at": str(fields.get("created", "") or ""),
            "source": "jira",
            "raw": sanitize_secrets(issue),
        }

    # -- read operations -------------------------------------------------- #

    def get_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """Return a Jira issue.

        Args:
            ticket_id: Issue key (``ITS-123``).

        Returns:
            The canonical ticket dictionary.

        Raises:
            AdapterNotFoundError: When the issue does not exist.
        """
        payload = self._request(
            "GET",
            "/rest/api/3/issue/{0}".format(ticket_id),
            params={"fields": "summary,description,priority,status,reporter,created,labels"},
        )
        if not payload:
            raise AdapterNotFoundError(
                "Jira issue {0} does not exist".format(ticket_id),
                {"ticket_id": ticket_id},
            )
        return self._map_ticket(payload)

    def list_tickets(
        self,
        limit: int = 50,
        offset: int = 0,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search the Jira issues of the configured project.

        Args:
            limit: Maximum number of issues (``maxResults``).
            offset: Number of issues to skip (``startAt``).
            query: Optional extra JQL clause.

        Returns:
            The list of canonical ticket dictionaries.
        """
        jql = "project={0}".format(self.project_key or "")
        if query:
            jql = "{0} AND {1}".format(jql, query)
        payload = self._request(
            "GET",
            "/rest/api/3/search",
            params={"jql": jql, "maxResults": limit, "startAt": offset},
        )
        issues = payload.get("issues", []) or []
        return [self._map_ticket(issue) for issue in issues if isinstance(issue, dict)]

    # -- write operations ------------------------------------------------- #

    def _skipped(self, ticket_id: str, operation: str) -> Dict[str, Any]:
        """Return the standard "writes are disabled" answer.

        Args:
            ticket_id: Identifier of the ticket.
            operation: Name of the operation.

        Returns:
            The operation result dictionary.
        """
        return {
            "ticket_id": ticket_id,
            "status": "skipped",
            "reason": "writes_disabled",
            "operation": operation,
            "external_id": None,
            "timestamp": utc_now_iso(),
        }

    def add_comment(self, ticket_id: str, comment: str) -> Dict[str, Any]:
        """Add a comment to a Jira issue.

        Args:
            ticket_id: Issue key.
            comment: Body of the comment (converted to ADF).

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "add_comment")
        payload = self._request(
            "POST",
            "/rest/api/3/issue/{0}/comment".format(ticket_id),
            json_body={
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": comment}],
                        }
                    ],
                }
            },
        )
        return {
            "ticket_id": ticket_id,
            "status": "comment_added",
            "external_id": str(payload.get("id", "")) or None,
            "timestamp": utc_now_iso(),
            "comment": comment,
        }

    def assign(self, ticket_id: str, queue: str) -> Dict[str, Any]:
        """Assign a Jira issue to a user.

        Args:
            ticket_id: Issue key.
            queue: Account identifier of the assignee.  In production the
                queue name is resolved to an ``accountId`` before the call.

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "assign")
        payload = self._request(
            "PUT",
            "/rest/api/3/issue/{0}/assignee".format(ticket_id),
            json_body={"accountId": queue},
        )
        return {
            "ticket_id": ticket_id,
            "status": "assigned",
            "external_id": str(payload.get("accountId", queue)) or None,
            "timestamp": utc_now_iso(),
            "queue": queue,
        }

    def set_priority(self, ticket_id: str, priority: str) -> Dict[str, Any]:
        """Update the priority of a Jira issue.

        Args:
            ticket_id: Issue key.
            priority: Canonical priority (``P1``..``P4``).

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "set_priority")
        self._request(
            "PUT",
            "/rest/api/3/issue/{0}".format(ticket_id),
            json_body={"fields": {"priority": {"name": priority}}},
        )
        return {
            "ticket_id": ticket_id,
            "status": "priority_updated",
            "external_id": ticket_id,
            "timestamp": utc_now_iso(),
            "priority": priority,
        }

    def resolve(self, ticket_id: str, resolution: str) -> Dict[str, Any]:
        """Move a Jira issue to the resolved transition.

        Args:
            ticket_id: Issue key.
            resolution: Resolution text (added as a comment first).

        Returns:
            The operation result dictionary.
        """
        if not writes_enabled():
            return self._skipped(ticket_id, "resolve")
        self.add_comment(ticket_id, resolution)
        self._request(
            "POST",
            "/rest/api/3/issue/{0}/transitions".format(ticket_id),
            json_body={"transition": {"id": self.resolve_transition_id}},
        )
        return {
            "ticket_id": ticket_id,
            "status": "resolved",
            "external_id": ticket_id,
            "timestamp": utc_now_iso(),
            "resolution": resolution,
        }


__all__ = [
    "DEFAULT_TICKETS_PATH",
    "JiraITSM",
    "MockITSM",
    "ServiceNowITSM",
]
