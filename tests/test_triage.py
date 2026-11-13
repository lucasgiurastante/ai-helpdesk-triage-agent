"""Behavioural tests of the triage service.

They cover the deterministic parts of the classification: normalisation, the
contiguous keyword matching, the confidence formula, the tie-break rules, the
priority matrix and the entity extraction.
"""

import unittest

from services.common import load_json, repo_path
from services.triage import TriageService


def _service() -> TriageService:
    """Build a triage service with the real knowledge base and policies."""
    kb = load_json(repo_path("data", "kb.json"))
    risk_matrix = load_json(repo_path("policies", "risk_matrix.json"))
    return TriageService(
        kb,
        risk_matrix.get("priority_matrix"),
        risk_matrix.get("category_overrides"),
    )


def _ticket(**overrides):
    """Build a minimal ticket payload."""
    ticket = {
        "ticket_id": "TCK-TEST",
        "requester": "juan.perez",
        "description": "",
        "service": "SAP Fiori",
    }
    ticket.update(overrides)
    return ticket


class NormalizationTests(unittest.TestCase):
    """Normalisation and tokenisation rules."""

    def test_normalize_removes_accents_and_punctuation(self):
        service = _service()
        normalized = service.normalize("Contraseña, ¡vencida! -- dön't")
        self.assertEqual(normalized, "contrasena vencida don t")

    def test_normalize_is_idempotent(self):
        service = _service()
        once = service.normalize("Mi cuenta bloqueada en SAP Fiori")
        self.assertEqual(service.normalize(once), once)

    def test_tokenize_singularizes_spanish_plurals(self):
        service = _service()
        self.assertIn("permiso", service.tokenize("permisos"))
        self.assertIn("acceso", service.tokenize("accesos"))
        self.assertIn("sesion", service.tokenize("sesiones"))

    def test_tokenize_handles_empty_text(self):
        service = _service()
        self.assertEqual(service.tokenize(""), [])


class MatchingTests(unittest.TestCase):
    """Knowledge base matching."""

    def test_match_article_finds_account_locked(self):
        service = _service()
        matched = service.match_article(
            "Mi cuenta bloqueada en SAP Fiori, no puedo entrar y el login dice "
            "credencial incorrecta.",
            "SAP Fiori",
        )
        self.assertEqual(matched["article"]["id"], "KB001")
        self.assertIn("cuenta bloqueada", matched["matched_strong_keywords"])
        self.assertIn("sap", matched["matched_keywords"])

    def test_match_is_case_and_accent_insensitive(self):
        service = _service()
        matched = service.match_article("SAP FIORI: CUENTA BLOQUEADA", "SAP Fiori")
        self.assertEqual(matched["article"]["id"], "KB001")

    def test_match_requires_contiguous_keyword(self):
        service = _service()
        # "cuenta esta bloqueada" is not the contiguous sequence "cuenta
        # bloqueada", so the strong keyword must not match.
        matched = service.match_article(
            "La cuenta esta bloqueada y no puedo entrar", "SAP Fiori"
        )
        self.assertNotIn("cuenta bloqueada", matched["matched_strong_keywords"])

    def test_match_article_without_any_hit(self):
        service = _service()
        matched = service.match_article(
            "tema administrativo del proyecto", "Account"
        )
        self.assertIsNone(matched["article"])
        self.assertEqual(matched["score_breakdown"]["confidence"], 0.0)

    def test_match_article_chooses_unlock_over_reset_for_failed_attempts(self):
        """KB013: a lock caused by failed logins is an unlock, not a reset."""
        service = _service()
        matched = service.match_article(
            "Solicito desbloquear mi cuenta porque fue bloqueada por demasiados "
            "intentos fallidos de login",
            "Account",
        )
        self.assertEqual(matched["article"]["id"], "KB013")
        self.assertEqual(matched["article"]["action"], "unlock_account")
        self.assertIn("desbloquear", matched["matched_strong_keywords"])
        # The same article must never shadow a real SAP lock (KB001).
        other = service.match_article(
            "No puedo entrar a SAP Fiori, dice usuario bloqueado", "SAP Fiori"
        )
        self.assertEqual(other["article"]["id"], "KB001")

    def test_tie_break_prefers_the_lowest_article_id(self):
        service = _service()
        # Both KB003 and KB004 match the folder access request; the tie-break
        # is the number of strong keywords and then the article id.
        matched = service.match_article(
            "Necesito solicitar acceso a la carpeta compartida del equipo, es un "
            "permiso de lectura.",
            "Shared Drive",
        )
        self.assertEqual(matched["article"]["id"], "KB003")


