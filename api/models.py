"""Modelos Pydantic del laboratorio (Fase 01).

Define el contrato de entrada/salida del endpoint POST /triage y del
endpoint GET /health. Se importan con guardas desde api/__init__.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from pydantic import BaseModel, Field
    PYDANTIC_AVAILABLE = True
except ImportError:  # pragma: no cover - guard
    BaseModel = object  # type: ignore
    Field = None  # type: ignore
    PYDANTIC_AVAILABLE = False


class HealthResponse(BaseModel):  # type: ignore[misc]
    status: str = "ok"
    mode: str = "mock"
    version: str = "0.1.0"


class TriageRequest(BaseModel):  # type: ignore[misc]
    ticket_id: str = Field(default="", description="Identificador del ticket", max_length=64)
    requester: str = Field(default="", description="Solicitante (email o nombre)", max_length=128)
    description: str = Field(default="", description="Texto libre del ticket", max_length=4000)
    title: Optional[str] = Field(default=None, description="Título opcional", max_length=256)
    service: Optional[str] = Field(default=None, description="Servicio afectado", max_length=64)
    vip: bool = Field(default=False, description="¿Solicitante VIP?")

    def is_empty(self) -> bool:
        return not (self.description or self.title)


class ScoreBreakdown(BaseModel):  # type: ignore[misc]
    confidence: float = 0.0
    coverage: float = 0.0
    evidence: float = 0.0
    specificity: float = 0.0
    service_bonus: float = 0.0
    weighted_matched: int = 0
    weighted_total: int = 0


class TriageResponse(BaseModel):  # type: ignore[misc]
    ticket_id: str = ""
    requester: str = ""
    service: str = ""
    normalized_text: str = ""
    intent: str = ""
    category: str = ""
    priority: str = ""
    impact: str = ""
    urgency: str = ""
    confidence: float = 0.0
    kb_id: Optional[str] = None
    matched_keywords: List[str] = Field(default_factory=list)
    matched_strong_keywords: List[str] = Field(default_factory=list)
    score_breakdown: Dict[str, Any] = Field(default_factory=dict)
    suggested_action: str = ""
    suggested_resolution: str = ""
    base_risk: str = ""
    entities: Dict[str, Any] = Field(default_factory=dict)
    alternatives: List[Dict[str, Any]] = Field(default_factory=list)
    request_id: Optional[str] = None
