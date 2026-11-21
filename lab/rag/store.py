"""Wrapper de almacenamiento vectorial (Chroma o fallback in-memory).

La dependencia ``chromadb`` es opcional: si no está instalada se usa un
almacén en memoria basado en solapamiento de tokens (suficiente para tests
y desarrollo offline).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

try:
    import chromadb  # type: ignore

    _CHROMA_AVAILABLE = True
except Exception:  # pragma: no cover - depende del entorno
    chromadb = None  # type: ignore
    _CHROMA_AVAILABLE = False


def _tokens(text: str) -> set:
    return set(re.findall(r"\w+", (text or "").lower()))


def _sanitize_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte metadatos a tipos aceptados por Chroma."""
    out: Dict[str, Any] = {}
    for key, value in (meta or {}).items():
        if value is None:
            out[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, (list, tuple, dict)):
            try:
                out[key] = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                out[key] = str(value)
        else:
            out[key] = str(value)
    return out


def _normalize_doc(doc: Any) -> tuple:
    if isinstance(doc, str):
        return None, doc, {}
    if not isinstance(doc, dict):
        return None, str(doc), {}
    doc_id = doc.get("id") or doc.get("doc_id") or doc.get("document_id") or doc.get("uuid")
    text = (
        doc.get("text")
        or doc.get("content")
        or doc.get("page_content")
        or doc.get("body")
        or ""
    )
    metadata = doc.get("metadata")
    if metadata is None:
        # todo lo que no sea id/text se considera metadata
        metadata = {
            k: v for k, v in doc.items() if k not in ("id", "doc_id", "document_id", "uuid", "text", "content", "page_content", "body", "metadata")
        }
    if not isinstance(metadata, dict):
        metadata = {"metadata": metadata}
    if doc_id is None:
        doc_id = "doc-" + _content_hash(text)
    return str(doc_id), str(text), dict(metadata)