class ScoringTests(unittest.TestCase):
    """The confidence formula of SPEC section 3.3.3."""

    def test_score_breakdown_matches_the_formula(self):
        service = _service()
        article = {
            "id": "X",
            "service": "SAP Fiori",
            "keywords": ["a", "b", "c", "d"],
            "strong_keywords": ["e", "f"],
        }
        breakdown = service.score(
            article,
            matched_strong=["e"],
            matched_weak=["a", "b"],
            text_tokens=["e", "a", "b"],
            service="SAP Fiori",
        )
        weighted_matched = 2 * 1 + 1 * 2
        weighted_total = 2 * 2 + 1 * 4
        coverage = weighted_matched / weighted_total
        evidence = min(1.0, weighted_matched / 3.0)
        expected = round(
            min(1.0, 0.50 * coverage + 0.30 * evidence + 0.20 * 1.0 + 0.05), 2
        )
        self.assertEqual(breakdown["weighted_matched"], weighted_matched)
        self.assertEqual(breakdown["weighted_total"], weighted_total)
        self.assertEqual(breakdown["confidence"], expected)
        self.assertEqual(breakdown["service_bonus"], 0.05)

    def test_score_without_service_match_has_no_bonus(self):
        service = _service()
        breakdown = service.score(
            {"id": "X", "service": "Email", "keywords": ["a"], "strong_keywords": []},
            matched_strong=[],
            matched_weak=["a"],
            text_tokens=["a"],
            service="SAP Fiori",
        )
        self.assertEqual(breakdown["service_bonus"], 0.0)
        self.assertEqual(breakdown["specificity"], 0.6)

    def test_score_of_empty_text_is_zero(self):
        service = _service()
        breakdown = service.score(
            {"id": "X", "service": "SAP Fiori", "keywords": ["a"], "strong_keywords": []},
            matched_strong=[],
            matched_weak=[],
            text_tokens=[],
            service="SAP Fiori",
        )
        self.assertEqual(breakdown["confidence"], 0.0)


