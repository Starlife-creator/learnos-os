"""B3 个人资料 RAG 测试：摄取、BM25 检索、溯源、越界拒绝、hint 联动。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
from handler import Handler
import rag
import ai
from unittest.mock import patch

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestRag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="rag_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "rag_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        cls.materials = Path(cls.temp_dir.name) / "materials"
        cls.materials.mkdir()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "LearnOS"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def request_error(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "LearnOS"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=8)
        return ctx.exception.code, json.loads(ctx.exception.read().decode("utf-8"))

    def _write_note(self, name, text):
        fp = self.materials / name
        fp.write_text(text, encoding="utf-8")
        return fp

    def test_ingest_and_bm25_search(self):
        fp = self._write_note("力学笔记.md", (
            "# 牛顿定律\n牛顿第二定律：力是改变物体运动状态的原因。\n"
            "适用条件：惯性参考系，宏观低速，质量恒定。\n\n"
            "# 法拉第电磁感应\n磁通量变化产生感应电动势，方向由楞次定律判断。\n"
            "E = -dΦ/dt。"
        ))
        status, r = self.request("/api/rag/ingest", "POST", {"path": str(fp)})
        self.assertEqual(status, 200)
        self.assertGreater(r["chunks"], 0)
        # 检索命中电磁感应片段（BM25）
        status, r = self.request("/api/rag/search?q=" + quote("法拉第 感应电动势"))
        self.assertEqual(status, 200)
        self.assertTrue(r["items"])
        top = r["items"][0]
        self.assertIn("法拉第", top["content"])
        self.assertEqual(top["name"], "力学笔记.md")
        self.assertIn("source_path", top)
        # 检索命中牛顿片段
        status, r = self.request("/api/rag/search?q=" + quote("惯性参考系"))
        self.assertIn("牛顿", r["items"][0]["content"])

    def test_ingest_gbk_fallback(self):
        fp = self._write_note("gbk笔记.txt", "动量守恒定律：系统合外力为零时动量守恒。")
        fp.write_bytes(fp.read_bytes().decode("utf-8").encode("gbk"))
        status, r = self.request("/api/rag/ingest", "POST", {"path": str(fp)})
        self.assertEqual(status, 200)
        status, r = self.request("/api/rag/search?q=" + quote("动量守恒"))
        self.assertTrue(r["items"])

    def test_outside_workspace_rejected(self):
        # 跨平台：工作区父目录一定是绝对路径且在工作区外
        outside = str(Path(config.APP_DIR).resolve().parent / "outside_test_dir")
        status, r = self.request_error("/api/rag/ingest", "POST", {"path": outside})
        self.assertEqual(status, 400)
        self.assertIn("工作区", r["error"])

    def test_ingest_missing_path(self):
        status, r = self.request_error("/api/rag/ingest", "POST", {"path": "materials/不存在.md"})
        self.assertEqual(status, 400)

    def test_rag_open_requires_registered_path(self):
        status, r = self.request_error("/api/rag/open?path=" + quote("materials/未登记.md"))
        self.assertEqual(status, 400)

    def test_docs_list_and_delete(self):
        self._write_note("临时文档.md", "热力学第一定律：内能变化等于热量与做功之和。")
        self.request("/api/rag/ingest", "POST", {"path": str(self.materials)})
        status, r = self.request("/api/rag/docs")
        self.assertTrue(any("临时文档" in d["source_path"] for d in r["items"]))
        target = next(d for d in r["items"] if "临时文档" in d["source_path"])
        status, r = self.request(f"/api/rag/doc/{target['id']}", "DELETE")
        self.assertEqual(status, 200)
        status, r = self.request("/api/rag/docs")
        self.assertFalse(any(d["id"] == target["id"] for d in r["items"]))

    def test_rag_context_injection(self):
        self._write_note("光学笔记.md", "光的干涉：频率相同的光在空间叠加形成明暗条纹。")
        self.request("/api/rag/ingest", "POST", {"path": str(self.materials)})
        problem = {
            "id": 0, "topic": "光的干涉", "title": "双缝干涉", "content": "双缝干涉条纹间距？",
        }
        msgs, sources = Handler._rag_context(Handler.__new__(Handler), problem)
        self.assertTrue(sources)
        self.assertEqual(sources[0]["name"], "光学笔记.md")
        self.assertIn("光学笔记", msgs[0]["content"])

    def test_filter_noise_dual_channel(self):
        """X1 低质命中双通道过滤：垃圾（低分+低覆盖）剔除；高分或高覆盖任一达标即保留。

        分数通道兜词法量纲（实测垃圾 ≈1.0-1.6，真命中 ≥13）；
        覆盖率通道兜向量量纲（RRF 融合分为小数量纲）。
        """
        query = "滑铁卢战役发生在哪一年"
        noise = {"score": 1.6, "content": "牛顿第二定律：力是改变物体运动状态的原因。"}
        real_by_score = {"score": 13.0, "content": "内容无关但词法分数达标"}
        real_by_coverage = {"score": 0.01, "content": "滑铁卢战役发生在1815年，拿破仑战败。"}
        out = rag.filter_noise([noise, real_by_score, real_by_coverage], query)
        self.assertNotIn(noise, out, "低分+低覆盖的沾边命中必须剔除")
        self.assertIn(real_by_score, out, "高分通道应保留")
        self.assertIn(real_by_coverage, out, "覆盖率通道应保留（向量量纲兜底）")
        self.assertEqual(rag.filter_noise([], query), [])

    def test_rag_context_zero_hit_boundary(self):
        """X1 越界抑制：资料库非空但零命中 → 注入「资料未覆盖」硬边界消息（探针用例）。

        探针问题取教材未讲内容（滑铁卢战役），语料仅有物理笔记 → 检索必然零命中，
        AI 提示必须携带「不得声称根据教材 + 教材外补充须标注」约束，防编造。
        """
        fp = self._write_note("边界探针.md", "狭义相对论：光速不变原理与相对性原理。")
        rag.ingest_path(str(fp))
        problem = {
            "id": 0, "topic": "法国大革命", "title": "滑铁卢战役",
            "content": "拿破仑滑铁卢战役发生在哪一年？（资料库未收录）",
        }
        msgs, sources = Handler._rag_context(Handler.__new__(Handler), problem)
        self.assertEqual(sources, [], "零命中不得携带溯源标签")
        self.assertTrue(msgs, "资料库非空时零命中应注入边界消息")
        self.assertIn("资料", msgs[0]["content"])
        self.assertIn("不得声称", msgs[0]["content"])
        self.assertIn("AI 补充", msgs[0]["content"])

    def test_rag_search_probe_out_of_corpus(self):
        """X1 探针基准：教材未讲内容检索零命中（弃答率指标的正向探针）。"""
        fp = self._write_note("探针语料.md", "热力学第二定律：孤立系统熵不减。")
        rag.ingest_path(str(fp))
        self.assertFalse(rag.search("滑铁卢战役 拿破仑", k=3), "语料外探针词必须零命中")
        self.assertTrue(rag.search("热力学 熵", k=3), "语料内词仍正常命中（防误伤）")

    def test_direct_bm25(self):
        self._write_note("纯函数测试.md", "量子力学波函数描述粒子概率分布。")
        rag.ingest_path(str(self.materials))
        hits = rag.search("波函数 概率", k=3)
        self.assertTrue(hits)
        self.assertIn("波函数", hits[0]["content"])

    def test_bm25_cache_invalidation(self):
        """C6：摄取/删除后 BM25 缓存失效，检索结果即时更新。"""
        rag._BM25_CACHE["docs"] = None
        fp1 = self._write_note("缓存A.md", "基尔霍夫第一定律：节点电流代数和为零。")
        rag.ingest_path(str(fp1))
        self.assertTrue(rag.search("基尔霍夫", k=2))
        fp2 = self._write_note("缓存B.md", "衍射光栅方程：dsinθ = mλ。")
        rag.ingest_path(str(fp2))
        hits = rag.search("衍射光栅", k=2)
        self.assertTrue(any("衍射光栅" in h["content"] for h in hits))
        doc = rag.list_docs()
        target = next(d for d in doc if "缓存A" in d["source_path"])
        rag.delete_doc(target["id"])
        self.assertFalse(any("基尔霍夫" in h["content"] for h in rag.search("基尔霍夫", k=5)))


class TestRagEmbeddings(unittest.TestCase):
    """M8 向量检索：可选启用、RRF 融合、降级与清理（零第三方依赖）。"""
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="ragemb_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "emb_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
        cls.materials = Path(cls.temp_dir.name) / "materials"
        cls.materials.mkdir()

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        cls.temp_dir.cleanup()

    def _write_note(self, name, text):
        fp = self.materials / name
        fp.write_text(text, encoding="utf-8")
        return fp

    def _fake_settings(self, model="test-embed"):
        return {
            "embedding_model": model, "model": "m",
            "api_base": "https://x.test/v1", "api_key": "k",
        }

    def _fake_embed(self, texts, model=None):
        # 含"法拉第"→[1,0,0]，其余→[0,1,0]；查询含"法拉第"则与法拉第 chunk 余弦=1
        return [[1.0, 0.0, 0.0] if "法拉第" in t else [0.0, 1.0, 0.0] for t in texts]

    def test_migration_table_created(self):
        r = db.row("SELECT name FROM sqlite_master WHERE type='table' AND name='rag_embeddings'")
        self.assertIsNotNone(r)

    def test_disabled_by_default(self):
        self.assertFalse(rag.embedding_enabled())

    def test_search_fallback_no_embed_call(self):
        # 未启用时 search 纯词法，绝不触发 ai.embed
        self._write_note("fallback.md", "量子力学波函数描述粒子概率分布。")
        rag.ingest_path(str(self.materials))
        with patch.object(ai, "embed", side_effect=AssertionError("不应调用 embed")):
            hits = rag.search("波函数 概率", k=3)
        self.assertTrue(hits)
        self.assertIn("波函数", hits[0]["content"])

    def test_ingest_stores_embeddings_and_vector_search(self):
        self._write_note("faraday.md", "# 法拉第电磁感应\n磁通量变化产生感应电动势。\n")
        self._write_note("optics.md", "# 光的干涉\n频率相同的光叠加形成明暗条纹。\n")
        with patch.object(ai, "get_cached_settings", return_value=self._fake_settings()):
            with patch.object(ai, "embed", side_effect=self._fake_embed):
                rag.ingest_path(str(self.materials))
                n = db.row("SELECT COUNT(*) AS n FROM rag_embeddings")["n"]
                self.assertGreater(n, 0)
                hits = rag._vector_search("法拉第相关查询", k=3)
                self.assertTrue(hits)
                self.assertIn("法拉第", hits[0]["content"])

    def test_hybrid_search_runs(self):
        self._write_note("hybrid.md", "# 楞次定律\n感应电流方向总是阻碍磁通量变化。\n")
        with patch.object(ai, "get_cached_settings", return_value=self._fake_settings()):
            with patch.object(ai, "embed", side_effect=self._fake_embed):
                rag.ingest_path(str(self.materials))
                hits = rag.search("法拉第 感应", k=3)
        self.assertTrue(hits)

    def test_delete_removes_embeddings(self):
        self._write_note("del.md", "安培力：通电导线在磁场中受的力 F = BIL。\n")
        with patch.object(ai, "get_cached_settings", return_value=self._fake_settings()):
            with patch.object(ai, "embed", side_effect=self._fake_embed):
                rag.ingest_path(str(self.materials))
                doc = rag.list_docs()
                target = next(d for d in doc if "del.md" in d["source_path"])
                before = db.row("SELECT COUNT(*) AS n FROM rag_embeddings")["n"]
                self.assertGreaterEqual(before, 1)
                rag.delete_doc(target["id"])
                after = db.row("SELECT COUNT(*) AS n FROM rag_embeddings")["n"]
                self.assertLess(after, before)
                self.assertTrue(rag.restore_doc(target["id"]))
                restored = db.row("SELECT COUNT(*) AS n FROM rag_embeddings")["n"]
                self.assertGreaterEqual(restored, 1)


if __name__ == "__main__":
    unittest.main()
