"""Proveedor LLM OpenAI (Fase 02).

Requiere la librería ``openai`` y la variable de entorno OPENAI_API_KEY.
Se importa con guardas; si no está disponible, is_available() devuelve False.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from lab.llm.base import LLMProvider
from lab.llm.schema import LLMResponse, LLMMetadata, LLMUsage, LLM_JSON_SCHEMA

OPENAI_AVAILABLE = False
try:
    import openai  # type: ignore
    OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    openai = None  # type: ignore


class OpenAIProvider(LLMProvider):
    name = "openai"
    model = "gpt-4o-mini"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        if model:
            self.model = model
        self._client = None
        if OPENAI_AVAILABLE and self.api_key:
            try:
                self._client = openai.OpenAI(api_key=self.api_key)
            except Exception:
                self._client = None

    def is_available(self) -> bool:
        return bool(OPENAI_AVAILABLE and self._client is not None)

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
            return self._error_response("openai no disponible (librería o API key ausente)", start)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or "{}"
            data = json.loads(content)
            usage_raw = getattr(resp, "usage", None)
            usage = LLMUsage(
                prompt_tokens=getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
                completion_tokens=getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0,
                total_tokens=getattr(usage_raw, "total_tokens", 0) if usage_raw else 0,
            )
            elapsed = (time.perf_counter() - start) * 1000.0
            metadata = LLMMetadata(
                provider=self.name,
                model=self.model,
                usage=usage,
                latency_ms=round(elapsed, 3),
                raw_response_id=getattr(resp, "id", None),
            )
            data_with_meta = dict(data)
            data_with_meta["metadata"] = metadata.to_dict()
            return LLMResponse.from_dict(data_with_meta)
        except Exception as exc:
            return self._error_response(f"openai error: {exc}", start)

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
