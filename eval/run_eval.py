"""Evaluation harness of the AI Help Desk agent.

The harness runs the **real** pipeline (triage + identity + policy) over a
labelled set of tickets and measures how well the agent classifies,
prioritises, proposes the right action and takes the right decision
(SPEC sections 3.8, 4.9 and 5).

Guarantees of the harness:

* it never touches the network, never executes an action and never writes
  to the ITSM -- only the mock identity adapter is used;
* it is deterministic: the same files always produce the same report (only
  the report timestamp changes);
* it fails loudly: when a metric drops below the acceptance threshold the
  details are printed and the process exits with code ``1``.

Usage::

    python3 eval/run_eval.py
    python3 eval/run_eval.py --input eval/labeled_tickets.json --verbose
    python3 -m eval.run_eval
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # pragma: no cover - package import shim
    from .metrics import check_thresholds, summarize
except ImportError:  # pragma: no cover - executed as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from eval.metrics import check_thresholds, summarize

#: Repository root -- every relative path is resolved from here.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Default labelled set.
DEFAULT_INPUT = "eval/labeled_tickets.json"

#: Default Markdown report.
DEFAULT_OUT_MD = "outputs/eval_report.md"

#: Default JSON report.
DEFAULT_OUT_JSON = "outputs/eval_report.json"

#: Acceptance thresholds (SPEC section 3.8).
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "category_accuracy": 0.85,
    "priority_accuracy": 0.80,
    "action_accuracy": 0.75,
    "decision_accuracy": 0.80,
}

#: Identity context used when the requester is unknown to the directory.
DEFAULT_USER: Dict[str, Any] = {
    "username": "",
    "email": "",
    "display_name": "",
    "department": "",
    "active": True,
    "locked": False,
    "locked_by_admin": False,
    "groups": [],
    "manager": None,
    "vip": False,
    "mfa_enrolled": True,
    "identity_verified": True,
    "password_last_set_days": 0,
    "source": "default",
}


def _resolve_path(path: str) -> Path:
    """Resolve a path against the repository root when it is relative.

    Args:
        path: Absolute or relative path.

    Returns:
        The resolved :class:`pathlib.Path`.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate.resolve()
    return (REPO_ROOT / candidate).resolve()


def _load_json(path: str) -> Any:
    """Load a JSON document from disk.

    Args:
        path: Path of the file.

    Returns:
        The decoded document.

    Raises:
        FileNotFoundError: When the file does not exist.
        ValueError: When the file does not hold valid JSON.
    """
    target = _resolve_path(path)
    if not target.is_file():
        raise FileNotFoundError("File not found: {0}".format(target))
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_labeled(path: str = DEFAULT_INPUT) -> List[Dict[str, Any]]:
    """Load the labelled evaluation set.

    Args:
        path: Path of the labelled set (relative paths are resolved against
            the repository root).

    Returns:
        The list of labelled tickets.

    Raises:
        FileNotFoundError: When the file does not exist.
        ValueError: When the file does not hold a valid labelled set.
    """
    payload = _load_json(path)
    if isinstance(payload, dict):
        tickets = payload.get("tickets", [])
    else:
        tickets = payload
    if not isinstance(tickets, list):
        raise ValueError("Malformed labelled set in {0}: expected a list of tickets".format(path))
    return [ticket for ticket in tickets if isinstance(ticket, dict)]


