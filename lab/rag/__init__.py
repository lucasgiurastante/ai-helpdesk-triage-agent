"""Paquete RAG del laboratorio (Fase 03).

Ingesta, metadatos, chunking e índice vectorial (Chroma o fallback in-memory).
"""

from lab.rag.chunking import chunk_text
from lab.rag.index import build_index, rebuild
from lab.rag.ingestion import (
    ingest_kb,
    load_kb,
    remove_document,
    update_document,
)
from lab.rag.metadata import add_metadata, content_hash, version_of
from lab.rag.store import VectorStore

from lab.rag.retrieval import RetrievedDoc, retrieve
from lab.rag.citations import Citation, build_citations, format_citations_for_prompt
from lab.rag.abstention import should_abstain, abstain_response
from lab.rag.safety import is_suspicious, sanitize_document, wrap_context

__all__ = [
    "RetrievedDoc",
    "retrieve",
    "Citation",
    "build_citations",
    "format_citations_for_prompt",
    "should_abstain",
    "abstain_response",
    "is_suspicious",
    "sanitize_document",
    "wrap_context",
    "VectorStore",
    "add_metadata",
    "content_hash",
    "version_of",
    "chunk_text",
    "load_kb",
    "ingest_kb",
    "update_document",
    "remove_document",
    "build_index",
    "rebuild",
]
