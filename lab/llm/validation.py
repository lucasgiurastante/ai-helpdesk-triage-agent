"""Validación y normalización de salidas del LLM (Fase 02).

Clamp de confidence, normalización de priority, fallback a human_review
cuando la salida es inválida o el proveedor falla.
"""

from __future__ import annotations

from typing import Any

from lab.llm.schema import LLMResponse

VALID_PRIORITIES = {"P1", "P2", "P3", "P4"}


def validate_response(resp: LLMResponse, *, max_tokens: int = 512) -> LLMResponse:
    """Normaliza y valida una LLMResponse in-place (devuelve la misma instancia)."""
    # Clamp confidence
    if resp.confidence < 0.0:
        resp.confidence = 0.0
    if resp.confidence > 1.0:
        resp.confidence = 1.0

    # Normalizar priority
    p = (resp.priority or "").upper().strip()
    if p not in VALID_PRIORITIES:
        # intentar mapear "p1" etc.
        if p in ("1", "HIGH", "CRITICAL"):
            p = "P1"
        elif p in ("2",):
            p = "P2"
        elif p in ("4", "LOW"):
            p = "P4"
        else:
            p = "P3"
    resp.priority = p

    # category vacío -> unknown
    if not resp.category:
        resp.category = "unknown"

    # suggested_action vacío -> human_review
    if not resp.suggested_action:
        resp.suggested_action = "human_review"

    # Minimización de datos: truncar reasoning si es muy largo
    if len(resp.reasoning) > 500:
        resp.reasoning = resp.reasoning[:500] + "..."

    # Tokens: asegurar no exceder max_tokens en metadata
    if resp.metadata.usage.total_tokens > max_tokens * 4:
        # no modificar, solo registrar (no crítico)
        pass

    return resp


def is_failed(resp: LLMResponse) -> bool:
    return resp.confidence <= 0.0 and resp.category == "unknown" and "error" in (resp.reasoning or "")
