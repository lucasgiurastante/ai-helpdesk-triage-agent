"""Ingesta de la base de conocimiento (KB) en el almacén vectorial."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from lab.rag.metadata import add_metadata
from lab.rag.store import VectorStore


def _build_text(doc: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("title", "resolution", "resolution_en"):
        val = doc.get(key)
        if val:
            parts.append(str(val))
    for key in ("runbook", "keywords", "strong_keywords"):
        val = doc.get(key)
        if isinstance(val, list):
            parts.extend(str(x) for x in val if x)
        elif val:
            parts.append(str(val))
    service = doc.get("service") or doc.get("category")
    if service:
        parts.append(str(service))
    return "\n".join(p for p in parts if p).strip()


def load_kb(path: str) -> List[Dict[str, Any]]:
    """Carga ``kb.json`` y devuelve lista de documentos con metadatos."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        data = [data]

    docs: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        doc = dict(item)
        doc["text"] = _build_text(doc)
        doc = add_metadata(doc, version="1.0", source=os.path.basename(path))
        docs.append(doc)

    docs.sort(key=lambda d: str(d.get("doc_id") or d.get("id") or ""))
    return docs


def _to_store_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc_id = doc.get("doc_id") or doc.get("id")
    text = doc.get("text") or _build_text(doc)
    metadata = {k: v for k, v in doc.items() if k != "text"}
    return {"id": str(doc_id), "text": text, "metadata": metadata}


def ingest_kb(store: VectorStore, kb_path: str) -> int:
    """Indexa todos los documentos de la KB en ``store``."""
    docs = load_kb(kb_path)
    store_docs = [_to_store_doc(d) for d in docs]
    return store.add_documents(store_docs)


def update_document(store: VectorStore, doc_id: str, new_doc: Any) -> int:
    """Actualiza (reemplaza) un documento por id."""
    store.delete(doc_id)
    if isinstance(new_doc, dict):
        d = dict(new_doc)
    else:
        d = {"text": str(new_doc)}
    if "id" not in d and "doc_id" not in d:
        d["id"] = doc_id
    d.setdefault("doc_id", doc_id)
    d = add_metadata(d, version=d.get("version", "1.0"), source=d.get("source", "update"))
    return store.add_documents([_to_store_doc(d)])


def remove_document(store: VectorStore, doc_id: str) -> int:
    """Elimina un documento del almacén."""
    return store.delete(doc_id)
