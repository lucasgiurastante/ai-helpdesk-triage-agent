"""Shared helpers, constants and base types for the AI Help Desk agent.

Design constraints (SPEC):
  * standard library only -- the whole runtime must work without ``pip install``;
  * ISO-8601 UTC timestamps with a trailing ``Z`` and no microseconds;
  * deterministic text normalisation (accents, punctuation, light stemming) so
    that KB matching is reproducible across runs and machines;
  * no mutable module level state: every piece of state lives in a class or is
    passed explicitly.
"""

import csv
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Repository root -- every path is resolved from here, never from the CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Risk levels ordered from the lowest to the highest.
RISK_ORDER: Tuple[str, ...] = ("low", "medium", "high", "critical")

#: Priority -> urgency weight (P1 is the most urgent).
PRIORITY_WEIGHT: Dict[str, int] = {"P4": 1, "P3": 2, "P2": 3, "P1": 4}

#: Timeout (minutes) per approver role -- SPEC section 3.5.7.
APPROVAL_TIMEOUT_MINUTES: Dict[str, int] = {
    "support_lead": 240,
    "manager": 480,
    "security": 60,
    "on_call": 60,
}

#: Fixed audit event enumeration -- SPEC section 3.6.
AUDIT_EVENTS: Tuple[str, ...] = (
    "run_started",
    "ticket_received",
    "triage_completed",
    "identity_checked",
    "policy_decided",
    "action_proposed",
    "approval_created",
    "approval_resolved",
    "action_executed",
    "action_failed",
    "escalation_raised",
    "itsm_writeback",
    "integration_error",
    "run_completed",
)

#: Canonical categories -- SPEC section 2.1.
CATEGORIES: Tuple[str, ...] = (
    "access",
    "account_locked",
    "password",
    "network",
    "vpn",
    "software",
    "hardware",
    "email",
    "security",
    "unknown",
)

#: Services the agent is able to reason about -- SPEC section 3.3.6.
KNOWN_SERVICES: Tuple[str, ...] = (
    "SAP Fiori",
    "Shared Drive",
    "Network",
    "Software",
    "Security",
    "VPN",
    "Email",
    "Hardware",
    "Account",
)

#: Default impact x urgency -> priority matrix -- SPEC section 3.3.5.
DEFAULT_PRIORITY_MATRIX: Dict[str, Dict[str, str]] = {
    "individual": {"low": "P4", "normal": "P3", "high": "P2", "critical": "P1"},
    "team": {"low": "P4", "normal": "P3", "high": "P2", "critical": "P1"},
    "department": {"low": "P3", "normal": "P2", "high": "P1", "critical": "P1"},
    "organization": {"low": "P2", "normal": "P1", "high": "P1", "critical": "P1"},
}

#: Classification confidence thresholds -- SPEC section 3.3.3.
AUTO_EXECUTE_MIN_CONFIDENCE = 0.70
REVIEW_MIN_CONFIDENCE = 0.45

#: Verbs that express a *request* (intent="request") -- SPEC section 2.1.
#: Grammatical variants of the canonical list are accepted.
REQUEST_VERBS: Tuple[str, ...] = (
    "solicito",
    "solicita",
    "solicitar",
    "necesito",
    "necesita",
    "necesito",
    "quiero",
    "quiere",
    "queria",
    "acceder a",
    "acceso a",
    "permiso",
    "alta",
    "habilitar",
    "habilitacion",
)

#: Verbs that express a *failure* (intent="trouble") -- SPEC section 2.1.
TROUBLE_VERBS: Tuple[str, ...] = (
    "no puedo",
    "no puede",
    "no podemos",
    "falla",
    "fallo",
    "error",
    "dice",
    "bloqueado",
    "bloqueada",
    "no funciona",
)

#: Scope phrases used by the impact heuristic -- SPEC section 3.3.5.
ORGANIZATION_SCOPE: Tuple[str, ...] = (
    "toda la empresa",
    "todos los usuarios",
    "sitio completo",
)

DEPARTMENT_SCOPE: Tuple[str, ...] = (
    "todo el piso",
    "todo el departamento",
    "todo el equipo",
)

#: Regex used to spot user-like tokens ("juan.perez") inside free text.
MENTIONED_USER_RE = re.compile(r"\b([a-z]+(?:[._][a-z]+)+)\b")

#: Regex used to spot e-mail addresses inside free text.
EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")


# --------------------------------------------------------------------------- #
# Paths and serialisation helpers
# --------------------------------------------------------------------------- #

def repo_path(*parts: str) -> str:
    """Return an absolute path inside the repository root.

    Args:
        *parts: Path components relative to the repository root.

    Returns:
        The absolute path as a string.
    """
    return str(REPO_ROOT.joinpath(*parts))


