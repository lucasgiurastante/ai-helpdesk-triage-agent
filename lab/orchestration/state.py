"""Estado tipado del grafo de orquestacion (Fase 04 parte A)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class HelpdeskState(TypedDict, total=False):
    # --- Entrada del ticket ---
    ticket_id: Optional[str]
    requester: Optional[str]
    description: Optional[str]
    title: Optional[str]
    service: Optional[str]
    vip: bool

    # --- Normalizacion / clasificacion ---
    normalized_text: Optional[str]
    intent: Optional[str]
    category: Optional[str]
    priority: Optional[str]
    confidence: float
    suggested_action: Optional[str]
    kb_id: Optional[str]
    base_risk: Optional[str]

    # --- RAG ---
    retrieved: List[Any]
    citations: List[Any]
    rag_abstained: bool

    # --- LLM ---
    llm: Optional[Dict[str, Any]]

    # --- Policy ---
    policy_decision: Optional[str]
    risk: Optional[str]
    approver_role: Optional[str]
    escalation_queue: Optional[str]
    policy_result: Optional[Dict[str, Any]]

    # --- Acciones ---
    approval_id: Optional[str]
    escalation: Optional[Dict[str, Any]]
    final: Optional[Dict[str, Any]]

    # --- Contadores / control ---
    iterations: int
    tool_calls: int
    messages: List[Any]
    error: Optional[str]
    metadata: Dict[str, Any]


# Alias conveniente
OrchestrationState = HelpdeskState


__all__ = ["HelpdeskState", "OrchestrationState"]
