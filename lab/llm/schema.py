"""Esquema estructurado de respuesta del LLM (Fase 02).

Define el contrato que todo proveedor debe devolver, independiente del backend.
Usa Pydantic si está disponible; de lo contrario, dataclass equivalente.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

try:
    from pydantic import BaseModel, Field
    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover
    BaseModel = object  # type: ignore
    Field = None  # type: ignore
    HAS_PYDANTIC = False


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMMetadata:
    provider: str = "mock"
    model: str = "mock-deterministic"
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: float = 0.0
    raw_response_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "latency_ms": self.latency_ms,
            "raw_response_id": self.raw_response_id,
        }


@dataclass
class LLMResponse:
    """Respuesta estructurada del LLM.

    Campos de negocio (lo que el agente consume):
      - category, priority, suggested_action, suggested_resolution, confidence
      - reasoning: justificación breve (no se filtra contenido sensible)
    Metadatos (LLMMetadata): provider, model, tokens, latencia.
    """
    category: str = "unknown"
    priority: str = "P3"
    suggested_action: str = "human_review"
    suggested_resolution: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    metadata: LLMMetadata = field(default_factory=LLMMetadata)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LLMResponse":
        meta_raw = data.get("metadata") or {}
        usage_raw = meta_raw.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            total_tokens=int(usage_raw.get("total_tokens", 0)),
        )
        metadata = LLMMetadata(
            provider=meta_raw.get("provider", "mock"),
            model=meta_raw.get("model", "mock-deterministic"),
            usage=usage,
            latency_ms=float(meta_raw.get("latency_ms", 0.0)),
            raw_response_id=meta_raw.get("raw_response_id"),
        )
        return cls(
            category=data.get("category", "unknown"),
            priority=data.get("priority", "P3"),
            suggested_action=data.get("suggested_action", "human_review"),
            suggested_resolution=data.get("suggested_resolution", ""),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
            metadata=metadata,
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "LLMResponse":
        return cls.from_dict(json.loads(text))


# Esquema JSON que se pide al LLM (para salida estructurada).
LLM_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
        "suggested_action": {"type": "string"},
        "suggested_resolution": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reasoning": {"type": "string"},
    },
    "required": ["category", "priority", "suggested_action", "confidence"],
}
