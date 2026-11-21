"""Abstencion por evidencia insuficiente (Fase 03 parte B)."""

from __future__ import annotations

from typing import Any, List


def _score_of(item: Any) -> float:
    if isinstance(item, dict):
        try:
            return float(item.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    val = getattr(item, "score", None)
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def should_abstain(
    retrieved_docs: Any,
    min_confidence: float = 0.3,
    min_docs: int = 1,
) -> bool:
    docs = list(retrieved_docs or [])
    if len(docs) < (min_docs or 0):
        return True
    if not docs:
        return True
    max_score = max((_score_of(d) for d in docs), default=0.0)
    if max_score < (min_confidence or 0.0):
        return True
    return False


def abstain_response() -> dict:
    return {
        "category": "unknown",
        "action": "human_review",
        "confidence": 0.0,
        "reasoning": "evidencia insuficiente",
    }
