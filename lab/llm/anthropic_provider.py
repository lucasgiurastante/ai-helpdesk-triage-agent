"""Proveedor LLM Anthropic (Fase 02).

Requiere la librería ``anthropic`` y la variable de entorno ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from lab.llm.base import LLMProvider
from lab.llm.schema import LLMResponse, LLMMetadata, LLMUsage

ANTHROPIC_AVAILABLE = False
try:
    import anthropic  # type: ignore
    ANTHROPIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    anthropic = None  # type: ignore


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    model = "claude-3-5-haiku-latest"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        if model:
            self.model = model
        self._client = None
        if ANTHROPIC_AVAILABLE and self.api_key:
            try:
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except Exception:
                self._client = None

    def is_available(self) -> bool:
        return bool(ANTHROPIC_AVAILABLE and self._client is not None)

    def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> LLMResponse:
        start = time.perf_counter()
        if not self.is_available():
            return self._error_response("anthropic no disponible (librería o API key ausente)", start)

        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or "",
                messages=[{"role": "user", "content": prompt}],
            )
            content = ""
            for block in getattr(msg, "content", []) or []:
                if getattr(block, "type", None) == "text":
                    content += getattr(block, "text", "")
            data = {}
            try:
                data = json.loads(content)
            except Exception:
                # Intentar extraer JSON del texto
                import re
                m = re.search(r"\{.*\}", content, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except Exception:
                        data = {}
            usage_raw = getattr(msg, "usage", None)
            usage = LLMUsage(
                prompt_tokens=getattr(usage_raw, "input_tokens", 0) if usage_raw else 0,
                completion_tokens=getattr(usage_raw, "output_tokens", 0) if usage_raw else 0,
                total_tokens=(getattr(usage_raw, "input_tokens", 0) + getattr(usage_raw, "output_tokens", 0)) if usage_raw else 0,
            )
            elapsed = (time.perf_counter() - start) * 1000.0
            metadata = LLMMetadata(
                provider=self.name,
                model=self.model,
                usage=usage,
                latency_ms=round(elapsed, 3),
                raw_response_id=getattr(msg, "id", None),
            )
            data_with_meta = dict(data)
            data_with_meta["metadata"] = metadata.to_dict()
            return LLMResponse.from_dict(data_with_meta)
        except Exception as exc:
            return self._error_response(f"anthropic error: {exc}", start)

    def _error_response(self, message: str, start: float) -> LLMResponse:
        elapsed = (time.perf_counter() - start) * 1000.0
        return LLMResponse(
            category="unknown",
            priority="P3",
            suggested_action="human_review",
            confidence=0.0,
            reasoning=f"error: {message}",
            metadata=LLMMetadata(provider=self.name, model=self.model, latency_ms=round(elapsed, 3)),
        )
