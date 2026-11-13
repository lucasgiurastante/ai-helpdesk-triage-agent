"""Policy engine: risk assessment, escalation rules and the final decision.

The engine evaluates structured rules only. There is no ``eval``/``exec`` and
no string condition parsing anywhere in this module: the condition language is
a small, auditable DSL (``{field, op, value}`` plus ``all``/``any``/``not``)
shared by the escalation rules and the risk factors (SPEC sections 3.4.2 and
3.4.5).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.common import (
    APPROVAL_TIMEOUT_MINUTES,
    escalate_risk,
    max_risk,
    safe_get,
)

LOGGER = logging.getLogger(__name__)

#: Comparison operators supported by the rule DSL.
OPERATORS: Tuple[str, ...] = ("eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte")

#: Queue used when the effective risk is critical but no escalation rule fired.
DEFAULT_CRITICAL_QUEUE = "security"

#: Fallback approver role when the risk level does not define one.
FALLBACK_APPROVER_ROLE = "support_lead"


# --------------------------------------------------------------------------- #
# Condition DSL
# --------------------------------------------------------------------------- #

def _as_number(value: Any) -> Optional[float]:
    """Return ``value`` as a float when it is numeric, ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _equals(actual: Any, expected: Any) -> bool:
    """Strict-ish equality that keeps booleans and numbers apart."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) is bool(expected) and actual == expected
    if actual is None or expected is None:
        return actual is expected
    return actual == expected


def _contains(expected: Any, actual: Any) -> bool:
    """``in`` operator: membership in a list, or intersection of two lists."""
    if isinstance(expected, (list, tuple, set)):
        candidates = list(expected)
    else:
        candidates = [expected]
    if isinstance(actual, (list, tuple, set)):
        return any(item in candidates for item in actual)
    return actual in candidates


def _compare(actual: Any, expected: Any, op: str) -> bool:
    """Apply a single comparison operator."""
    if op == "eq":
        return _equals(actual, expected)
    if op == "ne":
        return not _equals(actual, expected)
    if op == "in":
        return _contains(expected, actual)
    if op == "not_in":
        return not _contains(expected, actual)

    left = _as_number(actual)
    right = _as_number(expected)
    if left is not None and right is not None:
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        if op == "lte":
            return left <= right
        return False

    left_text = "" if actual is None else str(actual)
    right_text = "" if expected is None else str(expected)
    if op == "gt":
        return left_text > right_text
    if op == "gte":
        return left_text >= right_text
    if op == "lt":
        return left_text < right_text
    if op == "lte":
        return left_text <= right_text
    return False


def evaluate_condition(condition: Any, context: Dict[str, Any]) -> bool:
    """Evaluate a structured condition against a context dictionary.

    Supported shapes::

        {"all": [ ... ]}
        {"any": [ ... ]}
        {"not": { ... }}
        {"field": "<name>", "op": "<operator>", "value": <value>}

    Args:
        condition: The condition tree.
        context: The evaluation context (flat or nested dictionary).

    Returns:
        ``True`` when the condition holds. Malformed conditions are logged and
        evaluated as ``False`` so that a bad rule can never crash a batch.
    """
    if not isinstance(condition, dict):
        LOGGER.warning("Ignoring malformed rule condition: %r", condition)
        return False

    if "all" in condition:
        items = condition.get("all") or []
        return all(evaluate_condition(item, context) for item in items)
    if "any" in condition:
        items = condition.get("any") or []
        return any(evaluate_condition(item, context) for item in items)
    if "not" in condition:
        return not evaluate_condition(condition.get("not"), context)

    field = condition.get("field")
    op = condition.get("op")
    value = condition.get("value")
    if not field or op not in OPERATORS:
        LOGGER.warning("Ignoring unsupported rule condition: %r", condition)
        return False
    actual = safe_get(context, field)
    return _compare(actual, value, op)


def _rule_rank(rule: Dict[str, Any]) -> Tuple[int, int]:
    """Sort key of an escalation rule: priority first, escalate wins ties."""
    effect = rule.get("effect") or {}
    is_escalation = 1 if effect.get("decision") == "escalate" else 0
    return (int(rule.get("priority", 0) or 0), is_escalation)


# --------------------------------------------------------------------------- #
# Policy engine
# --------------------------------------------------------------------------- #

class PolicyEngine:
    """Evaluate the risk of a proposed action and decide how to proceed.

    Args:
        risk_matrix: Content of ``policies/risk_matrix.json``.
        access_policies: Content of ``policies/access_policies.json`` keyed by
            resource name.
        escalation_rules: Content of ``policies/escalation_rules.json``.
    """

    def __init__(
        self,
        risk_matrix: dict,
        access_policies: dict,
        escalation_rules: List[dict],
    ) -> None:
        self.risk_matrix: Dict[str, Any] = dict(risk_matrix or {})
        self.access_policies: Dict[str, Any] = dict(access_policies or {})
        self.escalation_rules: List[Dict[str, Any]] = list(escalation_rules or [])
        self.policy_version: str = str(self.risk_matrix.get("version", "unknown"))
        self.levels: Dict[str, Any] = self.risk_matrix.get("levels") or {}
        self.thresholds: Dict[str, Any] = self.risk_matrix.get("thresholds") or {}
        self.action_risk: Dict[str, str] = self.risk_matrix.get("action_risk") or {}
        self.category_overrides: Dict[str, Any] = (
            self.risk_matrix.get("category_overrides") or {}
        )
        self.risk_factors: List[Dict[str, Any]] = list(
            self.risk_matrix.get("risk_factors") or []
        )

    # ------------------------------------------------------------------ #
    # Risk
    # ------------------------------------------------------------------ #

    def risk_for_action(self, action: str, base_risk: str) -> str:
        """Return the base risk of an action.

        The risk of an action never degrades: the highest of the action risk
        (risk matrix) and the base risk coming from the knowledge base wins
        (SPEC section 3.4.1).

        Args:
            action: Canonical action name.
            base_risk: Base risk declared by the KB article.

        Returns:
            The effective base risk.
        """
        return max_risk(self.action_risk.get(action), base_risk)

    def _timeout_for(self, role: Optional[str], risk: str) -> int:
        """Return the approval timeout for a role (fallback: risk level)."""
        if role and role in APPROVAL_TIMEOUT_MINUTES:
            return APPROVAL_TIMEOUT_MINUTES[role]
        level = self.levels.get(risk) or {}
        timeout = level.get("timeout_minutes")
        if isinstance(timeout, int):
            return timeout
        return APPROVAL_TIMEOUT_MINUTES.get(FALLBACK_APPROVER_ROLE, 60)

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #

    def _build_context(
        self,
        triage: Dict[str, Any],
        user: Dict[str, Any],
        resource: Optional[str],
        resource_sensitive: bool,
        risk: str,
        base_risk: str,
        action: str,
        effective_action: str,
    ) -> Dict[str, Any]:
        """Build the flat context consumed by the rule DSL (SPEC 3.4.0)."""
        entities = triage.get("entities") or {}
        matched_count = len(triage.get("matched_keywords") or []) + len(
            triage.get("matched_strong_keywords") or []
        )
        return {
            "priority": triage.get("priority"),
            "category": triage.get("category"),
            "intent": triage.get("intent"),
            "impact": triage.get("impact"),
            "urgency": triage.get("urgency"),
            "risk": risk,
            "base_risk": base_risk,
            "confidence": float(triage.get("confidence") or 0.0),
            "matched_count": matched_count,
            "action": action,
            "effective_action": effective_action,
            "service": triage.get("service"),
            "kb_id": triage.get("kb_id"),
            "resource": resource,
            "resource.sensitive": resource_sensitive,
            "user.vip": bool(user.get("vip")),
            "user.active": bool(user.get("active")),
            "user.locked": bool(user.get("locked")),
            "user.mfa_enrolled": bool(user.get("mfa_enrolled")),
            "user.identity_verified": bool(user.get("identity_verified")),
        }

    # ------------------------------------------------------------------ #
    # Escalation rules
    # ------------------------------------------------------------------ #

    def matched_escalations(self, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return the escalation rules that match the context.

        The result is sorted with the strongest rule first: highest priority,
        and ``escalate`` winning ties (SPEC section 3.4.5).

        Args:
            ctx: The evaluation context.

        Returns:
            The matching rules, strongest first.
        """
        matched = [
            rule
            for rule in self.escalation_rules
            if evaluate_condition(rule.get("when") or {}, ctx)
        ]
        matched.sort(key=_rule_rank, reverse=True)
        return matched

    @staticmethod
    def _rule_reasons(rule: Dict[str, Any]) -> List[str]:
        """Return the human readable reasons of an escalation rule.

        The ``reasons`` list of the rule wins; the ``effect.reason`` is only
        used as a fallback so that a rule never appears without an explanation.
        """
        reasons = list(rule.get("reasons") or [])
        if reasons:
            return reasons
        effect_reason = (rule.get("effect") or {}).get("reason")
        return [effect_reason] if effect_reason else []

    def _safety_flags(self, user: Dict[str, Any], ctx: Dict[str, Any]) -> List[str]:
        """Return the safety flags that block the automatic path."""
        flags: List[str] = []
        if ctx.get("user.vip"):
            flags.append("vip")
        if not ctx.get("user.active"):
            flags.append("inactive_user")
        if float(ctx.get("confidence") or 0.0) < float(
            self.thresholds.get("review_min_confidence", 0.45)
        ):
            flags.append("low_confidence")
        if ctx.get("resource.sensitive"):
            flags.append("sensitive_resource")
        if not ctx.get("user.mfa_enrolled"):
            flags.append("no_mfa")
        return flags

    def decide(
        self, risk: str, confidence: float, user: Dict[str, Any], ctx: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Decide how a ticket must be handled.

        The cascade is the contract of SPEC section 3.4.3:

        1. a matching escalation rule with effect ``escalate`` wins;
        2. otherwise ``critical`` risk escalates;
        3. ``high`` and ``medium`` risk require approval;
        4. ``low`` risk is automatic only with high confidence and no flags.

        Args:
            risk: Effective risk after the precedence rules and risk factors.
            confidence: Classification confidence.
            user: Identity record of the requester.
            ctx: Evaluation context.

        Returns:
            ``{"decision", "approver_role", "escalation_queue", "reasons",
            "rule_ids"}``.
        """
        matched = self.matched_escalations(ctx)
        winner = matched[0] if matched else None

        reasons: List[str] = []
        rule_ids: List[str] = [rule.get("id") for rule in matched if rule.get("id")]

        escalate_rule = None
        for rule in matched:
            if (rule.get("effect") or {}).get("decision") == "escalate":
                escalate_rule = rule
                break

        decision: str
        approver_role: Optional[str] = None
        queue: Optional[str] = None
        matrix_reason = ""

        if escalate_rule is not None:
            effect = escalate_rule.get("effect") or {}
            decision = "escalate"
            queue = effect.get("queue")
            approver_role = effect.get("approver_role")
            reasons.extend(self._rule_reasons(escalate_rule))
        elif risk == "critical":
            decision = "escalate"
            queue = DEFAULT_CRITICAL_QUEUE
            approver_role = (self.levels.get("critical") or {}).get("approver_role")
            matrix_reason = (
                "El riesgo efectivo es critical: se escala al equipo de seguridad "
                "y nunca se auto-ejecuta"
            )
        elif risk == "high":
            decision = "require_approval"
            approver_role = (self.levels.get("high") or {}).get("approver_role")
            matrix_reason = (
                "El riesgo efectivo high exige aprobación del rol {role}"
            )
        elif risk == "medium":
            decision = "require_approval"
            approver_role = (self.levels.get("medium") or {}).get("approver_role")
            matrix_reason = (
                "El riesgo efectivo medium exige aprobación del rol {role}"
            )
        else:
            flags = self._safety_flags(user, ctx)
            threshold = float(
                self.thresholds.get("auto_execute_min_confidence", 0.70)
            )
            if confidence >= threshold and not flags:
                decision = "auto_execute"
                matrix_reason = (
                    f"Riesgo low, confianza {confidence:.2f} >= {threshold:.2f} y "
                    "sin flags de seguridad: la acción se auto-ejecuta"
                )
            else:
                decision = "require_approval"
                approver_role = (self.levels.get("low") or {}).get(
                    "approver_role"
                ) or FALLBACK_APPROVER_ROLE
                detail = ", ".join(flags) if flags else "confianza insuficiente"
                matrix_reason = (
                    "Riesgo low pero no cumple el camino automático "
                    f"({detail}): se requiere aprobación"
                )

        # A matching "require_approval" rule may refine the approver role.
        if (
            decision == "require_approval"
            and winner is not None
            and (winner.get("effect") or {}).get("decision") != "escalate"
        ):
            rule_role = (winner.get("effect") or {}).get("approver_role")
            if rule_role:
                approver_role = rule_role

        if decision == "escalate" and approver_role is None:
            approver_role = FALLBACK_APPROVER_ROLE

        if matrix_reason:
            reasons.insert(0, matrix_reason.format(role=approver_role or "-"))

        if (
            decision == "require_approval"
            and winner is not None
            and (winner.get("effect") or {}).get("decision") != "escalate"
        ):
            for reason in self._rule_reasons(winner):
                if reason not in reasons:
                    reasons.append(reason)

        rule_ids.append(f"RISK-MATRIX-{risk.upper()}")
        return {
            "decision": decision,
            "approver_role": approver_role if decision != "auto_execute" else None,
            "escalation_queue": queue,
            "reasons": reasons,
            "rule_ids": rule_ids,
        }

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        triage: Dict[str, Any],
        user: Dict[str, Any],
        request: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Evaluate the risk of the action proposed by the triage service.

        Args:
            triage: Output of ``TriageService.classify``.
            user: Identity record of the requester.
            request: Optional access request context. ``request["resource"]``
                selects the matching entry of ``access_policies``.

        Returns:
            The policy decision described in SPEC section 3.4.4. ``reasons`` is
            always populated and ``rule_ids`` always lists the rules that were
            applied.
        """
        user = user or {}
        request = request or {}
        triage = triage or {}

        category = triage.get("category") or "unknown"
        action = triage.get("suggested_action") or "human_review"
        override = self.category_overrides.get(category) or {}
        effective_action = override.get("action") or action

        entities = triage.get("entities") or {}
        resource = request.get("resource") or entities.get("resource")
        resource_policy = self.access_policies.get(resource) if resource else None
        resource_policy = resource_policy or {}
        resource_sensitive = bool(resource_policy.get("sensitive", False))

        base_risk = triage.get("base_risk") or "medium"
        confidence = float(triage.get("confidence") or 0.0)

        risk = self.risk_for_action(effective_action, base_risk)
        risk = max_risk(
            risk, override.get("risk"), resource_policy.get("risk")
        )

        applied_factors: List[Dict[str, Any]] = []
        for factor in self.risk_factors:
            context = self._build_context(
                triage,
                user,
                resource,
                resource_sensitive,
                risk,
                base_risk,
                action,
                effective_action,
            )
            if not evaluate_condition(factor.get("when") or {}, context):
                continue
            delta = int(factor.get("delta", 1) or 0)
            risk = escalate_risk(risk, delta)
            applied_factors.append(
                {
                    "id": factor.get("id"),
                    "delta": delta,
                    "reason": factor.get("reason", ""),
                }
            )

        context = self._build_context(
            triage,
            user,
            resource,
            resource_sensitive,
            risk,
            base_risk,
            action,
            effective_action,
        )
        decision_bundle = self.decide(risk, confidence, user, context)

        reasons: List[str] = []
        rule_ids: List[str] = []
        for factor in applied_factors:
            if factor.get("reason"):
                reasons.append(factor["reason"])
            if factor.get("id"):
                rule_ids.append(factor["id"])
        reasons.extend(decision_bundle["reasons"])
        rule_ids.extend(decision_bundle["rule_ids"])

        decision = decision_bundle["decision"]
        approver_role = decision_bundle["approver_role"]
        if not reasons:
            reasons.append("Decisión tomada por la matriz de riesgo sin factores adicionales")

        return {
            "decision": decision,
            "requires_approval": decision != "auto_execute",
            "risk": risk,
            "base_risk": base_risk,
            "risk_factors": applied_factors,
            "effective_action": effective_action,
            "approver_role": approver_role,
            "escalation_queue": decision_bundle["escalation_queue"],
            "confidence": confidence,
            "timeout_minutes": self._timeout_for(approver_role, risk),
            "reasons": reasons,
            "rule_ids": rule_ids,
            "policy_version": self.policy_version,
        }


__all__ = ["PolicyEngine", "evaluate_condition", "OPERATORS"]