class ClassificationTests(unittest.TestCase):
    """End to end classification of a ticket."""

    def test_classify_returns_every_contract_key(self):
        service = _service()
        result = service.classify(
            _ticket(description="Mi cuenta bloqueada en SAP Fiori, no puedo entrar")
        )
        for key in (
            "ticket_id",
            "requester",
            "service",
            "normalized_text",
            "intent",
            "category",
            "priority",
            "impact",
            "urgency",
            "confidence",
            "kb_id",
            "matched_keywords",
            "matched_strong_keywords",
            "score_breakdown",
            "suggested_action",
            "suggested_resolution",
            "base_risk",
            "entities",
            "alternatives",
        ):
            self.assertIn(key, result)

    def test_classify_detects_account_locked_with_high_confidence(self):
        service = _service()
        result = service.classify(
            _ticket(
                description="Mi cuenta bloqueada en SAP Fiori, no puedo entrar y el "
                "login dice credencial incorrecta."
            )
        )
        self.assertEqual(result["category"], "account_locked")
        self.assertEqual(result["kb_id"], "KB001")
        self.assertEqual(result["intent"], "trouble")
        self.assertEqual(result["suggested_action"], "password_reset_link")
        self.assertGreaterEqual(result["confidence"], 0.70)

    def test_classify_unknown_text_falls_back_to_human_review(self):
        service = _service()
        result = service.classify(
            _ticket(
                description="Queria consultarte por un tema administrativo del proyecto",
                service="Account",
            )
        )
        self.assertEqual(result["category"], "unknown")
        self.assertEqual(result["suggested_action"], "human_review")
        self.assertLess(result["confidence"], 0.45)
        self.assertIsNone(result["kb_id"])

    def test_classify_reports_the_runner_up_articles(self):
        service = _service()
        result = service.classify(
            _ticket(
                description="Necesito solicitar acceso a la carpeta compartida del "
                "equipo, es un permiso de lectura.",
                service="Shared Drive",
            )
        )
        self.assertTrue(result["alternatives"])
        for alternative in result["alternatives"]:
            self.assertIn("kb_id", alternative)
            self.assertIn("category", alternative)
            self.assertIn("confidence", alternative)

    def test_intent_request_wins_only_without_failure_verbs(self):
        service = _service()
        self.assertEqual(
            service.detect_intent("Solicito acceso a la carpeta de Finanzas"),
            "request",
        )
        self.assertEqual(
            service.detect_intent(
                "No puedo acceder a la carpeta y dice que hay un error"
            ),
            "trouble",
        )

    def test_priority_uses_the_most_urgent_of_matrix_and_hint(self):
        service = _service()
        result = service.classify(
            _ticket(
                description="Recibí un correo sospechoso con phishing, hay posible "
                "malware y robo de contrasena, es urgente.",
                service="Security",
            )
        )
        self.assertEqual(result["urgency"], "critical")
        self.assertEqual(result["priority"], "P1")

    def test_impact_team_when_the_user_is_vip(self):
        service = _service()
        result = service.classify(
            _ticket(
                description="Mi laptop no enciende, la pantalla queda negra",
                service="Hardware",
                vip=True,
            )
        )
        self.assertEqual(result["impact"], "team")

    def test_impact_department_and_organization_scopes(self):
        service = _service()
        floor_text = "no hay internet en todo el piso"
        company_text = "sitio completo sin conexion"
        self.assertEqual(
            service.impact_urgency(_ticket(description=floor_text), "network", floor_text)[0],
            "department",
        )
        self.assertEqual(
            service.impact_urgency(
                _ticket(description=company_text), "network", company_text
            )[0],
            "organization",
        )


class EntityTests(unittest.TestCase):
    """Deterministic entity extraction."""

    def test_mentioned_users_and_email_are_extracted(self):
        service = _service()
        result = service.extract_entities(
            _ticket(
                description="Carlos me dijo que carlos.ruiz y ana.silva lo saben, "
                "escriban a carlos.ruiz@example.com"
            ),
            [],
        )
        self.assertEqual(result["user"], "juan.perez")
        self.assertIn("carlos.ruiz", result["mentioned_users"])
        self.assertIn("ana.silva", result["mentioned_users"])
        self.assertEqual(result["email"], "carlos.ruiz@example.com")

    def test_app_uses_the_ticket_service_when_known(self):
        service = _service()
        result = service.extract_entities(_ticket(service="SAP Fiori"), [])
        self.assertEqual(result["app"], "SAP Fiori")

    def test_resource_requires_the_pattern_to_be_present(self):
        service = _service()
        kb = load_json(repo_path("data", "kb.json"))
        article = next(item for item in kb if item["id"] == "KB003")
        self.assertEqual(
            service.extract_entities(
                _ticket(description="acceso a la carpeta de Finanzas"), [article]
            )["resource"],
            "finance-folder",
        )
        self.assertIsNone(
            service.extract_entities(
                _ticket(description="acceso a la carpeta del equipo"), [article]
            )["resource"]
        )


if __name__ == "__main__":
    unittest.main()
