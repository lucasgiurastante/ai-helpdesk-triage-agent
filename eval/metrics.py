"""Pure metric helpers for the evaluation harness.

Nothing in this module performs I/O: it only turns the per-ticket results
produced by :mod:`eval.run_eval` into the numbers that decide whether the
agent is good enough (SPEC sections 3.8, 4.10 and 5).

The comparison is always an **exact string comparison** on ``category``,
``intent``, ``priority``, ``action`` and ``decision`` (SPEC section 5.3).
"""

from typing import Any, Dict, Iterable, List, Sequence, Tuple

#: Canonical decisions (SPEC section 2.4).
DECISIONS: Tuple[str, ...] = ("auto_execute", "require_approval", "escalate")

#: Canonical risk levels (SPEC section 2.3).
RISK_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "critical")

#: Canonical priorities (SPEC section 2.2).
PRIORITIES: Tuple[str, ...] = ("P1", "P2", "P3", "P4")

#: Fields compared by the harness.
COMPARED_FIELDS: Tuple[str, ...] = ("category", "intent", "priority", "action", "decision")


def _pair_values(pairs: Iterable[Any]) -> List[Tuple[str, str]]:
    """Normalise the input of :func:`accuracy` into ``(expected, actual)`` pairs.

    Args:
        pairs: Iterable of ``(expected, actual)`` tuples, lists or mappings
            holding an ``expected`` and an ``actual`` key.

    Returns:
        The list of normalised pairs.
    """
    normalized: List[Tuple[str, str]] = []
    for item in pairs:
        if isinstance(item, dict):
            normalized.append(
                (str(item.get("expected", "")), str(item.get("actual", "")))
            )
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            normalized.append((str(item[0]), str(item[1])))
    return normalized


def accuracy(pairs: Iterable[Any]) -> float:
    """Return the ratio of exact matches.

    Args:
        pairs: Iterable of ``(expected, actual)`` pairs (tuples, lists or
            dictionaries with an ``expected``/``actual`` key).

    Returns:
        The accuracy in the ``0.0 .. 1.0`` range.  An empty input returns
        ``0.0`` (there is nothing to be right about).
    """
    values = _pair_values(pairs)
    if not values:
        return 0.0
    hits = sum(1 for expected, actual in values if expected == actual)
    return round(hits / float(len(values)), 4)


def confusion_matrix(expected: Sequence[str], actual: Sequence[str]) -> Dict[str, Dict[str, int]]:
    """Build the confusion matrix of two label sequences.

    Rows are the expected labels, columns the predicted ones.

    Args:
        expected: Expected labels.
        actual: Predicted labels.

    Returns:
        A nested dictionary ``{expected: {predicted: count}}``.  Every label
        seen in either sequence gets a row and a column, so the matrix is
        always square and easy to render.
    """
    labels: List[str] = []
    for label in list(expected) + list(actual):
        text = str(label)
        if text not in labels:
            labels.append(text)
    matrix: Dict[str, Dict[str, int]] = {label: {} for label in labels}
    for label in labels:
        for other in labels:
            matrix[label][other] = 0
    for expected_label, actual_label in zip(expected, actual):
        matrix[str(expected_label)][str(actual_label)] += 1
    return matrix


def _field_pairs(results: Sequence[Dict[str, Any]], field: str) -> List[Tuple[str, str]]:
    """Extract the ``(expected, actual)`` pairs of one field.

    Args:
        results: Per-ticket results.
        field: Name of the compared field.

    Returns:
        The list of pairs (tickets without the field are ignored).
    """
    pairs: List[Tuple[str, str]] = []
    for result in results:
        expected = (result.get("expected") or {}).get(field)
        actual = (result.get("actual") or {}).get(field)
        if expected is None or actual is None:
            continue
        pairs.append((str(expected), str(actual)))
    return pairs


def _recall(results: Sequence[Dict[str, Any]], expected_value: str, actual_value: str) -> float:
    """Return the recall of one decision value.

    Args:
        results: Per-ticket results.
        expected_value: Decision expected in the ground truth.
        actual_value: Decision predicted by the agent.

    Returns:
        The ratio of tickets with the expected decision that were predicted
        correctly (``0.0`` when there is no such ticket).
    """
    total = 0
    hits = 0
    for result in results:
        expected = (result.get("expected") or {}).get("decision")
        actual = (result.get("actual") or {}).get("decision")
        if expected is None:
            continue
        if str(expected) == expected_value:
            total += 1
            if str(actual) == actual_value:
                hits += 1
    if total == 0:
        return 0.0
    return round(hits / float(total), 4)