def _import_runtime() -> Tuple[Any, Any]:
    """Import the triage and policy services of the runtime.

    The import is lazy so that the harness can be inspected (and its metric
    helpers unit tested) even before the runtime exists.

    Returns:
        The ``(TriageService, PolicyEngine)`` classes.

    Raises:
        ImportError: When the runtime is not available yet.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from services.policy import PolicyEngine  # noqa: PLC0415 -- lazy on purpose
    from services.triage import TriageService  # noqa: PLC0415 -- lazy on purpose

    return TriageService, PolicyEngine


def _build_runtime() -> Tuple[Any, Any]:
    """Build the triage and policy services from the runtime data files.

    Returns:
        A ``(triage_service, policy_engine)`` tuple.

    Raises:
        FileNotFoundError: When a runtime data file is missing.
        ImportError: When the runtime services are not available yet.
    """
    triage_class, policy_class = _import_runtime()

    kb_payload = _load_json("data/kb.json")
    if isinstance(kb_payload, dict):
        kb_articles = kb_payload.get("articles", [])
    else:
        kb_articles = kb_payload
    if not isinstance(kb_articles, list) or not kb_articles:
        raise FileNotFoundError("data/kb.json does not contain any article")

    risk_matrix = _load_json("policies/risk_matrix.json")
    access_policies = _load_json("policies/access_policies.json")
    escalation_rules = _load_json("policies/escalation_rules.json")
    if isinstance(escalation_rules, dict):
        escalation_rules = escalation_rules.get("rules", [])

    try:
        triage_service = triage_class(kb=kb_articles)
    except TypeError:
        triage_service = triage_class(kb_articles)
    try:
        policy_engine = policy_class(
            risk_matrix=risk_matrix,
            access_policies=access_policies,
            escalation_rules=escalation_rules,
        )
    except TypeError:
        policy_engine = policy_class(risk_matrix, access_policies, escalation_rules)
    return triage_service, policy_engine


def _build_identity() -> Any:
    """Build the mock identity adapter used by the harness.

    Returns:
        The identity adapter, or ``None`` when the mock directory cannot be
        loaded (the harness then falls back to the scenario profiles).
    """
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from adapters.identity import MockIdentity  # noqa: PLC0415 -- lazy on purpose

        return MockIdentity()
    except Exception:  # pragma: no cover - defensive, the mock is optional
        return None


def _resolve_user(identity: Any, ticket: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Build the identity context of a ticket.

    The directory (``data/users.json``) is the source of truth; the
    ``user_profile`` of the labelled ticket describes the *scenario* (VIP,
    without MFA, inactive...) and is applied on top of it.  When the
    requester does not exist in the directory the scenario profile is used
    alone, which keeps the labelled set self contained.

    Args:
        identity: The identity adapter (may be ``None``).
        ticket: The labelled ticket.

    Returns:
        A ``(user, source)`` tuple.
    """
    username = str(ticket.get("requester", "") or "")
    profile = ticket.get("user_profile") or {}
    base: Dict[str, Any] = {}
    source = "scenario"
    if identity is not None:
        try:
            base = dict(identity.get_user(username))
            source = "directory"
        except Exception:
            base = {}
    merged: Dict[str, Any] = dict(base) if base else dict(DEFAULT_USER)
    merged.update({key: value for key, value in profile.items() if value is not None})
    merged["username"] = username or merged.get("username", "")
    if base and profile:
        source = "directory+scenario"
    elif not base and not profile:
        source = "default"
    return merged, source