def _content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class VectorStore:
    """Almacén vectorial con backend Chroma o fallback in-memory."""

    def __init__(
        self,
        collection_name: str = "kb",
        persist_path: Optional[str] = None,
        embedding_model: Optional[str] = None,
    ) -> None:
        self.collection_name = collection_name
        self.persist_path = persist_path
        self.embedding_model = embedding_model
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._chroma = False
        self._client = None
        self._collection = None

        if _CHROMA_AVAILABLE:
            try:
                if persist_path:
                    os.makedirs(persist_path, exist_ok=True)
                    self._client = chromadb.PersistentClient(path=persist_path)
                else:
                    self._client = chromadb.Client()
                self._collection = self._client.get_or_create_collection(
                    name=collection_name
                )
                self._chroma = True
            except Exception:
                self._chroma = False
                self._client = None
                self._collection = None

    # ------------------------------------------------------------------ #
    def is_available(self) -> bool:
        return bool(_CHROMA_AVAILABLE and self._chroma)

    def backend(self) -> str:
        return "chroma" if self._chroma else "memory"

    # ------------------------------------------------------------------ #
    def add_documents(self, docs: List[Any]) -> int:
        if not docs:
            return 0
        if self._chroma:
            ids: List[str] = []
            documents: List[str] = []
            metadatas: List[Dict[str, Any]] = []
            for doc in docs:
                did, text, meta = _normalize_doc(doc)
                ids.append(did)
                documents.append(text)
                metadatas.append(_sanitize_metadata(meta))
            try:
                self._collection.add(ids=ids, documents=documents, metadatas=metadatas)
            except Exception:
                # Algunas versiones requieren upsert si ya existen.
                try:
                    self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
                except Exception:
                    raise
            return len(ids)
        # fallback
        count = 0
        for doc in docs:
            did, text, meta = _normalize_doc(doc)
            self._docs[did] = {"id": did, "text": text, "metadata": meta}
            count += 1
        return count

    def query(
        self,
        text: str,
        k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        k = max(1, int(k or 1))
        if self._chroma:
            try:
                kwargs: Dict[str, Any] = {"query_texts": [text], "n_results": k}
                if filters:
                    kwargs["where"] = filters
                res = self._collection.query(**kwargs)
            except Exception:
                return []
            out: List[Dict[str, Any]] = []
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for i, did in enumerate(ids):
                out.append(
                    {
                        "id": did,
                        "text": docs[i] if i < len(docs) else "",
                        "metadata": metas[i] if i < len(metas) else {},
                        "score": (1.0 - float(dists[i])) if i < len(dists) and dists[i] is not None else 0.0,
                        "distance": dists[i] if i < len(dists) else None,
                    }
                )
            return out
        # fallback
        results: List[Dict[str, Any]] = []
        q_tokens = _tokens(text)
        for did, rec in self._docs.items():
            meta = rec.get("metadata") or {}
            if filters:
                ok = True
                for key, val in filters.items():
                    if meta.get(key) != val:
                        ok = False
                        break
                if not ok:
                    continue
            score = self._score(text, rec["text"])
            results.append(
                {
                    "id": did,
                    "text": rec["text"],
                    "metadata": meta,
                    "score": score,
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:k]

    def _score(self, query: str, doc: str) -> float:
        tq = _tokens(query)
        td = _tokens(doc)
        if not tq or not td:
            return 0.0
        inter = len(tq & td)
        return inter / ((len(tq) * len(td)) ** 0.5)

    def delete(self, ids: Any) -> int:
        if isinstance(ids, str):
            ids = [ids]
        ids = [str(x) for x in (ids or [])]
        if not ids:
            return 0
        if self._chroma:
            try:
                self._collection.delete(ids=ids)
            except Exception:
                pass
            return len(ids)
        removed = 0
        for did in ids:
            if did in self._docs:
                del self._docs[did]
                removed += 1
        return removed

    def clear(self) -> None:
        if self._chroma:
            try:
                # recrear colección
                self._client.delete_collection(self.collection_name)
            except Exception:
                pass
            try:
                self._collection = self._client.get_or_create_collection(
                    name=self.collection_name
                )
            except Exception:
                pass
            return
        self._docs.clear()

    def count(self) -> int:
        if self._chroma:
            try:
                return self._collection.count()
            except Exception:
                return 0
        return len(self._docs)

    def get(self, ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if self._chroma:
            try:
                if ids:
                    res = self._collection.get(ids=[str(x) for x in ids])
                else:
                    res = self._collection.get()
            except Exception:
                return []
            out = []
            for i, did in enumerate(res.get("ids") or []):
                docs = res.get("documents") or []
                metas = res.get("metadatas") or []
                out.append(
                    {
                        "id": did,
                        "text": docs[i] if i < len(docs) else "",
                        "metadata": metas[i] if i < len(metas) else {},
                    }
                )
            return out
        if ids:
            return [self._docs[str(x)] for x in ids if str(x) in self._docs]
        return list(self._docs.values())

    # ------------------------------------------------------------------ #
    def persist(self, path: Optional[str] = None) -> bool:
        target = path or self.persist_path
        if target is None:
            return False
        if self._chroma:
            self.persist_path = target
            # PersistentClient ya persiste; asegurar directorio.
            try:
                os.makedirs(target, exist_ok=True)
            except Exception:
                pass
            return True
        # fallback: dump JSON
        try:
            if target.endswith(".json"):
                out_path = target
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            else:
                os.makedirs(target, exist_ok=True)
                out_path = os.path.join(target, "store.json")
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(
                    list(self._docs.values()), fh, ensure_ascii=False, indent=2, default=str
                )
            self.persist_path = target
            return True
        except Exception:
            return False

    def load(self, path: Optional[str] = None) -> bool:
        target = path or self.persist_path
        if target is None:
            return False
        if self._chroma:
            try:
                if target and os.path.isdir(target):
                    self._client = chromadb.PersistentClient(path=target)
                else:
                    self._client = chromadb.PersistentClient(path=target)
                self._collection = self._client.get_or_create_collection(
                    name=self.collection_name
                )
                self._chroma = True
                self.persist_path = target
                return True
            except Exception:
                self._chroma = False
                return False
        # fallback
        if target.endswith(".json"):
            in_path = target
        else:
            in_path = os.path.join(target, "store.json")
        if not os.path.isfile(in_path):
            return False
        try:
            with open(in_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._docs.clear()
            for rec in data:
                did = rec.get("id")
                if did is None:
                    did, _, _ = _normalize_doc(rec)
                self._docs[str(did)] = {
                    "id": str(did),
                    "text": rec.get("text", ""),
                    "metadata": rec.get("metadata", {}),
                }
            self.persist_path = target
            return True
        except Exception:
            return False
