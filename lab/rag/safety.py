"""Seguridad del flujo RAG frente a instrucciones en documentos (Fase 03 parte B)."""

from __future__ import annotations

import re
from typing import Any

# Patrones sospechosos (instrucciones inyectadas en documentos).
_SUSPICIOUS_PATTERNS = [
    r"ignore\s+(all\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules)",
    r"disregard\s+(all\s+|the\s+)?(previous|prior|above|earlier)",
    r"system\s*:",
    r"you\s+are\s+now",
    r"new\s+instructions\s*:",
    r"forget\s+(everything|all|previous)",
    r"override\s+(the\s+)?(system|instructions|rules)",
    r"act\s+as\s+(if|a|an)",
    r"<\s*/?\s*system\s*>",
    r"\[\s*INST\s*\]",
    r"###\s*instruction",
]

_COMPILED = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _SUSPICIOUS_PATTERNS]


def is_suspicious(text: Any) -> bool:
    if text is None:
        return False
    t = str(text)
    return any(rx.search(t) for rx in _COMPILED)


def sanitize_document(text: Any) -> str:
    """Neutraliza patrones de inyeccion de instrucciones en documentos."""
    if text is None:
        return ""
    t = str(text)
    for rx in _COMPILED:
        t = rx.sub("[REDACTED]", t)
    return t


def wrap_context(text: Any) -> str:
    """Envuelve el texto en delimitadores que lo marcan como datos, no instrucciones."""
    t = "" if text is None else str(text)
    return (
        "<<<UNTRUSTED_DOCUMENT_START>>>\n"
        + t
        + "\n<<<UNTRUSTED_DOCUMENT_END>>>"
    )