def ensure_directory(path: str) -> None:
    """Create the parent directory of ``path`` when it does not exist."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def utc_now_iso() -> str:
    """Return the current UTC timestamp as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def iso_from(moment: datetime) -> str:
    """Format a :class:`datetime` as an ISO-8601 UTC timestamp with ``Z``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, accepting the ``Z`` suffix.

    Args:
        value: Timestamp to parse. ``None`` or an empty string yields ``None``.

    Returns:
        A timezone aware :class:`datetime` or ``None`` when unparsable.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def shift_iso(value: str, minutes: int) -> str:
    """Return ``value`` shifted by ``minutes`` minutes (ISO-8601 UTC)."""
    base = parse_iso(value) or datetime.now(timezone.utc)
    return iso_from(base + timedelta(minutes=minutes))


def load_json(path: str) -> Any:
    """Load a JSON document from disk.

    Args:
        path: Absolute or relative path to the JSON file.

    Returns:
        The decoded document.

    Raises:
        OSError: The file does not exist or cannot be read.
        json.JSONDecodeError: The file is not valid JSON.
    """
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: str, data: Any) -> None:
    """Persist ``data`` as UTF-8 JSON (indent 2, no ASCII escaping).

    Args:
        path: Destination file. Parent directories are created when needed.
        data: Any JSON serialisable object.
    """
    ensure_directory(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def read_csv(path: str) -> List[Dict[str, str]]:
    """Read a CSV file into a list of dictionaries.

    Args:
        path: Path to the CSV file.

    Returns:
        One dictionary per row, with stripped cell values.
    """
    rows: List[Dict[str, str]] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row: Dict[str, str] = {}
            for key, value in raw.items():
                if key is None:
                    continue
                row[key.strip()] = value.strip() if isinstance(value, str) else value
            rows.append(row)
    return rows


def write_csv(
    path: str, rows: Sequence[Dict[str, Any]], fieldnames: Optional[Sequence[str]] = None
) -> None:
    """Write rows to a CSV file.

    Args:
        path: Destination file. Parent directories are created when needed.
        rows: Dictionaries to serialise.
        fieldnames: Fixed column order. When omitted the union of the row keys
            (first-seen order) is used.
    """
    ensure_directory(path)
    columns: List[str] = list(fieldnames) if fieldnames else []
    if not columns:
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def safe_get(source: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path out of nested dictionaries/lists.

    A literal key wins over a nested lookup, so flat policy contexts such as
    ``{"resource.sensitive": True}`` are reachable with the same helper.

    Args:
        source: Object to inspect.
        path: Dotted path, e.g. ``"user.vip"``.
        default: Value returned when the path cannot be resolved.

    Returns:
        The resolved value or ``default``.
    """
    if source is None:
        return default
    if isinstance(source, dict) and path in source:
        return source[path]
    current = source
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            current = getattr(current, part, None)
            if current is None:
                return default
    return current


def sha256_hex(text: str) -> str:
    """Return the hexadecimal SHA-256 digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    """Return the first ``length`` characters of the SHA-256 digest of ``text``."""
    return sha256_hex(text)[:length]


# --------------------------------------------------------------------------- #
# Text normalisation
# --------------------------------------------------------------------------- #

def normalize_text(text: Optional[str]) -> str:
    """Normalise free text for keyword matching.

    The pipeline is lower-case -> accent removal -> punctuation to spaces ->
    whitespace collapse. It is idempotent, so it can be applied to already
    normalised values without changing them.

    Args:
        text: Raw text (may be ``None``).

    Returns:
        The normalised text.
    """
    if not text:
        return ""
    lowered = text.lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", cleaned).strip()


def singularize(token: str) -> str:
    """Apply the light stemming rules described in SPEC section 3.3.1.

    The suffix ``es`` is removed when the token is longer than four characters
    and the suffix ``s`` afterwards when the token is longer than three. The
    rule is applied to the ticket text and to the KB keywords, which makes the
    matching insensitive to Spanish/English plurals.

    Args:
        token: A single already normalised token.

    Returns:
        The singularised token.
    """
    if len(token) > 4 and token.endswith("es"):
        token = token[:-2]
    if len(token) > 3 and token.endswith("s"):
        token = token[:-1]
    return token


def tokenize(text: Optional[str]) -> List[str]:
    """Split normalised text into singularised tokens.

    Args:
        text: Raw or normalised text.

    Returns:
        The list of tokens (may be empty).
    """
    normalized = normalize_text(text)
    if not normalized:
        return []
    return [singularize(token) for token in normalized.split(" ") if token]


def contains_sequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """Return ``True`` when ``needle`` appears contiguously inside ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return False
    span = len(needle)
    for index in range(len(haystack) - span + 1):
        if list(haystack[index : index + span]) == list(needle):
            return True
    return False


# --------------------------------------------------------------------------- #
# Risk / priority helpers
# --------------------------------------------------------------------------- #

def risk_index(risk: Optional[str]) -> int:
    """Return the ordinal of a risk level (``-1`` when unknown)."""
    if not risk:
        return -1
    try:
        return RISK_ORDER.index(risk)
    except ValueError:
        return -1


