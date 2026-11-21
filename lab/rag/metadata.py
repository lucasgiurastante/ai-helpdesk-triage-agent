"""Metadatos y versionado para documentos del RAG."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict


def _canonical(doc: Any) -> str:
    try:
        return json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(doc)


def content_hash(doc: Any) -> str:
    """SHA-256 del contenido canónico del documento."""
    return hashlib.sha256(_canonical(doc).encode("utf-8")).hexdigest()


def version_of(doc: Any) -> Any:
    """Devuelve la versión de un documento (si existe)."""
    if isinstance(doc, dict):
        return doc.get("version")
    return None


def add_metadata(doc: Any, version: Any, source: Any) -> Any:
    """Añade metadatos estándar a un documento.

    Campos añadidos: ``doc_id``, ``version``, ``source``, ``ingested_at``,
    ``content_hash`` (sha256). Devuelve un nuevo dict (no muta el original).
    """
    base: Dict[str, Any]
    if isinstance(doc, dict):
        base = dict(doc)
    else:
        base = {"text": str(doc)}

    doc_id = base.get("doc_id") or base.get("id") or base.get("document_id")
    if doc_id is None:
        doc_id = content_hash(base)[:12]

    base["doc_id"] = str(doc_id)
    base["version"] = version
    base["source"] = source
    base["ingested_at"] = datetime.now(timezone.utc).isoformat()
    # hash del documento original (sin los metadatos añadidos)
    original = {k: v for k, v in base.items() if k not in ("doc_id", "version", "source", "ingested_at", "content_hash")}
    # si el original estaba vacío, usar el doc completo de entrada
    if not original:
        original = doc
    base["content_hash"] = content_hash(original)
    return base
