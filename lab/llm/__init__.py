"""Paquete LLM del laboratorio (Fase 02).

Proveedores de LLM con interfaz común: mock determinista, OpenAI y Anthropic.
Las dependencias opcionales (openai, anthropic) se importan con guardas.
"""

from lab.llm.base import LLMProvider
from lab.llm.factory import create_provider
from lab.llm.mock import MockLLMProvider
from lab.llm.schema import LLMResponse, LLMMetadata, LLMUsage, LLM_JSON_SCHEMA
from lab.llm.validation import validate_response, is_failed

__all__ = [
    "LLMProvider",
    "create_provider",
    "MockLLMProvider",
    "LLMResponse",
    "LLMMetadata",
    "LLMUsage",
    "LLM_JSON_SCHEMA",
    "validate_response",
    "is_failed",
]
