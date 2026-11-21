"""Tests del laboratorio RAG (Fase 03)."""

import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_PATH = REPO_ROOT / "data" / "kb.json"

from lab.rag import (
    VectorStore,
    add_metadata,
    version_of,
    content_hash,
    chunk_text,
    load_kb,
    ingest_kb,
    update_document,
    remove_document,
    build_index,
    rebuild,
)


class TestVectorStoreMock(unittest.TestCase):
    def setUp(self):
        self.store = VectorStore(collection_name="test", persist_path=None)

    def test_is_available_false_or_true(self):
        # En entorno sin chromadb, fallback.
        self.assertIsInstance(self.store.is_available(), bool)

    def test_add_and_query(self):
        n = self.store.add_documents(
            [
                {"id": "d1", "text": "password reset locked account", "metadata": {"cat": "pwd"}},
                {"id": "d2", "text": "vpn connection failure", "metadata": {"cat": "vpn"}},
            ]
        )
        self.assertEqual(n, 2)
        self.assertEqual(self.store.count(), 2)
        res = self.store.query("locked password", k=1)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["id"], "d1")

    def test_filters(self):
        self.store.add_documents(
            [
                {"id": "a", "text": "alpha beta", "metadata": {"tag": "x"}},
                {"id": "b", "text": "alpha gamma", "metadata": {"tag": "y"}},
            ]
        )
        res = self.store.query("alpha", k=5, filters={"tag": "y"})
        self.assertEqual([r["id"] for r in res], ["b"])

    def test_delete(self):
        self.store.add_documents([{"id": "x", "text": "hello world"}])
        self.assertEqual(self.store.delete("x"), 1)
        self.assertEqual(self.store.count(), 0)

    def test_clear(self):
        self.store.add_documents([{"id": "x", "text": "hello world"}])
        self.store.clear()
        self.assertEqual(self.store.count(), 0)

    def test_persist_and_load(self):
        self.store.add_documents([{"id": "p", "text": "persist me"}])
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "store")
            self.assertTrue(self.store.persist(path))
            store2 = VectorStore(collection_name="test", persist_path=path)
            ok = store2.load(path)
            self.assertTrue(ok)
            self.assertEqual(store2.count(), 1)
            self.assertEqual(store2.get(["p"])[0]["text"], "persist me")


class TestMetadata(unittest.TestCase):
    def test_add_metadata_fields(self):
        doc = {"id": "K1", "title": "t", "resolution": "r"}
        out = add_metadata(doc, version="1.0", source="kb.json")
        for field in ("doc_id", "version", "source", "ingested_at", "content_hash"):
            self.assertIn(field, out)
        self.assertEqual(out["version"], "1.0")
        self.assertEqual(out["source"], "kb.json")
        self.assertEqual(len(out["content_hash"]), 64)
        self.assertEqual(version_of(out), "1.0")
        # original no mutado
        self.assertNotIn("content_hash", doc)

    def test_content_hash_deterministic(self):
        self.assertEqual(content_hash({"a": 1}), content_hash({"a": 1}))


class TestChunking(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(chunk_text(""), [])

    def test_chunks_created(self):
        text = "Primera oracion. Segunda oracion. " * 40
        chunks = chunk_text(text, chunk_size=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertIn("text", ch)
            self.assertIn("metadata", ch)
            self.assertLessEqual(len(ch["text"]), 120)

    def test_single_chunk(self):
        chunks = chunk_text("hola mundo", chunk_size=500, overlap=50)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["text"], "hola mundo")


class TestIngestion(unittest.TestCase):
    def test_load_kb(self):
        docs = load_kb(str(KB_PATH))
        self.assertEqual(len(docs), 13)
        for d in docs:
            self.assertIn("doc_id", d)
            self.assertIn("text", d)
            self.assertTrue(d["text"])

    def test_ingest_kb(self):
        store = VectorStore(collection_name="kbtest", persist_path=None)
        n = ingest_kb(store, str(KB_PATH))
        self.assertEqual(n, 13)
        self.assertEqual(store.count(), 13)

    def test_update_and_remove(self):
        store = VectorStore(collection_name="kbtest2", persist_path=None)
        ingest_kb(store, str(KB_PATH))
        update_document(store, "KB001", {"id": "KB001", "title": "nuevo", "resolution": "nueva res"})
        # still present
        self.assertEqual(store.count(), 13)
        remove_document(store, "KB001")
        self.assertEqual(store.count(), 12)


class TestIndex(unittest.TestCase):
    def test_build_index_deterministic(self):
        store = VectorStore(collection_name="idx", persist_path=None)
        n = build_index(store, str(KB_PATH))
        self.assertEqual(n, 13)
        ids = [d["id"] for d in store.get()]
        self.assertEqual(ids, sorted(ids))

    def test_rebuild(self):
        store = VectorStore(collection_name="idx2", persist_path=None)
        build_index(store, str(KB_PATH))
        # add duplicate-ish doc
        store.add_documents([{"id": "ZZ", "text": "extra"}])
        self.assertEqual(store.count(), 14)
        rebuild(store, str(KB_PATH))
        self.assertEqual(store.count(), 13)


if __name__ == "__main__":
    unittest.main()
