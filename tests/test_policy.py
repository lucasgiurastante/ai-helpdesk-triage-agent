"""Behavioural tests of the policy engine.

They cover the risk precedence, the risk factors, the decision matrix, the
escalation rule DSL and the "no eval/exec" guardrail.
"""

import inspect
import unittest

from services.common import load_json, repo_path
from services.policy import PolicyEngine, evaluate_condition


def _engine() -> PolicyEngine:
    """Build a policy engine with the real policies."""
    risk_matrix = load_json(repo_path("policies", "risk_matrix.json"))
    access_policies = load_json(repo_path("policies", "access_policies.json"))
    escalation_rules = load_json(repo_path("policies", "escalation_rules.json"))
    return PolicyEngine(risk_matrix, access_policies, escalation_rules)


def _triage(**overrides):
    """Build a triage payload with sensible defaults."""
    triage = {
        "ticket_id": "TCK-TEST",
        "requester": "juan.perez",
        "service": "SAP Fiori",
        "category": "access",
        "intent": "trouble",
        "priority": "P3",
        "impact": "individual",
        "urgency": "normal",
        "confidence": 0.80,
        "kb_id": "KB005",
        "matched_keywords": ["sap"],
        "matched_strong_keywords": ["rol"],
        "suggested_action": "grant_role",
        "base_risk": "high",
        "entities": {"resource": None},
    }
    triage.update(overrides)
    return triage


def _user(**overrides):
    """Build an identity record with sensible defaults."""
    user = {
        "username": "juan.perez",
        "active": True,
        "locked": False,
        "locked_by_admin": False,
        "vip": False,
        "mfa_enrolled": True,
        "identity_verified": True,
        "manager": "laura.gomez",
    }
    user.update(overrides)
    return user


class RiskPrecedenceTests(unittest.TestCase):
    """The risk of an action never degrades."""

    def test_action_risk_wins_over_the_kb_base_risk(self):
        engine = _engine()
        self.assertEqual(engine.risk_for_action("grant_role", "low"), "high")

    def test_base_risk_wins_when_the_action_is_unknown(self):
        engine = _engine()
        self.assertEqual(engine.risk_for_action("not_an_action", "critical"), "critical")

    def test_default_risk_is_medium(self):
        engine = _engine()
        self.assertEqual(engine.risk_for_action("not_an_action", ""), "medium")

    def test_context_only_raises_the_risk(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(suggested_action="send_kb_article", base_risk="low"),
            _user(),
        )
        self.assertEqual(decision["risk"], "low")

    def test_sensitive_resource_raises_the_risk(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                suggested_action="send_kb_article",
                base_risk="low",
                category="access",
                entities={"resource": "finance-folder"},
            ),
            _user(),
        )
        # The resource policy already sets the risk to high and the sensitive
        # flag raises it one more level.
        self.assertEqual(decision["risk"], "critical")
        self.assertIn("RISK-003", decision["rule_ids"])
        self.assertTrue(
            any("sensible" in reason.lower() for reason in decision["reasons"])
        )

    def test_vip_factor_raises_the_risk(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(suggested_action="send_kb_article", base_risk="low"),
            _user(vip=True),
        )
        self.assertEqual(decision["risk"], "medium")
        self.assertIn("RISK-001", decision["rule_ids"])

    def test_inactive_user_factor_raises_the_risk(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(suggested_action="send_kb_article", base_risk="low"),
            _user(active=False),
        )
        self.assertEqual(decision["risk"], "medium")
        self.assertIn("RISK-002", decision["rule_ids"])

    def test_minimum_privilege_factor_raises_the_risk(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(suggested_action="grant_role", base_risk="high"), _user()
        )
        self.assertEqual(decision["risk"], "critical")
        self.assertIn("RISK-006", decision["rule_ids"])

    def test_security_category_override_is_applied(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                category="security",
                suggested_action="send_kb_article",
                base_risk="low",
            ),
            _user(),
        )
        self.assertEqual(decision["effective_action"], "escalate_security_team")
        self.assertEqual(decision["risk"], "critical")
        self.assertEqual(decision["decision"], "escalate")


class DecisionMatrixTests(unittest.TestCase):
    """The decision cascade of SPEC section 3.4.3."""

    def test_low_risk_and_high_confidence_is_automatic(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                suggested_action="send_kb_article",
                base_risk="low",
                confidence=0.80,
                category="email",
            ),
            _user(),
        )
        self.assertEqual(decision["decision"], "auto_execute")
        self.assertFalse(decision["requires_approval"])
        self.assertIsNone(decision["approver_role"])

    def test_low_risk_and_medium_confidence_requires_approval(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                suggested_action="send_kb_article",
                base_risk="low",
                confidence=0.60,
                category="email",
            ),
            _user(),
        )
        self.assertEqual(decision["decision"], "require_approval")
        self.assertTrue(decision["requires_approval"])
        self.assertEqual(decision["approver_role"], "support_lead")

    def test_medium_risk_requires_the_support_lead(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(suggested_action="restart_service", base_risk="medium"),
            _user(),
        )
        self.assertEqual(decision["decision"], "require_approval")
        self.assertEqual(decision["approver_role"], "support_lead")
        self.assertEqual(decision["timeout_minutes"], 240)

    def test_high_risk_requires_the_manager(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(suggested_action="request_access_approval", base_risk="high"),
            _user(),
        )
        self.assertEqual(decision["decision"], "require_approval")
        self.assertEqual(decision["approver_role"], "manager")
        self.assertEqual(decision["timeout_minutes"], 480)

    def test_critical_risk_escalates(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(suggested_action="disable_user", base_risk="critical"),
            _user(),
        )
        self.assertEqual(decision["decision"], "escalate")
        self.assertEqual(decision["escalation_queue"], "security")
        self.assertEqual(decision["approver_role"], "security")

    def test_every_decision_has_reasons_and_rule_ids(self):
        engine = _engine()
        for action, base_risk in (
            ("send_kb_article", "low"),
            ("restart_service", "medium"),
            ("request_access_approval", "high"),
            ("disable_user", "critical"),
        ):
            decision = engine.evaluate(
                _triage(suggested_action=action, base_risk=base_risk), _user()
            )
            self.assertTrue(decision["reasons"])
            self.assertTrue(decision["rule_ids"])
            self.assertEqual(
                decision["policy_version"],
                load_json(repo_path("policies", "risk_matrix.json"))["version"],
            )


