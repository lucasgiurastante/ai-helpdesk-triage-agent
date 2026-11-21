"""Estrategia de chunking para documentos RAG."""

from __future__ import annotations

import re
from typing import Any, Dict, List


def _split_sentences(text: str) -> List[str]:
    # Divide por párrafos y luego por oraciones.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    sentences: List[str] = []
    for para in paragraphs:
        # oraciones terminadas en . ! ? seguidas de espacio/mayúscula o fin
        parts = re.split(r"(?<=[.!?])\s+", para)
        for part in parts:
            part = part.strip()
            if part:
                sentences.append(part)
    if not sentences and text.strip():
        sentences = [text.strip()]
    return sentences


def chunk_text(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[Dict[str, Any]]:
    """Divide ``text`` en chunks de hasta ``chunk_size`` caracteres.

    Intenta respetar límites de oración/párrafo. Devuelve lista de dicts:
    ``{"text": str, "metadata": {"chunk_index", "start_char", "end_char"}}``.
    """
    text = text or ""
    if not text.strip():
        return []

    chunk_size = max(1, int(chunk_size))
    overlap = max(0, int(overlap))
    if overlap >= chunk_size:
        overlap = chunk_size // 4

    sentences = _split_sentences(text)
    chunks: List[Dict[str, Any]] = []
    current: List[str] = []
    current_len = 0
    start_char = 0

    def flush(end_char: int) -> None:
        nonlocal current, current_len, start_char
        if not current:
            return
        chunk = " ".join(current).strip()
        if chunk:
            chunks.append(
                {
                    "text": chunk,
                    "chunk": chunk,
                    "metadata": {
                        "chunk_index": len(chunks),
                        "start_char": start_char,
                        "end_char": end_char,
                        "length": len(chunk),
                    },
                }
            )
        # overlap: conservar cola
        if overlap > 0 and current:
            tail = chunk[-overlap:]
            current = [tail]
            current_len = len(tail)
            start_char = max(0, end_char - overlap)
        else:
            current = []
            current_len = 0
            start_char = end_char

    pos = 0
    for sent in sentences:
        idx = text.find(sent, pos)
        if idx == -1:
            idx = pos
        sent_end = idx + len(sent)
        if current and current_len + 1 + len(sent) > chunk_size:
            flush(idx)
        current.append(sent)
        current_len = (current_len + 1 + len(sent)) if current_len else len(sent)
        pos = sent_end

    if current:
        flush(len(text))

    # Si un único chunk excede chunk_size (oración muy larga), cortar por tamaño.
    final: List[Dict[str, Any]] = []
    for ch in chunks:
        if len(ch["text"]) <= chunk_size:
            final.append(ch)
        else:
            t = ch["text"]
            i = 0
            while i < len(t):
                piece = t[i : i + chunk_size]
                final.append(
                    {
                        "text": piece,
                        "chunk": piece,
                        "metadata": {
                            "chunk_index": len(final),
                            "start_char": ch["metadata"]["start_char"] + i,
                            "end_char": ch["metadata"]["start_char"] + i + len(piece),
                            "length": len(piece),
                        },
                    }
                )
                i += chunk_size - overlap
    # reindexar
    for i, ch in enumerate(final):
        ch["metadata"]["chunk_index"] = i
    return final