def run_evaluation(
    labeled: List[Dict[str, Any]],
    adapters: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Run the pipeline over the labelled set and compute the metrics.

    Only triage, identity and policy are exercised: no action is executed
    and nothing is written to the ITSM (SPEC section 3.8).

    Args:
        labeled: The labelled tickets (see :func:`load_labeled`).
        adapters: Optional mapping with an ``identity`` connector.  When it
            is missing the mock identity adapter is built automatically.
        **kwargs: Optional ``triage_service`` and ``policy_engine`` overrides
            (used by the unit tests to inject fakes).

    Returns:
        The report dictionary: metadata, metrics, per-ticket results and
        the threshold breaches.
    """
    triage_service = kwargs.get("triage_service")
    policy_engine = kwargs.get("policy_engine")
    if triage_service is None or policy_engine is None:
        triage_service, policy_engine = _build_runtime()

    identity = None
    if adapters:
        identity = adapters.get("identity")
    if identity is None:
        identity = _build_identity()

    results: List[Dict[str, Any]] = []
    for ticket in labeled:
        ticket_id = str(ticket.get("ticket_id", "") or "")
        requester = str(ticket.get("requester", "") or "")
        description = str(ticket.get("description", "") or "")
        service = str(ticket.get("service", "") or "")
        expected = dict(ticket.get("expected") or {})

        payload = {
            "ticket_id": ticket_id,
            "title": description[:80],
            "description": description,
            "requester": requester,
            "service": service,
            "created_at": str(ticket.get("created_at", "") or ""),
        }

        triage = triage_service.classify(payload)
        user, identity_source = _resolve_user(identity, ticket)
        decision = policy_engine.evaluate(triage, user)

        actual = {
            "category": triage.get("category"),
            "intent": triage.get("intent"),
            "priority": triage.get("priority"),
            "action": triage.get("suggested_action"),
            "decision": decision.get("decision"),
            "risk": decision.get("risk"),
            "confidence": triage.get("confidence"),
            "kb_id": triage.get("kb_id"),
            "impact": triage.get("impact"),
            "urgency": triage.get("urgency"),
            "effective_action": decision.get("effective_action"),
            "approver_role": decision.get("approver_role"),
            "escalation_queue": decision.get("escalation_queue"),
            "reasons": decision.get("reasons", []),
            "rule_ids": decision.get("rule_ids", []),
        }

        results.append(
            {
                "ticket_id": ticket_id,
                "description": description,
                "requester": requester,
                "service": service,
                "expected": expected,
                "actual": actual,
                "identity_source": identity_source,
                "notes": ticket.get("notes", ""),
            }
        )

    metrics = summarize(results)
    return {
        "version": "1.0",
        "generated_at": _utc_now(),
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "metrics": metrics,
        "results": results,
        "breaches": check_thresholds(metrics, DEFAULT_THRESHOLDS),
        "passed": not check_thresholds(metrics, DEFAULT_THRESHOLDS),
    }


def _utc_now() -> str:
    """Return the current UTC timestamp as ``YYYY-MM-DDTHH:MM:SSZ``."""
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _render_matrix(matrix: Dict[str, Dict[str, int]]) -> List[str]:
    """Render a confusion matrix as Markdown lines.

    Args:
        matrix: The confusion matrix.

    Returns:
        The list of Markdown lines.
    """
    if not matrix:
        return ["_no data_"]
    labels = sorted(matrix)
    lines = ["| expected \\ predicted | " + " | ".join(labels) + " |"]
    lines.append("|---" * (len(labels) + 1) + "|")
    for row in labels:
        cells = [str(matrix[row].get(column, 0)) for column in labels]
        lines.append("| **{0}** | {1} |".format(row, " | ".join(cells)))
    return lines


def render_markdown(report: Dict[str, Any]) -> str:
    """Render the report as Markdown.

    Args:
        report: The report produced by :func:`run_evaluation`.

    Returns:
        The Markdown document.
    """
    metrics = report.get("metrics", {})
    thresholds = report.get("thresholds", {})
    lines: List[str] = []
    lines.append("# AI Help Desk Agent -- Evaluation Report")
    lines.append("")
    lines.append("Generated at: `{0}`".format(report.get("generated_at", "")))
    lines.append("")
    status = "PASSED" if report.get("passed") else "FAILED"
    lines.append("**Gate: {0}** -- tickets evaluated: **{1}**".format(status, metrics.get("total", 0)))
    lines.append("")
    sources: Dict[str, int] = {}
    for result in report.get("results", []):
        key = str(result.get("identity_source", "unknown"))
        sources[key] = sources.get(key, 0) + 1
    if sources:
        lines.append(
            "Identity source: "
            + ", ".join("{0} ({1})".format(key, value) for key, value in sorted(sources.items()))
            + " -- `directory` = resolved in data/users.json, `scenario` = the labelled profile of the ticket."
        )
        lines.append("")

    lines.append("## Global metrics")
    lines.append("")
    lines.append("| metric | value | threshold | status |")
    lines.append("|---|---|---|---|")
    for metric in ("category_accuracy", "priority_accuracy", "action_accuracy", "decision_accuracy"):
        threshold = float(thresholds.get(metric, 0.0))
        value = float(metrics.get(metric, 0.0))
        state = "ok" if value >= threshold else "**below threshold**"
        lines.append("| `{0}` | {1:.4f} | {2:.2f} | {3} |".format(metric, value, threshold, state))
    lines.append("")
    lines.append("| supporting metric | value |")
    lines.append("|---|---|")
    for metric in (
        "risk_accuracy",
        "intent_accuracy",
        "auto_resolution_rate",
        "approval_rate",
        "escalation_rate",
        "escalation_recall",
        "auto_resolution_recall",
        "avg_confidence",
        "failure_count",
    ):
        lines.append("| `{0}` | {1} |".format(metric, metrics.get(metric, 0)))
    lines.append("")

    lines.append("## Category confusion matrix")
    lines.append("")
    lines.extend(_render_matrix(metrics.get("category_confusion", {})))
    lines.append("")
    lines.append("## Priority confusion matrix")
    lines.append("")
    lines.extend(_render_matrix(metrics.get("priority_confusion", {})))
    lines.append("")
    lines.append("## Decision confusion matrix")
    lines.append("")
    lines.extend(_render_matrix(metrics.get("decision_confusion", {})))
    lines.append("")

    lines.append("## Breakdown by expected category")
    lines.append("")
    lines.append("| category | tickets | category acc. | priority acc. | action acc. | decision acc. |")
    lines.append("|---|---|---|---|---|---|")
    for group, entry in sorted((metrics.get("breakdown_by_category") or {}).items()):
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                group,
                entry.get("count", 0),
                entry.get("category_accuracy", 0),
                entry.get("priority_accuracy", 0),
                entry.get("action_accuracy", 0),
                entry.get("decision_accuracy", 0),
            )
        )
    lines.append("")

    lines.append("## Breakdown by expected risk")
    lines.append("")
    lines.append("| risk | tickets | category acc. | priority acc. | action acc. | decision acc. |")
    lines.append("|---|---|---|---|---|---|")
    for group, entry in sorted((metrics.get("breakdown_by_risk") or {}).items()):
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                group,
                entry.get("count", 0),
                entry.get("category_accuracy", 0),
                entry.get("priority_accuracy", 0),
                entry.get("action_accuracy", 0),
                entry.get("decision_accuracy", 0),
            )
        )
    lines.append("")

    failures = metrics.get("failures", [])
    lines.append("## Failures ({0})".format(len(failures)))
    lines.append("")
    if not failures:
        lines.append("_No failure: every ticket matched the expected category, priority, action and decision._")
        lines.append("")
    else:
        lines.append("| ticket | mismatched field | expected | actual | confidence |")
        lines.append("|---|---|---|---|---|")
        for failure in failures:
            for item in failure.get("mismatched_fields", []):
                lines.append(
                    "| {0} | {1} | {2} | {3} | {4} |".format(
                        failure.get("ticket_id", ""),
                        item.get("field", ""),
                        item.get("expected", ""),
                        item.get("actual", ""),
                        failure.get("confidence", ""),
                    )
                )
        lines.append("")
        lines.append("### Detail")
        lines.append("")
        for failure in failures:
            lines.append("- **{0}** -- {1}".format(
                failure.get("ticket_id", ""), failure.get("description_of_failure", "")
            ))
            lines.append("  - ticket: {0}".format(failure.get("description", "")))
        lines.append("")

    lines.append("## Per ticket results")
    lines.append("")
    lines.append("| ticket | service | expected | actual | confidence | identity source |")
    lines.append("|---|---|---|---|---|---|")
    for result in report.get("results", []):
        expected = result.get("expected") or {}
        actual = result.get("actual") or {}
        lines.append(
            "| {0} | {1} | {2}/{3}/{4}/{5} | {6}/{7}/{8}/{9} | {10} | {11} |".format(
                result.get("ticket_id", ""),
                result.get("service", ""),
                expected.get("category", ""),
                expected.get("priority", ""),
                expected.get("action", ""),
                expected.get("decision", ""),
                actual.get("category", ""),
                actual.get("priority", ""),
                actual.get("action", ""),
                actual.get("decision", ""),
                actual.get("confidence", ""),
                result.get("identity_source", ""),
            )
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_reports(report: Dict[str, Any], md_path: str, json_path: str) -> None:
    """Write the Markdown and JSON reports.

    Args:
        report: The report produced by :func:`run_evaluation`.
        md_path: Destination of the Markdown report.
        json_path: Destination of the JSON report.
    """
    md_target = _resolve_path(md_path)
    json_target = _resolve_path(json_path)
    for target in (md_target, json_target):
        parent = target.parent
        if parent and not parent.is_dir():
            parent.mkdir(parents=True, exist_ok=True)
    md_target.write_text(render_markdown(report), encoding="utf-8")
    with json_target.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command line parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description="Evaluate the AI Help Desk triage agent over a labelled set of tickets.",
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Path of the labelled set")
    parser.add_argument("--out-md", default=DEFAULT_OUT_MD, help="Path of the Markdown report")
    parser.add_argument("--out-json", default=DEFAULT_OUT_JSON, help="Path of the JSON report")
    parser.add_argument(
        "--min-category-accuracy",
        type=float,
        default=DEFAULT_THRESHOLDS["category_accuracy"],
        help="Minimum category accuracy",
    )
    parser.add_argument(
        "--min-priority-accuracy",
        type=float,
        default=DEFAULT_THRESHOLDS["priority_accuracy"],
        help="Minimum priority accuracy",
    )
    parser.add_argument(
        "--min-action-accuracy",
        type=float,
        default=DEFAULT_THRESHOLDS["action_accuracy"],
        help="Minimum action accuracy",
    )
    parser.add_argument(
        "--min-decision-accuracy",
        type=float,
        default=DEFAULT_THRESHOLDS["decision_accuracy"],
        help="Minimum decision accuracy",
    )
    parser.add_argument("--json-only", action="store_true", help="Only print the JSON report")
    parser.add_argument("--verbose", action="store_true", help="Print one line per evaluated ticket")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point of the harness.

    Args:
        argv: Command line arguments (``sys.argv[1:]`` when omitted).

    Returns:
        ``0`` when every threshold is met, ``1`` otherwise (or when the
        runtime is not available yet).
    """
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    thresholds = {
        "category_accuracy": args.min_category_accuracy,
        "priority_accuracy": args.min_priority_accuracy,
        "action_accuracy": args.min_action_accuracy,
        "decision_accuracy": args.min_decision_accuracy,
    }

    try:
        labeled = load_labeled(args.input)
    except (FileNotFoundError, ValueError) as exc:
        print("EVAL ERROR: {0}".format(exc))
        return 1

    try:
        report = run_evaluation(labeled)
    except ImportError as exc:
        print("EVAL ERROR: the runtime services are not available yet ({0}).".format(exc))
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print("EVAL ERROR: {0}".format(exc))
        return 1

    report["thresholds"] = thresholds
    report["breaches"] = check_thresholds(report.get("metrics", {}), thresholds)
    report["passed"] = not report["breaches"]

    write_reports(report, args.out_md, args.out_json)

    metrics = report.get("metrics", {})
    if args.json_only:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("AI Help Desk agent -- evaluation report")
        print("tickets evaluated: {0}".format(metrics.get("total", 0)))
        for metric in sorted(thresholds):
            value = float(metrics.get(metric, 0.0))
            threshold = thresholds[metric]
            state = "ok" if value >= threshold else "BELOW THRESHOLD"
            print("  {0:<20} {1:.4f} (threshold {2:.2f}) {3}".format(metric, value, threshold, state))
        print("report (md)   : {0}".format(_resolve_path(args.out_md)))
        print("report (json) : {0}".format(_resolve_path(args.out_json)))

    if args.verbose:
        for result in report.get("results", []):
            expected = result.get("expected") or {}
            actual = result.get("actual") or {}
            print(
                "  {0} | expected {1}/{2}/{3}/{4} | got {5}/{6}/{7}/{8} | conf {9}".format(
                    result.get("ticket_id", ""),
                    expected.get("category", ""),
                    expected.get("priority", ""),
                    expected.get("action", ""),
                    expected.get("decision", ""),
                    actual.get("category", ""),
                    actual.get("priority", ""),
                    actual.get("action", ""),
                    actual.get("decision", ""),
                    actual.get("confidence", ""),
                )
            )

    if report["breaches"]:
        for breach in report["breaches"]:
            print(
                "EVAL FAILED: {0} {1} < {2}".format(
                    breach["metric"], breach["value"], breach["threshold"]
                )
            )
        print("See {0} for the detailed failures.".format(_resolve_path(args.out_md)))
        return 1

    print("EVAL PASSED: every acceptance threshold is met.")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    sys.exit(main())
