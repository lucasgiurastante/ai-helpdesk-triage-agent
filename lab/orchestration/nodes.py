"""Nodos del grafo de orquestacion (Fase 04 parte A).

Cada funcion-nodo recibe un estado (dict) y devuelve un dict parcial con las
actualizaciones. Reutilizan el motor original (services/) y el laboratorio
(lab.llm, lab.rag) sin duplicar logica.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import get_config
from services.common import load_json, repo_path
from services.triage import TriageService
from services.policy import PolicyEngine
from services.approval import ApprovalQueue

from lab.llm import create_provider, validate_response
from lab.rag import VectorStore, retrieve, build_citations, should_abstain, abstain_response
from lab.rag.safety import sanitize_document
from lab.rag.index import build_index

from lab.orchestration.limits import MAX_ITERATIONS, MAX_TOOL_CALLS
from lab.orchestration.state import HelpdeskState

# Re-export constants for convenience
__all__ = [
    "MAX_ITERATIONS",
    "MAX_TOOL_CALLS",
    "node_entry",
    "node_classify",
    "node_retrieve",
    "node_redact",
    "node_policy",
    "node_approval",
    "node_escalate",
]


def _kb_path() -> str:
    return str(repo_path("data", "kb.json"))


def _users_path() -> Path:
    return repo_path("data", "users.json")


def _load_users() -> Dict[str, Any]:
    try:
        with open(_users_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _triage_service() -> TriageService:
    kb = load_json(repo_path("data", "kb.json"))
    risk_matrix = load_json(repo_path("policies", "risk_matrix.json"))
    return TriageService(
        kb,
        risk_matrix.get("priority_matrix"),
        risk_matrix.get("category_overrides"),
    )


def _policy_engine() -> PolicyEngine:
    risk_matrix = load_json(repo_path("policies", "risk_matrix.json"))
    access_policies = load_json(repo_path("policies", "access_policies.json"))
    escalation_rules = load_json(repo_path("policies", "escalation_rules.json"))
    return PolicyEngine(risk_matrix, access_policies, escalation_rules)


def _store() -> VectorStore:
    cfg = get_config()
    return VectorStore(collection_name="kb", persist_path=None)


def _ticket_from_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ticket_id": state.get("ticket_id"),
        "requester": state.get("requester"),
        "description": state.get("description") or state.get("title") or "",
        "service": state.get("service"),
        "vip": bool(state.get("vip", False)),
    }


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #


def node_entry(state: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza la entrada, valida campos, inicializa contadores."""
    state = state or {}
    text = state.get("description") or state.get("title") or ""
    # normalizar con el motor de triage (sin duplicar logica)
    try:
        svc = _triage_service()
        normalized = svc.normalize(text)
        intent = svc.detect_intent(text)
    except Exception:
        normalized = (text or "").strip().lower()
        intent = "unknown"

    ticket_id = state.get("ticket_id")
    if not ticket_id:
        ticket_id = state.get("id")

    return {
        "ticket_id": ticket_id,
        "requester": state.get("requester"),
        "description": text,
        "service": state.get("service"),
        "vip": bool(state.get("vip", False)),
        "normalized_text": normalized,
        "intent": intent or "unknown",
        "iterations": 0,
        "tool_calls": 0,
        "messages": list(state.get("messages") or []),
        "metadata": dict(state.get("metadata") or {}),
    }


def node_classify(state: Dict[str, Any]) -> Dict[str, Any]:
    """Clasifica el ticket usando TriageService."""
    state = state or {}
    ticket = _ticket_from_state(state)
    # inyectar vip si viene en state
    if state.get("vip") is not None:
        ticket["vip"] = bool(state.get("vip"))
    svc = _triage_service()
    result = svc.classify(ticket)

    return {
        "category": result.get("category"),
        "priority": result.get("priority"),
        "confidence": float(result.get("confidence") or 0.0),
        "suggested_action": result.get("suggested_action"),
        "kb_id": result.get("kb_id"),
        "normalized_text": result.get("normalized_text") or state.get("normalized_text"),
        "intent": result.get("intent") or state.get("intent"),
        "base_risk": result.get("base_risk") or "medium",
        "metadata": {**(state.get("metadata") or {}), "base_risk": result.get("base_risk") or "medium",
                    "suggested_action": result.get("suggested_action")},
    }


