"""Paquete de la API HTTP del laboratorio (FastAPI).

Expone el motor original (agent.py, services/) como servicio HTTP. Las
dependencias opcionales (fastapi, pydantic, uvicorn) se importan con guardas;
si no están instaladas, se lanza un ImportError explicativo.
"""

from __future__ import annotations

FASTAPI_AVAILABLE = False
PYDANTIC_AVAILABLE = False

try:
    import fastapi  # noqa: F401
    FASTAPI_AVAILABLE = True
except ImportError:
    fastapi = None  # type: ignore

try:
    import pydantic  # noqa: F401
    PYDANTIC_AVAILABLE = True
except ImportError:
    pydantic = None  # type: ignore


def _require_fastapi() -> None:
    if not FASTAPI_AVAILABLE or not PYDANTIC_AVAILABLE:
        raise ImportError(
            "FastAPI/Pydantic no están instalados. "
            "Instala las dependencias del laboratorio: "
            "pip install -r requirements-lab.txt"
        )
