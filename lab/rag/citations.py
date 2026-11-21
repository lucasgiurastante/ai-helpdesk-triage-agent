"""Citas para respuestas RAG (Fase 03 parte B)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class Citation:
    doc_id: str
    title: str = ""
    section: str = ""
    snippet: str = ""
    score: float = 0.0

    @property
    def heading(self) -> str:
        return self.title or self.section

    @property
    def source(self) -> str:
        return self.title or self.section


def _as_dict(item: Any) -> dict:
    if isinstance(item, dict):
        return item
    out = {}
    for attr in ("doc_id", "id", "text", "metadata", "score"):
        if hasattr(item, attr):
            out[attr] = getattr(item, attr)
    return out


def _resolve_title(meta: Any, item: Any) -> str:
    if isinstance(meta, dict):
        for key in ("title", "section", "heading", "name", "category", "service"):
            if meta.get(key):
                return str(meta.get(key))
    for key in ("title", "section", "heading", "name"):
        val = getattr(item, key, None)
        if val:
            return str(val)
    d = _as_dict(item)
    return str(d.get("doc_id") or d.get("id") or "")


def build_citations(retrieved_docs: Any) -> List[Citation]:
    citations: List[Citation] = []
    if not retrieved_docs:
        return citations
    for rd in retrieved_docs:
        d = _as_dict(rd)
        meta = d.get("metadata")
        if not isinstance(meta, dict):
            meta = getattr(rd, "metadata", {}) or {}
            if not isinstance(meta, dict):
                meta = {}
        text = d.get("text")
        if text is None:
            text = getattr(rd, "text", "") or ""
        text = str(text)
        snippet = text[:200]
        score = d.get("score", getattr(rd, "score", 0.0))
        try:
            score = float(score or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        doc_id = d.get("doc_id")
        if doc_id is None:
            doc_id = d.get("id")
        if doc_id is None:
            doc_id = getattr(rd, "doc_id", getattr(rd, "id", ""))
        title = _resolve_title(meta, rd)
        citations.append(
            Citation(
                doc_id=str(doc_id or ""),
                title=title,
                section=title,
                snippet=snippet,
                score=score,
            )
        )
    return citations


def format_citations_for_prompt(citations: Any) -> str:
    if not citations:
        return ""
    lines = ["Citas de la base de conocimiento:"]
    for i, c in enumerate(citations, 1):
        cid = getattr(c, "doc_id", "")
        title = getattr(c, "title", "") or getattr(c, "section", "")
        snippet = getattr(c, "snippet", "")
        score = getattr(c, "score", 0.0)
        try:
            score_str = f"{float(score):.2f}"
        except (TypeError, ValueError):
            score_str = str(score)
        lines.append(f"[{i}] {cid} - {title}: \"{snippet}\" (score={score_str})")
    return "\n".join(lines)