def node_retrieve(state: Dict[str, Any]) -> Dict[str, Any]:
    """Recupera documentos relevantes con RAG y sanitiza."""
    state = state or {}
    cfg = get_config()
    store = _store()
    kb_path = _kb_path()

    if store.count() == 0:
        try:
            build_index(store, kb_path)
        except Exception:
            try:
                from lab.rag.ingestion import ingest_kb

                ingest_kb(store, kb_path)
            except Exception:
                pass

    query = state.get("normalized_text") or state.get("description") or state.get("title") or ""
    retrieved_docs = retrieve(store, query, k=5)
    # sanitizar
    sanitized: List[Any] = []
    for rd in retrieved_docs:
        try:
            rd.text = sanitize_document(getattr(rd, "text", ""))
        except Exception:
            pass
        sanitized.append(rd)

    citations = build_citations(sanitized)
    abstained = should_abstain(sanitized, min_confidence=0.05)

    return {
        "retrieved": sanitized,
        "citations": citations,
        "rag_abstained": bool(abstained),
        "tool_calls": (state.get("tool_calls") or 0) + 1,
    }


def node_redact(state: Dict[str, Any]) -> Dict[str, Any]:
    """Redacta la respuesta final usando el proveedor LLM."""
    state = state or {}
    cfg = get_config()
    abstained = bool(state.get("rag_abstained"))

    citations = state.get("citations") or []
    citation_dicts = []
    for c in citations:
        if hasattr(c, "doc_id"):
            citation_dicts.append(
                {
                    "doc_id": getattr(c, "doc_id", ""),
                    "title": getattr(c, "title", ""),
                    "snippet": getattr(c, "snippet", ""),
                    "score": getattr(c, "score", 0.0),
                }
            )
        elif isinstance(c, dict):
            citation_dicts.append(c)

    if abstained:
        ar = abstain_response()
        final = {
            "response": ar.get("reasoning", "evidencia insuficiente"),
            "confidence": float(ar.get("confidence", 0.0)),
            "citations": [],
            "category": ar.get("category"),
            "action": ar.get("action"),
        }
        return {"final": final, "llm": None}

    provider = create_provider()
    # Construir prompt con contexto sanitizado
    retrieved = state.get("retrieved") or []
    context_parts = []
    for rd in retrieved:
        text = getattr(rd, "text", None)
        if text is None and isinstance(rd, dict):
            text = rd.get("text", "")
        context_parts.append(str(text or ""))
    context = "\n".join(context_parts)
    from lab.rag.safety import wrap_context

    prompt = (
        f"Ticket: {state.get('description') or state.get('title') or ''}\n"
        f"Categoria: {state.get('category')}\n"
        f"Prioridad: {state.get('priority')}\n"
        f"Contexto (no confiable):\n{wrap_context(context)}\n"
        "Redacta un resumen de resolucion para el ticket."
    )
    try:
        resp = provider.complete(prompt, max_tokens=cfg.llm_max_tokens)
        resp = validate_response(resp, max_tokens=cfg.llm_max_tokens)
    except Exception as exc:
        from lab.llm.schema import LLMResponse

        resp = LLMResponse(category="unknown", priority="P3", suggested_action="human_review",
                           confidence=0.0, reasoning=f"error: {exc}")
        resp = validate_response(resp, max_tokens=cfg.llm_max_tokens)

    llm_dict = resp.to_dict() if hasattr(resp, "to_dict") else None

    final = {
        "response": resp.suggested_resolution or resp.reasoning,
        "confidence": float(resp.confidence),
        "citations": citation_dicts,
        "category": resp.category,
        "priority": resp.priority,
        "suggested_action": resp.suggested_action,
        "reasoning": resp.reasoning,
    }

    return {"final": final, "llm": llm_dict}


