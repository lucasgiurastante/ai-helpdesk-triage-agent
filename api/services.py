"""Servicio de triage del laboratorio.

Adapta el motor original (services.triage.TriageService) a la API HTTP,
sin duplicar lógica. Importa el motor original como librería.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.triage import TriageService


def _load_json(relative: str) -> Any:
    path = REPO_ROOT / relative
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class TriageLabService:
    """Envuelve TriageService para uso desde la API."""

    def __init__(self) -> None:
        kb = _load_json("data/kb.json")
        risk_matrix = _load_json("policies/risk_matrix.json")
        self._triage = TriageService(
            kb,
            risk_matrix.get("priority_matrix"),
            risk_matrix.get("category_overrides"),
        )

    def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        result = self._triage.classify(payload)
        result = dict(result)
        result["request_id"] = str(uuid.uuid4())
        return result


def get_triage_service() -> TriageLabService:
    return TriageLabService()
