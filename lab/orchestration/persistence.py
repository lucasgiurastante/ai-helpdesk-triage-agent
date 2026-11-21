"""Persistencia local/mock para estado de orquestacion (Fase 04 parte B)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DEFAULT_STATE_PATH = "outputs/orchestration_state.json"

# Intento de usar MemorySaver de langgraph si esta disponible.
try:
    from langgraph.checkpoint.memory import MemorySaver as _LGM  # type: ignore
except Exception:  # pragma: no cover - import guard
    _LGM = None

# Fallback para otras rutas de import.
if _LGM is None:
    try:
        from langgraph.checkpoint import MemorySaver as _LGM  # type: ignore
    except Exception:
        _LGM = None

LANGGRAPH_AVAILABLE = _LGM is not None
MemorySaver = _LGM


def _resolve_path(path: Any) -> Path:
    if path is None:
        path = DEFAULT_STATE_PATH
    return Path(path)


def _json_default(obj: Any):
    try:
        return str(obj)
    except Exception:
        return None


def save_state(state: Any, path: Any = None) -> None:
    """Guarda el estado como JSON en `path` (o en la ruta por defecto)."""
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = state
    if not isinstance(data, dict):
        try:
            data = dict(data)
        except Exception:
            data = {"state": str(data)}
    p.write_text(
        json.dumps(data, default=_json_default, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_state(path: Any = None) -> dict | None:
    """Carga el estado desde `path` (o la ruta por defecto). None si no existe/falla."""
    p = _resolve_path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


if MemorySaver is not None:
    class InMemoryCheckpointer(MemorySaver):  # type: ignore[misc]
        """Checkpointer en memoria: snapshots por thread_id (compatible con MemorySaver)."""

        def __init__(self):
            try:
                super().__init__()
            except Exception:
                pass
            self._store: dict = {}

        def put(self, thread_id, state) -> None:
            self._store[thread_id] = copy.deepcopy(state) if isinstance(state, (dict, list)) else state

        def get(self, thread_id):
            v = self._store.get(thread_id, None)
            if isinstance(v, (dict, list)):
                return copy.deepcopy(v)
            return v

        def list_threads(self) -> list:
            return list(self._store.keys())

        def clear(self) -> None:
            self._store.clear()
else:
    class InMemoryCheckpointer:  # type: ignore[no-redef]
        """Checkpointer en memoria: snapshots por thread_id."""

        def __init__(self):
            self._store: dict = {}

        def put(self, thread_id, state) -> None:
            self._store[thread_id] = copy.deepcopy(state) if isinstance(state, (dict, list)) else state

        def get(self, thread_id):
            v = self._store.get(thread_id, None)
            if isinstance(v, (dict, list)):
                return copy.deepcopy(v)
            return v

        def list_threads(self) -> list:
            return list(self._store.keys())

        def clear(self) -> None:
            self._store.clear()


def get_checkpointer():
    """Devuelve un checkpointer usable.

    Si langgraph esta disponible se utiliza InMemoryCheckpointer (subclase de
    MemorySaver) para conservar la API simple put(thread_id, state)/get(thread_id);
    si no, el mismo InMemoryCheckpointer puro.
    """
    return InMemoryCheckpointer()


# Alias de conveniencia para tests/usuarios.
Checkpointer = MemorySaver if MemorySaver is not None else InMemoryCheckpointer