def max_risk(*risks: Optional[str]) -> str:
    """Return the highest of the given risk levels.

    Unknown or missing levels are ignored; when nothing is left the default is
    ``medium`` (SPEC section 3.4.1).
    """
    known = [risk for risk in risks if risk_index(risk) >= 0]
    if not known:
        return "medium"
    return max(known, key=risk_index)


def escalate_risk(risk: str, steps: int = 1) -> str:
    """Raise a risk level by ``steps`` levels, saturating at ``critical``."""
    index = max(risk_index(risk), 0)
    target = min(index + max(steps, 0), len(RISK_ORDER) - 1)
    return RISK_ORDER[target]


def most_urgent_priority(*priorities: Optional[str]) -> str:
    """Return the most urgent priority among the given values."""
    known = [p for p in priorities if p in PRIORITY_WEIGHT]
    if not known:
        return "P3"
    return max(known, key=lambda p: PRIORITY_WEIGHT[p])


# --------------------------------------------------------------------------- #
# Base types
# --------------------------------------------------------------------------- #

@dataclass
class TriageResult:
    """Structured output of :meth:`services.triage.TriageService.classify`."""

    ticket_id: str
    requester: str
    service: str
    normalized_text: str
    intent: str
    category: str
    priority: str
    impact: str
    urgency: str
    confidence: float
    kb_id: Optional[str]
    matched_keywords: List[str] = field(default_factory=list)
    matched_strong_keywords: List[str] = field(default_factory=list)
    score_breakdown: Dict[str, Any] = field(default_factory=dict)
    suggested_action: str = "human_review"
    suggested_resolution: str = ""
    base_risk: str = "medium"
    entities: Dict[str, Any] = field(default_factory=dict)
    alternatives: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TriageResult":
        """Build a :class:`TriageResult` from the raw ``classify`` dictionary."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in payload.items() if k in known})

    def to_dict(self) -> Dict[str, Any]:
        """Return the dataclass as a plain dictionary."""
        return asdict(self)


@dataclass
class PolicyDecision:
    """Structured output of :meth:`services.policy.PolicyEngine.evaluate`."""

    decision: str
    requires_approval: bool
    risk: str
    base_risk: str
    risk_factors: List[Dict[str, Any]] = field(default_factory=list)
    effective_action: str = "human_review"
    approver_role: Optional[str] = None
    escalation_queue: Optional[str] = None
    confidence: float = 0.0
    timeout_minutes: int = 60
    reasons: List[str] = field(default_factory=list)
    rule_ids: List[str] = field(default_factory=list)
    policy_version: str = "unknown"

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PolicyDecision":
        """Build a :class:`PolicyDecision` from the raw ``evaluate`` dictionary."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in payload.items() if k in known})

    def to_dict(self) -> Dict[str, Any]:
        """Return the dataclass as a plain dictionary."""
        return asdict(self)


@dataclass
class ApprovalRequest:
    """Input of :meth:`services.approval.ApprovalQueue.create`."""

    ticket_id: str
    requester: str
    action: str
    resource: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    risk: str = "low"
    decision: str = "require_approval"
    approver_role: str = "support_lead"
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    run_id: str = ""
    target: str = ""
    action_id: str = ""
    timeout_minutes: Optional[int] = None

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ApprovalRequest":
        """Build an :class:`ApprovalRequest` from a plain dictionary."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in payload.items() if k in known})

    def to_dict(self) -> Dict[str, Any]:
        """Return the dataclass as a plain dictionary."""
        return asdict(self)


@dataclass
class AuditEvent:
    """A single structured audit record (one JSONL line)."""

    ts: str
    run_id: str
    event: str
    ticket_id: Optional[str] = None
    actor: str = "agent"
    fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return the flat dictionary written to ``outputs/audit.log``."""
        payload: Dict[str, Any] = {
            "ts": self.ts,
            "run_id": self.run_id,
            "event": self.event,
            "ticket_id": self.ticket_id,
            "actor": self.actor,
        }
        payload.update(self.fields)
        return payload


__all__ = [
    "AUTO_EXECUTE_MIN_CONFIDENCE",
    "AUDIT_EVENTS",
    "APPROVAL_TIMEOUT_MINUTES",
    "ApprovalRequest",
    "AuditEvent",
    "CATEGORIES",
    "DEFAULT_PRIORITY_MATRIX",
    "KNOWN_SERVICES",
    "PolicyDecision",
    "PRIORITY_WEIGHT",
    "REPO_ROOT",
    "REVIEW_MIN_CONFIDENCE",
    "RISK_ORDER",
    "TriageResult",
    "contains_sequence",
    "ensure_directory",
    "escalate_risk",
    "iso_from",
    "load_json",
    "max_risk",
    "most_urgent_priority",
    "normalize_text",
    "parse_iso",
    "read_csv",
    "repo_path",
    "risk_index",
    "safe_get",
    "save_json",
    "sha256_hex",
    "shift_iso",
    "short_hash",
    "singularize",
    "tokenize",
    "utc_now_iso",
    "write_csv",
]
