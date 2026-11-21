"""Configuración central del laboratorio (mock vs live).

Fuente única de verdad para el modo de ejecución de los componentes nuevos
(`api/`, `lab/`). El agente original sigue usando sus propias variables de
entorno; aquí solo se centraliza la configuración de la evolución agentic.

Reglas:
- Por defecto todo es mock (offline, sin credenciales).
- Escrituras externas requieren HELPDESK_ALLOW_WRITES=1 (kill switch heredado).
- Las dependencias opcionales se importan con guardas en los módulos que las usan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _bool(key: str, default: bool = False) -> bool:
    val = _env(key, "1" if default else "0")
    return val == "1"


@dataclass
class LabConfig:
    # --- Modo global ---
    mock_mode: bool = True  # True = todo mock; False = permite live donde haya creds.
    allow_writes: bool = False  # kill switch heredado

    # --- Adapters (heredados del agente original) ---
    itsm_adapter: str = "mock"
    identity_adapter: str = "mock"
    app_adapter: str = "mock"

    # --- LLM ---
    llm_provider: str = "mock"  # mock | openai | anthropic
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "mock-deterministic"
    llm_max_tokens: int = 512

    # --- RAG / Chroma ---
    chroma_path: str = "outputs/chroma"
    embedding_model: str = "mock"

    # --- API ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- Voz ---
    stt_provider: str = "mock"
    tts_provider: str = "mock"

    # --- Observabilidad ---
    log_level: str = "INFO"

    extra: Dict[str, str] = field(default_factory=dict)


def load_config() -> LabConfig:
    cfg = LabConfig(
        mock_mode=_bool("LAB_MOCK_MODE", True) if _env("LAB_MOCK_MODE") else True,
        allow_writes=_bool("HELPDESK_ALLOW_WRITES", False),
        itsm_adapter=_env("HELPDESK_ITSM_ADAPTER", "mock") or "mock",
        identity_adapter=_env("HELPDESK_IDENTITY_ADAPTER", "mock") or "mock",
        app_adapter=_env("HELPDESK_APP_ADAPTER", "mock") or "mock",
        llm_provider=_env("LAB_LLM_PROVIDER", "mock") or "mock",
        openai_api_key=_env("OPENAI_API_KEY", ""),
        anthropic_api_key=_env("ANTHROPIC_API_KEY", ""),
        llm_model=_env("LAB_LLM_MODEL", "mock-deterministic") or "mock-deterministic",
        llm_max_tokens=int(_env("LAB_LLM_MAX_TOKENS", "512") or "512"),
        chroma_path=_env("LAB_CHROMA_PATH", "outputs/chroma") or "outputs/chroma",
        embedding_model=_env("LAB_EMBEDDING_MODEL", "mock") or "mock",
        api_host=_env("LAB_API_HOST", "127.0.0.1") or "127.0.0.1",
        api_port=int(_env("LAB_API_PORT", "8000") or "8000"),
        stt_provider=_env("LAB_STT_PROVIDER", "mock") or "mock",
        tts_provider=_env("LAB_TTS_PROVIDER", "mock") or "mock",
        log_level=_env("LAB_LOG_LEVEL", "INFO") or "INFO",
    )
    # Si no hay credenciales, forzar mock para el proveedor afectado.
    if cfg.llm_provider == "openai" and not cfg.openai_api_key:
        cfg.llm_provider = "mock"
    if cfg.llm_provider == "anthropic" and not cfg.anthropic_api_key:
        cfg.llm_provider = "mock"
    return cfg


# Instancia global perezosa
_CONFIG: LabConfig | None = None


def get_config() -> LabConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def reset_config() -> None:
    """Útil en tests para recargar configuración."""
    global _CONFIG
    _CONFIG = None
