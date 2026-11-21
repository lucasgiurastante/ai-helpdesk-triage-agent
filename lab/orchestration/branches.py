"""Ramas de decision del grafo de orquestacion (Fase 04 parte B).

Funciones puras que deciden el siguiente nodo a partir del estado.
No modifican el estado; solo devuelven la etiqueta de la rama.
"""

from __future__ import annotations

from typing import Any

APPROVAL_THRESHOLD_CONFIDENCE = 0.5


def _lookup(state: Any, *keys, default=None):
    # lazy import to avoid circular deps
    if isinstance(state, dict):
        for k in keys:
            if k in state and state.get(k) is not None:
                return state.get(k)
        return default
    for k in keys:
        v = getattr(state, k, None)
        if v is not None:
            return v
    return default


def route_after_policy(state) -> str:
    """Devuelve 'approve', 'escalate' o 'redact' segun policy_decision."""
    decision = _lookup(
        state, "policy_decision", "policy", "decision", "policy_result", default=""
    )
    decision = str(decision or "").strip().lower()
    if decision == "require_approval":
        return "approve"
    if decision == "escalate":
        return "escalate"
    if decision == "auto_execute":
        return "redact"
    # tolerancia a variantes
    if "approv" in decision:
        return "approve"
    if "escal" in decision:
        return "escalate"
    if "auto" in decision or "exec" in decision or "redact" in decision:
        return "redact"
    return "redact"


def route_after_retrieve(state) -> str:
    """Devuelve 'abstain' si RAG se abstuvo, si no 'policy'."""
    abstained = _lookup(state, "rag_abstained", "abstained", "abstain", default=False)
    if isinstance(abstained, str):
        abstained = abstained.strip().lower() in ("true", "1", "yes", "si", "sí")
    return "abstain" if bool(abstained) else "policy"


def route_after_redact(state) -> str:
    """Devuelve 'end' (o 'review' si confidence < threshold)."""
    conf = _lookup(
        state, "confidence", "triage_confidence", "redact_confidence",
        "policy_confidence", default=None,
    )
    try:
        conf_val = None if conf is None else float(conf)
    except (TypeError, ValueError):
        conf_val = None
    if conf_val is not None and conf_val < APPROVAL_THRESHOLD_CONFIDENCE:
        return "review"
    return "end"
