"""Factory de proveedores LLM (Fase 02).

Selecciona el proveedor según la configuración central (config.py).
Si el proveedor real no está disponible, hace fallback a mock.
"""

from __future__ import annotations

from typing import Optional

from config import get_config
from lab.llm.base import LLMProvider
from lab.llm.mock import MockLLMProvider


def create_provider(provider: Optional[str] = None, model: Optional[str] = None) -> LLMProvider:
    cfg = get_config()
    name = (provider or cfg.llm_provider or "mock").lower()

    if name == "openai":
        from lab.llm.openai_provider import OpenAIProvider

        p = OpenAIProvider(api_key=cfg.openai_api_key, model=model or cfg.llm_model)
        if p.is_available():
            return p
        # fallback
        return MockLLMProvider()

    if name == "anthropic":
        from lab.llm.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(api_key=cfg.anthropic_api_key, model=model or cfg.llm_model)
        if p.is_available():
            return p
        return MockLLMProvider()

    # default mock
    return MockLLMProvider(model=model or "mock-deterministic")