def _breakdown(
    results: Sequence[Dict[str, Any]],
    key: str,
    fields: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """Break the accuracy down by one dimension of the ground truth.

    Args:
        results: Per-ticket results.
        key: Name of the expected field used to group the tickets.
        fields: Compared fields reported inside every group.

    Returns:
        A dictionary ``{group: {"count": n, "<field>_accuracy": ratio, ...}}``.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for result in results:
        value = (result.get("expected") or {}).get(key)
        if value is None:
            continue
        groups.setdefault(str(value), []).append(result)
    breakdown: Dict[str, Dict[str, Any]] = {}
    for group in sorted(groups):
        subset = groups[group]
        entry: Dict[str, Any] = {"count": len(subset)}
        for field in fields:
            entry["{0}_accuracy".format(field)] = accuracy(_field_pairs(subset, field))
        breakdown[group] = entry
    return breakdown


def _failure_detail(result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the failure record of one ticket.

    Args:
        result: Per-ticket result.

    Returns:
        A dictionary with the ticket id, the expected and the actual values
        and the list of the fields that did not match.
    """
    expected = result.get("expected") or {}
    actual = result.get("actual") or {}
    mismatches = [
        {
            "field": field,
            "expected": expected.get(field),
            "actual": actual.get(field),
        }
        for field in COMPARED_FIELDS
        if expected.get(field) is not None and expected.get(field) != actual.get(field)
    ]
    return {
        "ticket_id": result.get("ticket_id", ""),
        "description": result.get("description", ""),
        "service": result.get("service", ""),
        "requester": result.get("requester", ""),
        "expected": {field: expected.get(field) for field in COMPARED_FIELDS},
        "actual": {field: actual.get(field) for field in COMPARED_FIELDS},
        "confidence": actual.get("confidence"),
        "kb_id": actual.get("kb_id"),
        "mismatched_fields": mismatches,
        "description_of_failure": "; ".join(
            "{0}: expected {1!r}, got {2!r}".format(
                item["field"], item["expected"], item["actual"]
            )
            for item in mismatches
        )
        or "no mismatch",
    }


def summarize(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate the per-ticket results into the metric bundle of the report.

    Args:
        results: Per-ticket results, as produced by the harness.

    Returns:
        A dictionary with the global metrics, the confusion matrices, the
        breakdowns by expected category and risk, and the list of failures.
    """
    tickets = list(results)
    metrics: Dict[str, Any] = {
        "total": len(tickets),
        "category_accuracy": accuracy(_field_pairs(tickets, "category")),
        "priority_accuracy": accuracy(_field_pairs(tickets, "priority")),
        "action_accuracy": accuracy(_field_pairs(tickets, "action")),
        "decision_accuracy": accuracy(_field_pairs(tickets, "decision")),
        "risk_accuracy": accuracy(_field_pairs(tickets, "risk")),
        "intent_accuracy": accuracy(_field_pairs(tickets, "intent")),
    }

    decisions = [(result.get("actual") or {}).get("decision") for result in tickets]
    known_decisions = [str(decision) for decision in decisions if decision is not None]
    total = len(known_decisions)
    metrics["auto_resolution_rate"] = (
        round(known_decisions.count("auto_execute") / float(total), 4) if total else 0.0
    )
    metrics["approval_rate"] = (
        round(known_decisions.count("require_approval") / float(total), 4) if total else 0.0
    )
    metrics["escalation_rate"] = (
        round(known_decisions.count("escalate") / float(total), 4) if total else 0.0
    )
    metrics["escalation_recall"] = _recall(tickets, "escalate", "escalate")
    metrics["auto_resolution_recall"] = _recall(tickets, "auto_execute", "auto_execute")
    metrics["approval_recall"] = _recall(tickets, "require_approval", "require_approval")

    confidences = [
        float((result.get("actual") or {}).get("confidence", 0.0) or 0.0)
        for result in tickets
    ]
    metrics["avg_confidence"] = (
        round(sum(confidences) / float(len(confidences)), 4) if confidences else 0.0
    )

    metrics["category_confusion"] = confusion_matrix(
        [str((result.get("expected") or {}).get("category", "")) for result in tickets],
        [str((result.get("actual") or {}).get("category", "")) for result in tickets],
    )
    metrics["priority_confusion"] = confusion_matrix(
        [str((result.get("expected") or {}).get("priority", "")) for result in tickets],
        [str((result.get("actual") or {}).get("priority", "")) for result in tickets],
    )
    metrics["decision_confusion"] = confusion_matrix(
        [str((result.get("expected") or {}).get("decision", "")) for result in tickets],
        [str((result.get("actual") or {}).get("decision", "")) for result in tickets],
    )
    metrics["breakdown_by_category"] = _breakdown(
        tickets, "category", ("category", "priority", "action", "decision")
    )
    metrics["breakdown_by_risk"] = _breakdown(
        tickets, "risk", ("category", "priority", "action", "decision")
    )

    failures: List[Dict[str, Any]] = []
    for result in tickets:
        expected = result.get("expected") or {}
        actual = result.get("actual") or {}
        wrong = [
            field
            for field in COMPARED_FIELDS
            if expected.get(field) is not None and expected.get(field) != actual.get(field)
        ]
        if wrong:
            failures.append(_failure_detail(result))
    metrics["failures"] = failures
    metrics["failure_count"] = len(failures)
    return metrics


def check_thresholds(
    metrics: Dict[str, Any],
    thresholds: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Compare the metrics against the acceptance thresholds.

    Args:
        metrics: The metric bundle produced by :func:`summarize`.
        thresholds: Mapping ``metric name -> minimum accepted value``.

    Returns:
        The list of the thresholds that were **not** met.  An empty list
        means the run passed the gate.
    """
    breaches: List[Dict[str, Any]] = []
    for metric, threshold in sorted(thresholds.items()):
        value = float(metrics.get(metric, 0.0) or 0.0)
        if value < float(threshold):
            breaches.append({"metric": metric, "value": value, "threshold": float(threshold)})
    return breaches


__all__ = [
    "COMPARED_FIELDS",
    "DECISIONS",
    "PRIORITIES",
    "RISK_LEVELS",
    "accuracy",
    "check_thresholds",
    "confusion_matrix",
    "summarize",
]
