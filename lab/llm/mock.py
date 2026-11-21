"""Proveedor LLM mock determinista (Fase 02).

No requiere red ni credenciales. Útil para desarrollo, tests y modo offline.
Aplica heurísticas simples sobre el prompt para producir una LLMResponse
estructurada y determinista.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

from lab.llm.base import LLMProvider
from lab.llm.schema import LLMResponse, LLMMetadata, LLMUsage

# Mapeo keyword -> (category, action, priority, confidence)
_RULES = [
    (["vpn", "remote access", "tunnel"], "vpn", "restart_service", "P2", 0.85),
    (["password", "reset", "locked out", "mfa"], "password_reset", "reset_password", "P3", 0.9),
    (["email", "mailbox", "outlook", "smtp"], "email", "check_mailbox", "P3", 0.8),
    (["disk", "storage", "space", "quota"], "storage", "cleanup_storage", "P2", 0.8),
    (["network", "wifi", "dns", "connectivity"], "network", "diagnose_network", "P2", 0.75),
    (["permission", "access", "role", "group"], "access", "grant_access", "P3", 0.75),
]


class MockLLMProvider(LLMProvider):
    name = "mock"
    model = "mock-deterministic"

    def __init__(self, model: str = "mock-deterministic") -> None:
        self.model = model

    def is_available(self) -> bool:
        return True

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
        text = (prompt or "").lower()
        category = "unknown"
        action = "human_review"
        priority = "P3"
        confidence = 0.4
        for keywords, cat, act, prio, conf in _RULES:
            if any(kw in text for kw in keywords):
                category = cat
                action = act
                priority = prio
                confidence = conf
                break

        reasoning = "mock: clasificación por keywords del prompt"
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        prompt_tokens = max(1, len(prompt.split()))
        completion_tokens = max(1, len(reasoning.split()))
        usage = LLMUsage(
            prompt_tokens=min(prompt_tokens, max_tokens),
            completion_tokens=completion_tokens,
            total_tokens=min(prompt_tokens, max_tokens) + completion_tokens,
        )
        metadata = LLMMetadata(
            provider=self.name,
            model=self.model,
            usage=usage,
            latency_ms=round(elapsed_ms, 3),
            raw_response_id=None,
        )
        return LLMResponse(
            category=category,
            priority=priority,
            suggested_action=action,
            suggested_resolution="",
            confidence=confidence,
            reasoning=reasoning,
            metadata=metadata,
        )
