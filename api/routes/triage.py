"""Ruta POST /triage del laboratorio."""

from __future__ import annotations

from typing import Any

from api import _require_fastapi

_require_fastapi()

from fastapi import APIRouter, HTTPException

from api.models import TriageRequest, TriageResponse
from api.services import get_triage_service


router = APIRouter()


@router.post("/triage", response_model=TriageResponse, tags=["triage"])
def triage(request: TriageRequest) -> TriageResponse:
    if request.is_empty():
        raise HTTPException(
            status_code=422,
            detail={"error": "empty_input", "message": "description or title is required"},
        )
    service = get_triage_service()
    payload = request.model_dump()
    if not payload.get("description") and payload.get("title"):
        payload["description"] = payload["title"]
    try:
        result = service.run(payload)
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail={"error": "internal", "message": str(exc)})
    data = {k: result.get(k) for k in TriageResponse.model_fields if k in result}
    return TriageResponse(**data)