class EscalationRuleTests(unittest.TestCase):
    """The structured rule DSL."""

    def test_p1_tickets_escalate_to_the_on_call(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                priority="P1",
                suggested_action="send_kb_article",
                base_risk="low",
                confidence=0.90,
            ),
            _user(),
        )
        self.assertEqual(decision["decision"], "escalate")
        self.assertEqual(decision["escalation_queue"], "on-call")
        self.assertIn("ESC-001", decision["rule_ids"])

    def test_security_category_escalates_to_security(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(category="security", suggested_action="send_kb_article"),
            _user(),
        )
        self.assertEqual(decision["decision"], "escalate")
        self.assertEqual(decision["escalation_queue"], "security")
        self.assertIn("ESC-002", decision["rule_ids"])

    def test_network_incident_with_area_scope_escalates_to_the_network_team(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                category="network",
                impact="department",
                suggested_action="escalate_network_team",
                base_risk="medium",
            ),
            _user(),
        )
        self.assertEqual(decision["decision"], "escalate")
        self.assertEqual(decision["escalation_queue"], "network-team")
        self.assertIn("ESC-006", decision["rule_ids"])

    def test_low_confidence_rule_assigns_the_support_lead(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                suggested_action="send_kb_article",
                base_risk="low",
                confidence=0.40,
                category="email",
            ),
            _user(),
        )
        self.assertEqual(decision["decision"], "require_approval")
        self.assertEqual(decision["approver_role"], "support_lead")
        self.assertIn("ESC-003", decision["rule_ids"])

    def test_uncertain_vpn_ticket_requires_review(self):
        engine = _engine()
        decision = engine.evaluate(
            _triage(
                category="vpn",
                suggested_action="restart_service",
                base_risk="medium",
                confidence=0.45,
            ),
            _user(),
        )
        self.assertEqual(decision["decision"], "require_approval")
        self.assertIn("ESC-007", decision["rule_ids"])

    def test_operators(self):
        context = {"confidence": 0.5, "priority": "P1", "risk": "high"}
        self.assertTrue(evaluate_condition({"field": "confidence", "op": "lt", "value": 0.7}, context))
        self.assertFalse(evaluate_condition({"field": "confidence", "op": "gt", "value": 0.7}, context))
        self.assertTrue(evaluate_condition({"field": "confidence", "op": "gte", "value": 0.5}, context))
        self.assertTrue(evaluate_condition({"field": "risk", "op": "in", "value": ["high", "critical"]}, context))
        self.assertTrue(evaluate_condition({"field": "risk", "op": "not_in", "value": ["low"]}, context))
        self.assertTrue(evaluate_condition({"field": "priority", "op": "eq", "value": "P1"}, context))
        self.assertTrue(evaluate_condition({"field": "priority", "op": "ne", "value": "P2"}, context))

    def test_composite_conditions(self):
        context = {"category": "network", "impact": "department"}
        condition = {
            "all": [
                {"field": "category", "op": "eq", "value": "network"},
                {"field": "impact", "op": "in", "value": ["department", "organization"]},
            ]
        }
        self.assertTrue(evaluate_condition(condition, context))
        self.assertFalse(
            evaluate_condition({"not": condition}, context)
        )
        self.assertTrue(
            evaluate_condition(
                {"any": [{"field": "category", "op": "eq", "value": "vpn"}, condition]},
                context,
            )
        )

    def test_unsupported_condition_is_ignored(self):
        self.assertFalse(
            evaluate_condition({"field": "x", "op": "matches", "value": ".*"}, {})
        )


class GuardrailTests(unittest.TestCase):
    """The engine never evaluates a string as code."""

    def test_policy_module_does_not_use_eval_or_exec(self):
        import services.policy as policy_module

        source = inspect.getsource(policy_module)
        self.assertNotIn("eval(", source)
        self.assertNotIn("exec(", source)

    def test_escalation_rules_use_the_structured_schema(self):
        rules = load_json(repo_path("policies", "escalation_rules.json"))
        ids = {rule["id"] for rule in rules}
        self.assertEqual(
            ids,
            {
                "ESC-001",
                "ESC-002",
                "ESC-003",
                "ESC-004",
                "ESC-005",
                "ESC-006",
                "ESC-007",
            },
        )
        for rule in rules:
            self.assertIn("when", rule)
            self.assertIn("effect", rule)
            self.assertIn(rule["effect"]["decision"], ("escalate", "require_approval"))


if __name__ == "__main__":
    unittest.main()
