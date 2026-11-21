"""Interfaz común de proveedores LLM (Fase 02).

Todo proveedor implementa LLMProvider.complete(prompt, *, system, max_tokens, temperature).
Devuelve LLMResponse (esquema estructurado).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from lab.llm.schema import LLMResponse


class LLMProvider(ABC):
    """Interfaz común para todos los proveedores de LLM."""

    name: str = "base"
    model: str = "base"

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """Genera una respuesta estructurada a partir del prompt."""

    def is_available(self) -> bool:
        return True

    def metadata(self) -> Dict[str, Any]:
        return {"provider": self.name, "model": self.model}
