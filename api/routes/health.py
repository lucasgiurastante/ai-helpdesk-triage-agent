"""Ruta GET /health del laboratorio."""

from __future__ import annotations

from typing import Any

from api import _require_fastapi

_require_fastapi()

from fastapi import APIRouter

from api.models import HealthResponse
from config import get_config


router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    cfg = get_config()
    mode = "mock" if cfg.mock_mode else "live"
    return HealthResponse(status="ok", mode=mode)
