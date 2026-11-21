"""Recuperacion top-k con filtros para RAG (Fase 03 parte B)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RetrievedDoc:
    doc_id: str
    text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


def _score_of(item: Any) -> float:
    if isinstance(item, dict):
        try:
            return float(item.get("score", item.get("similarity", 0.0)) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    for attr in ("score", "similarity"):
        val = getattr(item, attr, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return 0.0


def _doc_id_of(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("doc_id", "id", "document_id", "uuid"):
            if item.get(key) is not None:
                return str(item.get(key))
        return ""
    for attr in ("doc_id", "id", "document_id", "uuid"):
        val = getattr(item, attr, None)
        if val is not None:
            return str(val)
    return ""


def _text_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("content") or item.get("page_content") or item.get("body") or "")
    for attr in ("text", "content", "page_content", "body"):
        val = getattr(item, attr, None)
        if val is not None:
            return str(val)
    return str(item)


def _metadata_of(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        meta = item.get("metadata")
        if isinstance(meta, dict):
            return dict(meta)
        return {k: v for k, v in item.items() if k not in ("doc_id", "id", "document_id", "uuid", "text", "content", "page_content", "body", "score", "similarity", "distance")}
    meta = getattr(item, "metadata", None)
    if isinstance(meta, dict):
        return dict(meta)
    return {}


def _apply_filters(item_meta: Dict[str, Any], filters: Optional[Dict[str, Any]]) -> bool:
    if not filters:
        return True
    for key, val in filters.items():
        if item_meta.get(key) != val:
            return False
    return True


def retrieve(
    store: Any,
    query: str,
    k: int = 5,
    filters: Optional[Dict[str, Any]] = None,
    min_score: float = 0.0,
) -> List[RetrievedDoc]:
    k = max(1, int(k or 1))
    results: List[RetrievedDoc] = []

    raw: List[Any] = []
    if store is not None:
        q = getattr(store, "query", None)
        if callable(q):
            try:
                raw = q(query, k=k, filters=filters) or []
            except TypeError:
                try:
                    raw = q(query, k=k) or []
                except Exception:
                    raw = []
            except Exception:
                raw = []
        if filters and raw:
            filtered = [r for r in raw if _apply_filters(_metadata_of(r), filters)]
            if len(filtered) != len(raw):
                raw = filtered

    if not raw and store is not None:
        docs = getattr(store, "documents", None)
        if docs is None:
            docs = getattr(store, "_docs", None)
        if isinstance(docs, dict):
            docs = list(docs.values())
        if isinstance(docs, list):
            for d in docs:
                if _apply_filters(_metadata_of(d), filters):
                    raw.append(d)

    for r in raw:
        meta = _metadata_of(r)
        if not _apply_filters(meta, filters):
            continue
        score = _score_of(r)
        if score < (min_score or 0.0):
            continue
        results.append(
            RetrievedDoc(
                doc_id=_doc_id_of(r),
                text=_text_of(r),
                metadata=meta,
                score=score,
            )
        )

    results.sort(key=lambda d: d.score, reverse=True)
    return results[:k]
