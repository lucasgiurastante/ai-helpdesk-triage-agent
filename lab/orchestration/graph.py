"""Grafo de orquestacion (Fase 04 parte A).

Construye un StateGraph si langgraph esta disponible; de lo contrario ofrece
una ejecucion secuencial equivalente. Expone ``build_graph`` y ``run_pipeline``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lab.orchestration.nodes import (
    node_entry,
    node_classify,
    node_retrieve,
    node_policy,
    node_approval,
    node_escalate,
    node_redact,
)
from lab.orchestration.branches import route_after_policy
from lab.orchestration.limits import MAX_ITERATIONS, increment_iteration
from lab.orchestration.state import HelpdeskState

# Guarda de import de langgraph
try:  # pragma: no cover - depende del entorno
    import langgraph  # type: ignore
    from langgraph.graph import StateGraph, END  # type: ignore

    _LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover
    langgraph = None  # type: ignore
    StateGraph = None  # type: ignore
    END = None  # type: ignore
    _LANGGRAPH_AVAILABLE = False


def _merge(state: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(state or {})
    for k, v in (update or {}).items():
        if v is not None or k in ("final", "approval_id", "escalation", "llm"):
            out[k] = v
    # contadores: respetar updates None? increment_iteration handles
    return out


def _run_branch(state: Dict[str, Any], branch: str) -> Dict[str, Any]:
    if branch == "approve":
        return node_approval(state)
    if branch == "escalate":
        return node_escalate(state)
    return node_redact(state)


def run_pipeline(ticket: Dict[str, Any]) -> Dict[str, Any]:
    """Ejecuta el pipeline completo y devuelve el estado final."""
    state: Dict[str, Any] = dict(ticket or {})

    # entry
    state = _merge(state, node_entry(state))
    state = _merge(state, increment_iteration(state))

    # classify
    state = _merge(state, node_classify(state))
    state = _merge(state, increment_iteration(state))

    # retrieve
    state = _merge(state, node_retrieve(state))
    state = _merge(state, increment_iteration(state))

    # policy
    state = _merge(state, node_policy(state))
    state = _merge(state, increment_iteration(state))

    # rama
    branch = route_after_policy(state)
    state = _merge(state, _run_branch(state, branch))
    state = _merge(state, increment_iteration(state))

    # garantizar respuesta final
    if state.get("final") is None:
        state = _merge(state, node_redact(state))
        state = _merge(state, increment_iteration(state))

    # cap de iteraciones
    if state.get("iterations", 0) > MAX_ITERATIONS:
        state["iterations"] = MAX_ITERATIONS

    return state


class _FallbackGraph:
    """Grafo minimo cuando langgraph no esta disponible."""

    def invoke(self, state: Any) -> Any:
        if isinstance(state, dict):
            return run_pipeline(state)
        # soportar objetos con __dict__
        try:
            return run_pipeline(dict(state))
        except Exception:
            return run_pipeline({})

    def run(self, state: Any) -> Any:
        return self.invoke(state)


def build_graph():
    """Construye el grafo (langgraph si disponible, fallback si no)."""
    if _LANGGRAPH_AVAILABLE and StateGraph is not None:
        try:
            graph = StateGraph(HelpdeskState)

            graph.add_node("entry", node_entry)
            graph.add_node("classify", node_classify)
            graph.add_node("retrieve", node_retrieve)
            graph.add_node("policy", node_policy)
            graph.add_node("approval", node_approval)
            graph.add_node("escalate", node_escalate)
            graph.add_node("redact", node_redact)

            graph.set_entry_point("entry")
            graph.add_edge("entry", "classify")
            graph.add_edge("classify", "retrieve")
            graph.add_edge("retrieve", "policy")

            def _route(state):
                return route_after_policy(state)

            graph.add_conditional_edges(
                "policy",
                _route,
                {"approve": "approval", "escalate": "escalate", "redact": "redact"},
            )
            graph.add_edge("approval", "redact")
            graph.add_edge("escalate", "redact")
            graph.add_edge("redact", END)

            compiled = graph.compile()
            return compiled
        except Exception:
            return _FallbackGraph()
    return _FallbackGraph()


__all__ = ["build_graph", "run_pipeline", "MAX_ITERATIONS", "MAX_TOOL_CALLS", "_LANGGRAPH_AVAILABLE"]