def node_policy(state: Dict[str, Any]) -> Dict[str, Any]:
    """Integra PolicyEngine y devuelve la decision de politica."""
    state = state or {}
    engine = _policy_engine()

    metadata = state.get("metadata") or {}
    suggested_action = state.get("suggested_action") or metadata.get("suggested_action")
    base_risk = state.get("base_risk") or metadata.get("base_risk") or "medium"

    triage = {
        "ticket_id": state.get("ticket_id"),
        "requester": state.get("requester"),
        "service": state.get("service"),
        "category": state.get("category") or "unknown",
        "intent": state.get("intent") or "unknown",
        "priority": state.get("priority") or "P3",
        "impact": "individual",
        "urgency": "normal",
        "confidence": float(state.get("confidence") or 0.0),
        "kb_id": state.get("kb_id"),
        "matched_keywords": [],
        "matched_strong_keywords": [],
        "suggested_action": suggested_action or "human_review",
        "base_risk": base_risk,
        "entities": {"resource": None},
    }

    users = _load_users()
    requester = state.get("requester") or ""
    user = users.get(requester) if requester in users else None
    if not user:
        user = {
            "username": requester,
            "active": True,
            "locked": False,
            "vip": bool(state.get("vip", False)),
            "mfa_enrolled": True,
            "identity_verified": True,
        }
    else:
        user = dict(user)
        if state.get("vip") is not None:
            user["vip"] = bool(state.get("vip"))

    try:
        result = engine.evaluate(triage, user)
    except Exception as exc:
        result = {"decision": "require_approval", "risk": "medium", "error": str(exc)}

    return {
        "policy_decision": result.get("decision"),
        "risk": result.get("risk"),
        "approver_role": result.get("approver_role"),
        "escalation_queue": result.get("escalation_queue"),
        "policy_result": result,
        "base_risk": base_risk,
    }


def node_approval(state: Dict[str, Any]) -> Dict[str, Any]:
    """Crea una aprobacion via ApprovalQueue (mock)."""
    state = state or {}
    cfg = get_config()
    risk_matrix = load_json(repo_path("policies", "risk_matrix.json"))
    queue_path = os.path.join(str(REPO_ROOT), "outputs", "approvals.json")
    queue = ApprovalQueue(queue_path, risk_matrix)

    pr = state.get("policy_result") or {}
    action = state.get("suggested_action") or (state.get("metadata") or {}).get("suggested_action") or "human_review"
    request = {
        "ticket_id": state.get("ticket_id"),
        "requester": state.get("requester"),
        "action": action,
        "resource": None,
        "params": {"ticket_id": state.get("ticket_id")},
        "risk": state.get("risk") or "medium",
        "decision": state.get("policy_decision") or "require_approval",
        "approver_role": state.get("approver_role") or "support_lead",
        "confidence": float(state.get("confidence") or 0.0),
        "reasons": (pr.get("reasons") if isinstance(pr, dict) else []) or [],
        "run_id": "RUN-ORCH",
        "target": state.get("requester") or "",
    }
    record = queue.create(request)
    return {"approval_id": record.get("approval_id")}


def node_escalate(state: Dict[str, Any]) -> Dict[str, Any]:
    """Registra la escalacion."""
    state = state or {}
    pr = state.get("policy_result") or {}
    escalation = {
        "queue": state.get("escalation_queue") or (pr.get("escalation_queue") if isinstance(pr, dict) else None),
        "risk": state.get("risk"),
        "reasons": (pr.get("reasons") if isinstance(pr, dict) else []) or [],
        "rule_ids": (pr.get("rule_ids") if isinstance(pr, dict) else []) or [],
        "ticket_id": state.get("ticket_id"),
    }
    return {"escalation": escalation}
