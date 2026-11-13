"""Deterministic ticket classification: normalisation, KB matching, scoring.

The triage service is intentionally free of any LLM call: every decision is a
pure function of the ticket text, the knowledge base and the risk matrix, so
the result is reproducible and explainable in an interview (SPEC section 3.3).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.common import (
    DEFAULT_PRIORITY_MATRIX,
    DEPARTMENT_SCOPE,
    EMAIL_RE,
    KNOWN_SERVICES,
    MENTIONED_USER_RE,
    ORGANIZATION_SCOPE,
    REQUEST_VERBS,
    REVIEW_MIN_CONFIDENCE,
    TROUBLE_VERBS,
    contains_sequence,
    most_urgent_priority,
    normalize_text,
    tokenize,
)

LOGGER = logging.getLogger(__name__)

#: Weight of a strong keyword inside the confidence formula (SPEC 3.3.3).
STRONG_WEIGHT = 2.0

#: Weight of a normal keyword inside the confidence formula (SPEC 3.3.3).
WEAK_WEIGHT = 1.0

#: Number of weighted matches after which the ``evidence`` term saturates.
EVIDENCE_SATURATION = 3.0


class TriageService:
    """Classify help desk tickets against the knowledge base.

    Args:
        kb: Knowledge base articles. Each article carries ``id``, ``category``,
            ``intent``, ``service``, ``priority_hint``, ``action``,
            ``base_risk``, ``keywords``, ``strong_keywords`` and optionally
            ``resolution``, ``runbook``, ``estimated_minutes``, ``resource``
            and ``resource_patterns``.
        priority_matrix: Mapping ``impact -> urgency -> priority``.
        category_overrides: Per category overrides (``priority``, ``risk`` and
            ``action``). ``security`` for instance is forced to ``P1`` /
            ``critical`` / ``escalate_security_team``.
    """

    def __init__(
        self,
        kb: List[dict],
        priority_matrix: Optional[dict] = None,
        category_overrides: Optional[dict] = None,
    ) -> None:
        self.kb: List[dict] = list(kb or [])
        self.priority_matrix: Dict[str, Dict[str, str]] = (
            priority_matrix or DEFAULT_PRIORITY_MATRIX
        )
        self.category_overrides: Dict[str, dict] = dict(category_overrides or {})
        # Tickets already classified per service, used by the "same service in
        # the batch" impact heuristic (SPEC section 3.3.5).
        self._service_seen: Dict[str, int] = {}
        self._index: List[Dict[str, Any]] = [self._index_article(a) for a in self.kb]

    # ------------------------------------------------------------------ #
    # Normalisation / tokenisation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _index_article(article: dict) -> Dict[str, Any]:
        """Pre-compute the tokenised keywords of an article."""
        return {
            "article": article,
            "weak": [
                (keyword, tokenize(keyword))
                for keyword in (article.get("keywords") or [])
            ],
            "strong": [
                (keyword, tokenize(keyword))
                for keyword in (article.get("strong_keywords") or [])
            ],
        }

    def normalize(self, text: str) -> str:
        """Normalise text for matching (lower-case, no accents, no punctuation).

        Args:
            text: Raw text.

        Returns:
            The normalised text.
        """
        return normalize_text(text)

    def tokenize(self, text: str) -> List[str]:
        """Split text into singularised tokens.

        Args:
            text: Raw or normalised text.

        Returns:
            The token list.
        """
        return tokenize(text)

    # ------------------------------------------------------------------ #
    # Matching and scoring
    # ------------------------------------------------------------------ #

    @staticmethod
    def _match_keywords(
        entries: List[Tuple[str, List[str]]], text_tokens: List[str]
    ) -> List[str]:
        """Return the keywords whose token sequence appears contiguously."""
        matched: List[str] = []
        for keyword, keyword_tokens in entries:
            if keyword_tokens and contains_sequence(text_tokens, keyword_tokens):
                matched.append(keyword)
        return matched

    def score(
        self,
        article: dict,
        matched_strong: List[str],
        matched_weak: List[str],
        text_tokens: List[str],
        service: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute the explainable confidence of a KB match.

        The formula is fixed by the SPEC (section 3.3.3)::

            coverage     = weighted_matched / weighted_total
            evidence     = min(1.0, weighted_matched / 3.0)
            specificity  = 1.0 if matched_strong else 0.6
            service_bonus= 0.05 if article.service == service else 0.0
            confidence   = 0.50*coverage + 0.30*evidence + 0.20*specificity + bonus

        Args:
            article: The matched KB article.
            matched_strong: Strong keywords found in the text.
            matched_weak: Normal keywords found in the text.
            text_tokens: Tokenised text (an empty text never produces a match).
            service: Service of the ticket, used for the service bonus.

        Returns:
            The score breakdown: ``confidence``, ``coverage``, ``evidence``,
            ``specificity``, ``service_bonus``, ``weighted_matched`` and
            ``weighted_total``.
        """
        if not text_tokens:
            return {
                "confidence": 0.0,
                "coverage": 0.0,
                "evidence": 0.0,
                "specificity": 0.0,
                "service_bonus": 0.0,
                "weighted_matched": 0,
                "weighted_total": 0,
            }

        strong_keywords = article.get("strong_keywords") or []
        weak_keywords = article.get("keywords") or []
        weighted_matched = (
            STRONG_WEIGHT * len(matched_strong) + WEAK_WEIGHT * len(matched_weak)
        )
        weighted_total = (
            STRONG_WEIGHT * len(strong_keywords) + WEAK_WEIGHT * len(weak_keywords)
        )
        coverage = weighted_matched / weighted_total if weighted_total else 0.0
        evidence = min(1.0, weighted_matched / EVIDENCE_SATURATION)
        specificity = 1.0 if matched_strong else 0.6
        service_bonus = (
            0.05
            if service and article.get("service") == service
            else 0.0
        )
        confidence = min(
            1.0,
            0.50 * coverage
            + 0.30 * evidence
            + 0.20 * specificity
            + service_bonus,
        )
        return {
            "confidence": round(confidence, 2),
            "coverage": round(coverage, 4),
            "evidence": round(evidence, 4),
            "specificity": round(specificity, 4),
            "service_bonus": round(service_bonus, 4),
            "weighted_matched": int(weighted_matched),
            "weighted_total": int(weighted_total),
        }

    def _rank_articles(self, text: str, service: Optional[str]) -> List[Dict[str, Any]]:
        """Score every article and sort them by the SPEC tie-break rules."""
        text_tokens = self.tokenize(text)
        ranked: List[Dict[str, Any]] = []
        for entry in self._index:
            article = entry["article"]
            matched_weak = self._match_keywords(entry["weak"], text_tokens)
            matched_strong = self._match_keywords(entry["strong"], text_tokens)
            if not matched_weak and not matched_strong:
                continue
            breakdown = self.score(
                article, matched_strong, matched_weak, text_tokens, service
            )
            ranked.append(
                {
                    "article": article,
                    "matched_keywords": matched_weak,
                    "matched_strong_keywords": matched_strong,
                    "score_breakdown": breakdown,
                }
            )
        ranked.sort(
            key=lambda item: (
                -item["score_breakdown"]["confidence"],
                -len(item["matched_strong_keywords"]),
                str(item["article"].get("id", "")),
            )
        )
        return ranked

    def match_article(self, text: str, service: Optional[str] = None) -> Dict[str, Any]:
        """Match free text against the knowledge base.

        Tie-break order (SPEC section 3.3.2): highest score, then highest number
        of matched strong keywords, then lowest article id.

        Args:
            text: Ticket description.
            service: Ticket service, used for the service bonus.

        Returns:
            A dictionary with the best ``article``, the matched keywords, the
            score breakdown and the two runner-up articles (``alternatives``).
        """
        ranked = self._rank_articles(text, service)
        if not ranked:
            return {
                "article": None,
                "matched_keywords": [],
                "matched_strong_keywords": [],
                "score_breakdown": {
                    "confidence": 0.0,
                    "coverage": 0.0,
                    "evidence": 0.0,
                    "specificity": 0.0,
                    "service_bonus": 0.0,
                    "weighted_matched": 0,
                    "weighted_total": 0,
                },
                "alternatives": [],
            }
        best = ranked[0]
        alternatives = [
            {
                "kb_id": item["article"].get("id"),
                "category": item["article"].get("category"),
                "confidence": item["score_breakdown"]["confidence"],
            }
            for item in ranked[1:3]
        ]
        return {
            "article": best["article"],
            "matched_keywords": best["matched_keywords"],
            "matched_strong_keywords": best["matched_strong_keywords"],
            "score_breakdown": best["score_breakdown"],
            "alternatives": alternatives,
        }

    # ------------------------------------------------------------------ #
    # Entities
    # ------------------------------------------------------------------ #

    @staticmethod
    def _mentioned_users(text: str) -> List[str]:
        """Return user-like tokens (``name.surname``) found in the raw text.

        The regex is applied to the lower-cased original text: the normalised
        form removes the dots, which would make the pattern unreachable.
        """
        found = MENTIONED_USER_RE.findall((text or "").lower())
        return sorted(set(found))

    @staticmethod
    def _email(text: str) -> Optional[str]:
        """Return the first e-mail address found in the text."""
        match = EMAIL_RE.search((text or "").lower())
        return match.group(0) if match else None

    def _resolve_resource(self, article: dict, text: str) -> Optional[str]:
        """Resolve the resource referenced by an article.

        ``resource_patterns`` gate the article level ``resource``: the resource
        is only considered requested when the text actually mentions one of the
        patterns (for example "finanzas" for the ``finance-folder`` policy).
        """
        resource = article.get("resource")
        patterns = article.get("resource_patterns") or []
        if not patterns:
            return resource
        normalized = self.normalize(text)
        for pattern in patterns:
            needle = self.normalize(pattern)
            if needle and needle in normalized:
                return resource
        return None

    def extract_entities(self, ticket: dict, articles: List[dict]) -> Dict[str, Any]:
        """Extract the deterministic entities of a ticket.

        Args:
            ticket: The source ticket.
            articles: Candidate KB articles, best match first.

        Returns:
            ``{"user", "email", "app", "resource", "mentioned_users"}``. The
            ``mentioned_users`` list is not filtered against the directory --
            that filter is applied by the orchestrator.
        """
        text = ticket.get("description") or ticket.get("title") or ""
        requester = ticket.get("requester") or ""
        service = ticket.get("service") or ""
        app = service if service in KNOWN_SERVICES else (service or None)
        if not app and articles:
            app = articles[0].get("service")
        resource = None
        if articles:
            resource = self._resolve_resource(articles[0], text)
        return {
            "user": requester,
            "email": self._email(text),
            "app": app,
            "resource": resource,
            "mentioned_users": self._mentioned_users(text),
        }

    # ------------------------------------------------------------------ #
    # Impact, urgency, priority and intent
    # ------------------------------------------------------------------ #

    def detect_intent(self, text: str) -> str:
        """Detect the intent of a ticket from its verbs.

        ``trouble`` wins when both a request and a failure verb are present
        (SPEC section 2.1).

        Args:
            text: Ticket description (raw or normalised).

        Returns:
            ``"request"``, ``"trouble"`` or ``"unknown"`` when no verb matched.
        """
        normalized = self.normalize(text)
        has_request = any(verb in normalized for verb in REQUEST_VERBS)
        has_trouble = any(verb in normalized for verb in TROUBLE_VERBS)
        if has_trouble:
            return "trouble"
        if has_request:
            return "request"
        return "unknown"

    def impact_urgency(
        self, ticket: dict, category: str, text: str
    ) -> Tuple[str, str]:
        """Compute the impact and the urgency of a ticket.

        Impact rules (SPEC section 3.3.5): global scope wins over area scope,
        which wins over team scope (``vip`` flag, three or more mentioned users
        or a second ticket of the same service in the batch). Urgency is derived
        from the urgency keywords.

        Args:
            ticket: The source ticket. It may carry an optional ``vip`` boolean
                injected by the orchestrator from the directory of record.
            category: Detected category (kept for API symmetry).
            text: Ticket description.

        Returns:
            A ``(impact, urgency)`` tuple.
        """
        normalized = self.normalize(text)
        service = ticket.get("service") or ""

        impact = "individual"
        if any(scope in normalized for scope in ORGANIZATION_SCOPE):
            impact = "organization"
        elif any(scope in normalized for scope in DEPARTMENT_SCOPE):
            impact = "department"
        else:
            vip = bool(ticket.get("vip"))
            mentioned = len(self._mentioned_users(text))
            repeated_service = self._service_seen.get(service, 0) > 0
            if vip or mentioned >= 3 or repeated_service:
                impact = "team"

        urgency = "normal"
        if any(
            phrase in normalized
            for phrase in (
                "urgente",
                "critico",
                "bloquea la produccion",
                "caida total",
            )
        ):
            urgency = "critical"
        elif any(
            phrase in normalized
            for phrase in ("no puedo trabajar", "me bloquea", "deadline", "fin de mes")
        ):
            urgency = "high"
        elif any(
            phrase in normalized
            for phrase in ("cuando puedas", "sin apuro", "para cuando tengas tiempo")
        ):
            urgency = "low"

        self._service_seen[service] = self._service_seen.get(service, 0) + 1
        return impact, urgency

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #

    def classify(self, ticket: dict) -> Dict[str, Any]:
        """Classify a ticket.

        Args:
            ticket: Mapping with ``ticket_id``, ``requester``, ``description``
                and ``service`` (plus optional ``vip``).

        Returns:
            The dictionary described in SPEC section 3.3.4: identifiers,
            normalised text, intent, category, priority, impact, urgency,
            confidence, KB id, matched keywords, score breakdown, suggested
            action and resolution, base risk, entities and alternatives.
        """
        ticket_id = ticket.get("ticket_id") or ""
        requester = ticket.get("requester") or ""
        service = ticket.get("service") or ""
        text = ticket.get("description") or ticket.get("title") or ""
        normalized = self.normalize(text)

        ranked = self._rank_articles(text, service)
        best = ranked[0] if ranked else None
        breakdown = (
            best["score_breakdown"]
            if best
            else {
                "confidence": 0.0,
                "coverage": 0.0,
                "evidence": 0.0,
                "specificity": 0.0,
                "service_bonus": 0.0,
                "weighted_matched": 0,
                "weighted_total": 0,
            }
        )
        confidence = float(breakdown["confidence"])

        article = best["article"] if best else None
        if article is not None and confidence >= REVIEW_MIN_CONFIDENCE:
            category = article.get("category") or "unknown"
            kb_id = article.get("id")
            suggested_action = article.get("action") or "human_review"
            suggested_resolution = article.get("resolution") or ""
            base_risk = article.get("base_risk") or "medium"
            # The KB article carries the canonical intent of the scenario; the
            # verb heuristic is the fallback for unmatched tickets.
            intent = article.get("intent") or self.detect_intent(text)
        else:
            category = "unknown"
            kb_id = None
            suggested_action = "human_review"
            suggested_resolution = ""
            base_risk = "medium"
            intent = self.detect_intent(text)

        override = self.category_overrides.get(category) or {}
        if override.get("action"):
            suggested_action = override["action"]

        impact, urgency = self.impact_urgency(ticket, category, text)
        priority = self.priority_matrix.get(impact, {}).get(urgency, "P3")
        # The hint of the article only applies when the article was accepted as
        # the classification: a rejected (low confidence) match must not drag
        # the priority of the ticket.
        if kb_id and article and article.get("priority_hint"):
            priority = most_urgent_priority(priority, article["priority_hint"])
        if override.get("priority"):
            priority = most_urgent_priority(priority, override["priority"])

        entities = self.extract_entities(
            ticket, [item["article"] for item in ranked[:3]]
        )
        alternatives = [
            {
                "kb_id": item["article"].get("id"),
                "category": item["article"].get("category"),
                "confidence": item["score_breakdown"]["confidence"],
            }
            for item in ranked[1:3]
        ]

        return {
            "ticket_id": ticket_id,
            "requester": requester,
            "service": service,
            "normalized_text": normalized,
            "intent": intent,
            "category": category,
            "priority": priority,
            "impact": impact,
            "urgency": urgency,
            "confidence": confidence,
            "kb_id": kb_id,
            "matched_keywords": best["matched_keywords"] if best else [],
            "matched_strong_keywords": best["matched_strong_keywords"] if best else [],
            "score_breakdown": breakdown,
            "suggested_action": suggested_action,
            "suggested_resolution": suggested_resolution,
            "base_risk": base_risk,
            "entities": entities,
            "alternatives": alternatives,
        }


__all__ = ["TriageService"]
