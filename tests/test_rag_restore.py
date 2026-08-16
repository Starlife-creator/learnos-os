"""验证 rag delete/restore 往返：删除后恢复，内容一致。"""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class RagRestoreTest(unittest.TestCase):
    server = None
    thread = None

    @classmethod
    def setUpClass(cls):
        os.environ["PHYSICS_DB"] = str(_TMP / "rag_restore_test.db")
        db.init_db()
        # 清空 rag 表，保持干净
        with db.DB_LOCK, db.db() as conn:
            conn.execute("DELETE FROM rag_docs")
            conn.execute("DELETE FROM rag_chunks")
            conn.execute("INSERT INTO rag_docs(source_path, file_type, pages, chunk_count, ingested_at) VALUES ('t/undo.md','md',0,2,'2026-01-01 00:00:00')")
            doc_id = int(conn.execute("SELECT id FROM rag_docs LIMIT 1").fetchone()[0])
            conn.executemany(
                "INSERT INTO rag_chunks(doc_id, chunk_index, page, content) VALUES (?, ?, ?, ?)",
                [(doc_id, 0, 0, "first chunk"), (doc_id, 1, 0, "second chunk")],
            )
            cls.doc_id = doc_id
        import rag
        rag._UNDO.clear()

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
        p = _TMP / "rag_restore_test.db"
        if p.exists():
            p.unlink()

    def _start(self):
        from handler import Handler as H
        RagRestoreTest.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        RagRestoreTest.thread = Thread(target=RagRestoreTest.server.serve_forever, daemon=True)
        RagRestoreTest.thread.start()
        return RagRestoreTest.server.server_address[1]

    def _post(self, port, path):
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json", "X-Requested-With": "LearnOS"}
        conn.request("POST", path, body="{}", headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_delete_then_restore(self):
        import rag
        port = self._start()
        # 初始：1 文档 2 chunks
        docs = rag.list_docs()
        self.assertEqual(len(docs), 1)
        # 删除
        ok = rag.delete_doc(self.doc_id)
        self.assertTrue(ok)
        self.assertEqual(len(rag.list_docs()), 0)
        # HTTP restore
        status, data = self._post(port, "/api/rag/doc/%d/restore" % self.doc_id)
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"))
        # 恢复后内容一致
        docs = rag.list_docs()
        self.assertEqual(len(docs), 1)
        with db.DB_LOCK, db.db() as c:
            chunks = c.execute("SELECT content FROM rag_chunks WHERE doc_id = ? ORDER BY chunk_index", (self.doc_id,)).fetchall()
        self.assertEqual([r[0] for r in chunks], ["first chunk", "second chunk"])
        # 二次恢复应失败（快照已消费）
        status, data = self._post(port, "/api/rag/doc/%d/restore" % self.doc_id)
        self.assertEqual(status, 400, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)