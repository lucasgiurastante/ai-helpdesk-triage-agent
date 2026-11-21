"""Creación reproducible del índice vectorial."""

from __future__ import annotations

from typing import Any

from config import get_config

from lab.rag.ingestion import _to_store_doc, load_kb
from lab.rag.store import VectorStore


def build_index(store: VectorStore, kb_path: str) -> int:
    """Construye el índice de forma determinista (docs ordenados por id)."""
    docs = load_kb(kb_path)
    docs.sort(key=lambda d: str(d.get("doc_id") or d.get("id") or ""))

    store.clear()
    store_docs = [_to_store_doc(d) for d in docs]
    n = store.add_documents(store_docs)

    cfg = get_config()
    store.persist(cfg.chroma_path)
    return n


def rebuild(store: VectorStore, kb_path: str) -> int:
    """Reconstruye el índice desde cero."""
    store.clear()
    return build_index(store, kb_path)
