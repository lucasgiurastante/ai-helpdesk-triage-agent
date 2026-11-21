"""Limites de iteraciones y llamadas a herramientas (Fase 04 parte B)."""

from __future__ import annotations

from typing import Any

MAX_ITERATIONS = 5
MAX_TOOL_CALLS = 10

_ITER_KEYS = ("iterations", "iteration_count", "iteration", "num_iterations")
_TOOL_KEYS = ("tool_calls", "tool_call_count", "num_tool_calls", "tool_calls_count")


def _num(state: Any, keys, default: int = 0) -> int:
    if isinstance(state, dict):
        for k in keys:
            if k in state:
                try:
                    return int(state.get(k) or 0)
                except (TypeError, ValueError):
                    continue
        return default
    for k in keys:
        v = getattr(state, k, None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return default


def _present_keys(state: Any, keys) -> list:
    if isinstance(state, dict):
        found = [k for k in keys if k in state]
        return found if found else []
    found = [k for k in keys if getattr(state, k, None) is not None]
    return found if found else []


def check_iteration_limit(state) -> bool:
    """True si se alcanzo/superó el limite de iteraciones."""
    return _num(state, _ITER_KEYS) >= MAX_ITERATIONS


def check_tool_call_limit(state) -> bool:
    """True si se alcanzo/superó el limite de llamadas a herramientas."""
    return _num(state, _TOOL_KEYS) >= MAX_TOOL_CALLS


def increment_iteration(state) -> dict:
    """Incrementa el contador de iteraciones y devuelve el delta (partial state)."""
    keys = _present_keys(state, _ITER_KEYS)
    if not keys:
        keys = ["iterations"]
    cur = _num(state, keys)
    new = cur + 1
    out = {k: new for k in keys}
    if isinstance(state, dict):
        for k in keys:
            state[k] = new
        if "iterations" not in state:
            state["iterations"] = new
            out.setdefault("iterations", new)
    return out


def increment_tool_call(state, n: int = 1) -> dict:
    """Incrementa el contador de llamadas a herramientas en n."""
    try:
        step = int(n)
    except (TypeError, ValueError):
        step = 1
    keys = _present_keys(state, _TOOL_KEYS)
    if not keys:
        keys = ["tool_calls"]
    cur = _num(state, keys)
    new = cur + step
    out = {k: new for k in keys}
    if isinstance(state, dict):
        for k in keys:
            state[k] = new
        if "tool_calls" not in state:
            state["tool_calls"] = new
            out.setdefault("tool_calls", new)
    return out
