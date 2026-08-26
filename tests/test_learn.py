"""学习台 P0 测试：materials CRUD / HTML 消毒 / md 渲染 / 全文搜索 / AI 离线降级。"""
import sys
import tempfile
import unittest
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import db
import learn
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class TestLearnSanitize(unittest.TestCase):
    """纯单元：消毒器与 md 渲染（不启服务）。"""

    def test_sanitize_strips_script_and_events(self):
        dirty = ('<p onclick="steal()">ok</p>'
                 '<script>alert(1)</script><p>after</p>'
                 '<a href="javascript:x()">bad</a>'
                 '<img src="data:text/html;base64,x">'
                 '<iframe src="http://evil"></iframe>')
        clean = learn.sanitize_html(dirty)
        self.assertNotIn("<script", clean.lower())
        self.assertNotIn("alert(1)", clean)
        self.assertNotIn("<iframe", clean.lower())
        self.assertNotIn("onclick", clean.lower())
        self.assertNotIn("javascript:", clean.lower())
        self.assertNotIn("data:text/html", clean.lower())
        self.assertIn("ok", clean)
        self.assertIn("after", clean)

    def test_sanitize_keeps_safe_content(self):
        src = '<h1>标题</h1><p>段落 <strong>加粗</strong></p>' \
              '<a href="https://example.com/x">外链</a><img src="/media/a.png" alt="图">'
        clean = learn.sanitize_html(src)
        self.assertIn("<h1>", clean)
        self.assertIn("<strong>", clean)
        self.assertIn('rel="noopener nofollow"', clean)
        self.assertIn('src="/media/a.png"', clean)

    def test_sanitize_balances_unclosed_tags(self):
        clean = learn.sanitize_html("<div><b>未闭合")
        self.assertTrue(clean.endswith("</b></div>"))

    def test_md_to_html_basics(self):
        html = learn.md_to_html("# 标题\n\n正文 **粗** 和 *斜* 与 `code`\n\n- 甲\n- 乙\n")
        self.assertIn("<h1>标题</h1>", html)
        self.assertIn("<strong>粗</strong>", html)
        self.assertIn("<em>斜</em>", html)
        self.assertIn("<code>code</code>", html)
        self.assertIn("<ul>", html)
        self.assertIn("<li>甲</li>", html)

    def test_md_to_html_code_fence_and_links(self):
        html = learn.md_to_html("```py\nx = 1 < 2\n```\n[点我](javascript:alert(1)) [好链](https://a.b/c)\n")
        self.assertIn("<pre><code>", html)
        self.assertIn("1 &lt; 2", html)
        self.assertNotIn("href=\"javascript:", html)
        self.assertIn('href="https://a.b/c"', html)


class TestLearnApi(unittest.TestCase):
    server: ThreadingHTTPServer
    thread: Thread

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="learn_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        cls._orig_app = config.APP_DIR
        config.DB_PATH = Path(cls._tmp.name) / "learn_test.db"
        config.APP_DIR = Path(cls._tmp.name)          # 上传/自编教材落盘进临时目录
        db.DB_PATH = config.DB_PATH
        db.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()
        db.DB_PATH = cls._orig_db
        config.DB_PATH = cls._orig_db
        config.APP_DIR = cls._orig_app

    def _request(self, method, path, body=None, raw=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json_dumps(body) if body is not None else raw
        headers = {"X-Requested-With": "LearnOS"}
        if body is not None or raw is None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            return resp.status, json_loads(payload)
        except Exception:
            return resp.status, payload

    # ── 自编教材全流程 ──

    def test_authored_material_full_flow(self):
        # 新建（authored 落盘 textbooks/）
        status, data = self._request("POST", "/api/learn/materials",
                                     {"title": "力学讲义", "subject": "physics",
                                      "content": "# 牛顿定律\n\n$F=ma$ 是核心。\n\n## 惯性\n\n一切物体保持…"})
        self.assertEqual(status, 201, data)
        mid = data["id"]
        self.assertTrue(data["path"].startswith(("textbooks", r"textbooks")))
        # 列表可见
        status, data = self._request("GET", "/api/learn/materials?subject=physics")
        titles = [m["title"] for m in data["items"]]
        self.assertIn("力学讲义", titles)
        # 内容可读且已渲染为安全 HTML
        status, data = self._request("GET", f"/api/learn/materials/{mid}/content")
        self.assertEqual(status, 200)
        self.assertEqual(data["fmt"], "md")
        self.assertIn("<h1>牛顿定律</h1>", data["content"])
        self.assertIn("<h2>惯性</h2>", data["content"])
        # 改名
        status, data = self._request("POST", f"/api/learn/materials/{mid}/update",
                                     {"title": "力学讲义 v2"})
        self.assertEqual(status, 200)
        # 全文搜索命中
        from urllib.parse import quote
        status, data = self._request("GET", "/api/learn/search?q=" + quote("惯性") + "&subject=physics")
        self.assertEqual(status, 200)
        self.assertTrue(any(h["material_id"] == mid for h in data["items"]))
        # 删除登记（磁盘文件保留，不校验）
        status, data = self._request("DELETE", f"/api/learn/materials/{mid}")
        self.assertEqual(status, 200)
        status, data = self._request("GET", f"/api/learn/materials/{mid}/content")
        self.assertEqual(status, 404)

    def test_add_rejects_outside_path_and_bad_fmt(self):
        status, data = self._request("POST", "/api/learn/materials",
                                     {"path": "../outside.md", "subject": "physics"})
        self.assertEqual(status, 400)
        status, data = self._request("POST", "/api/learn/materials",
                                     {"path": "uploads/nothing.docx", "subject": "physics"})
        self.assertEqual(status, 400)

    def test_upload_then_register_and_read(self):
        body = "# 上传的教材\n\n内容 A。".encode("utf-8")
        status, data = self._request(
            "POST", "/api/material/upload?name=test_learn_up.md", raw=body)
        self.assertEqual(status, 201, data)
        path = data["path"]
        status, data = self._request("POST", "/api/learn/materials",
                                     {"path": path, "subject": "physics"})
        self.assertEqual(status, 201, data)
        mid = data["id"]
        status, data = self._request("GET", f"/api/learn/materials/{mid}/content")
        self.assertEqual(status, 200)
        self.assertIn("上传的教材", data["content"])

    def test_duplicate_registration_is_idempotent(self):
        body = "# 重复登记测试".encode("utf-8")
        _, up = self._request("POST", "/api/material/upload?name=dup.md", raw=body)
        _, r1 = self._request("POST", "/api/learn/materials", {"path": up["path"]})
        _, r2 = self._request("POST", "/api/learn/materials", {"path": up["path"]})
        self.assertEqual(r1["id"], r2["id"])

    def test_search_too_short_returns_empty(self):
        status, data = self._request("GET", "/api/learn/search?q=a&subject=physics")
        self.assertEqual(status, 200)
        self.assertEqual(data["items"], [])

    def test_learn_ask_offline_friendly_error(self):
        import ai
        with mock.patch.object(ai, "ai_configured", return_value=False):
            status, data = self._request("POST", "/api/learn/ask",
                                         {"question": "什么是惯性？"})
        self.assertEqual(status, 502)
        self.assertTrue(data.get("offline"))

    def test_learn_ask_requires_question(self):
        status, data = self._request("POST", "/api/learn/ask", {"question": ""})
        self.assertEqual(status, 400)

    # ── P0.5 批注 / 划词 ──

    def _make_material(self, title="批注测试教材"):
        status, data = self._request(
            "POST", "/api/learn/materials",
            {"title": title, "subject": "physics",
             "content": "# 章\n\n牛顿第一定律是惯性定律。\n\n力是改变运动状态的原因。"})
        self.assertEqual(status, 201, data)
        return data["id"]

    def test_annotation_crud_roundtrip(self):
        mid = self._make_material()
        anchor = {"prefix": "章 牛顿第一", "quote": "惯性定律", "suffix": "。 力是"}
        # 新增高亮
        status, data = self._request(
            "POST", f"/api/learn/materials/{mid}/annotations",
            {"kind": "highlight", "anchor": anchor})
        self.assertEqual(status, 201, data)
        aid = data["id"]
        # 新增带正文的旁注
        status, data = self._request(
            "POST", f"/api/learn/materials/{mid}/annotations",
            {"kind": "note", "anchor": anchor, "body": "考试重点"})
        self.assertEqual(status, 201)
        # 列表回读：锚点 JSON 结构保真
        status, data = self._request("GET", f"/api/learn/materials/{mid}/annotations")
        self.assertEqual(status, 200)
        items = data["items"]
        self.assertEqual(len(items), 2)
        by_kind = {it["kind"]: it for it in items}
        self.assertEqual(by_kind["highlight"]["anchor"]["quote"], "惯性定律")
        self.assertEqual(by_kind["highlight"]["anchor"]["prefix"], "章 牛顿第一")
        self.assertEqual(by_kind["note"]["body"], "考试重点")
        # 删除
        status, data = self._request("DELETE", f"/api/learn/annotations/{aid}")
        self.assertEqual(status, 200)
        status, data = self._request("GET", f"/api/learn/materials/{mid}/annotations")
        self.assertEqual(len(data["items"]), 1)
        # 再删 → 404
        status, data = self._request("DELETE", f"/api/learn/annotations/{aid}")
        self.assertEqual(status, 404)

    def test_annotation_validation(self):
        mid = self._make_material()
        # 非法 kind
        status, data = self._request(
            "POST", f"/api/learn/materials/{mid}/annotations",
            {"kind": "ink", "anchor": {"quote": "x"}})
        self.assertEqual(status, 400)
        # 缺 quote
        status, data = self._request(
            "POST", f"/api/learn/materials/{mid}/annotations",
            {"kind": "highlight", "anchor": {"prefix": "a"}})
        self.assertEqual(status, 400)
        # 不存在的教材
        status, data = self._request(
            "POST", "/api/learn/materials/999999/annotations",
            {"kind": "highlight", "anchor": {"quote": "x"}})
        self.assertEqual(status, 404)

    def test_apply_cards_endpoint(self):
        cards = [{"question": "用自己的话解释「惯性」", "answer": "物体保持原有运动状态的性质"},
                 {"question": "", "answer": "无问题的卡应被过滤"}]
        status, data = self._request("POST", "/api/learn/cards/apply",
                                     {"cards": cards, "subject": "physics"})
        self.assertEqual(status, 200)
        self.assertEqual(data["added"], 1)
        # 空列表拒绝
        status, data = self._request("POST", "/api/learn/cards/apply",
                                     {"cards": [], "subject": "physics"})
        self.assertEqual(status, 400)

    # ── P1 [[双链]] / AI 整章生成 ──

    def test_md_wikilink_matching(self):
        wiki = {"惯性": "concept_map.html?subject=physics&focus=%E6%83%AF%E6%80%A7"}
        html = learn.md_to_html("参见 [[惯性]] 与 [[不存在概念]]。", wiki=wiki)
        self.assertIn('class="wikilink" href="concept_map.html?subject=physics', html)
        self.assertIn(">惯性</a>", html)
        self.assertIn('class="wikilink missing"', html)
        self.assertIn("不存在概念", html)
        # 无 resolver 时原样保留字面量
        plain = learn.md_to_html("参见 [[惯性]]。")
        self.assertIn("[[惯性]]", plain)

    def test_wikilink_end_to_end_via_content(self):
        import graph
        graph.add_concept("动量守恒", 0, subject="physics")
        status, data = self._request(
            "POST", "/api/learn/materials",
            {"title": "双链教材", "subject": "physics",
             "content": "本节讨论 [[动量守恒]] 和 [[未建概念]]。\n\n- 列表 [[动量守恒]]"})
        self.assertEqual(status, 201)
        mid = data["id"]
        status, data = self._request("GET", f"/api/learn/materials/{mid}/content")
        self.assertEqual(status, 200)
        content = data["content"]
        self.assertIn("focus=%E5%8A%A8%E9%87%8F%E5%AE%88%E6%81%92", content)
        self.assertIn('class="wikilink missing"', content)

    def test_generate_offline_friendly_error(self):
        status, data = self._request(
            "POST", "/api/learn/generate",
            {"title": "力学", "outline": "- 惯性\n- 加速度", "subject": "physics"})
        self.assertEqual(status, 502)
        self.assertTrue(data.get("error"))

    def test_generate_requires_title_and_outline(self):
        import ai
        with mock.patch.object(ai, "ai_configured", return_value=True), \
             mock.patch.object(ai, "call_ai", return_value="# 章"):
            status, data = self._request("POST", "/api/learn/generate",
                                         {"title": "", "outline": ""})
        self.assertEqual(status, 400)

    # ── P2 今日回看 / 矢量图形批注 ──

    def test_review_today_excludes_shapes_oldest_first(self):
        mid = self._make_material(title="回看教材")
        from urllib.parse import quote
        # 依次建：highlight（最旧）→ note → shape（应被排除）
        _, a1 = self._request("POST", f"/api/learn/materials/{mid}/annotations",
                              {"kind": "highlight", "anchor": {"quote": "惯性定律"}})
        _, a2 = self._request("POST", f"/api/learn/materials/{mid}/annotations",
                              {"kind": "note", "anchor": {"quote": "力是改变"},
                               "body": "重点"})
        _, a3 = self._request("POST", f"/api/learn/materials/{mid}/annotations",
                              {"kind": "shape", "anchor": {"page": 1},
                               "body": '{"type":"arrow","x0":0.1,"y0":0.1,"x1":0.5,"y1":0.5,"color":"e11d48"}'})
        self.assertEqual(a3["id"] > a2["id"], True)
        status, data = self._request(
            "GET", "/api/learn/review-today?subject=" + quote("physics") + "&k=20")
        self.assertEqual(status, 200)
        items = [it for it in data["items"] if it["material_id"] == mid]
        self.assertEqual([it["kind"] for it in items], ["highlight", "note"])  # 最旧优先，无 shape
        self.assertEqual(items[0]["material_title"], "回看教材")
        self.assertIn(items[0]["material_fmt"], ("md", "txt", "html", "pdf"))

    def test_shape_annotation_roundtrip(self):
        import json as _json
        mid = self._make_material()
        geo = {"type": "pen", "points": [[0.1, 0.1], [0.2, 0.15], [0.3, 0.3]], "color": "4f7cff"}
        status, data = self._request(
            "POST", f"/api/learn/materials/{mid}/annotations",
            {"kind": "shape", "anchor": {"page": 2}, "body": _json.dumps(geo)})
        self.assertEqual(status, 201)
        status, data = self._request("GET", f"/api/learn/materials/{mid}/annotations")
        shape = [it for it in data["items"] if it["kind"] == "shape"]
        self.assertEqual(len(shape), 1)
        self.assertEqual(shape[0]["anchor"]["page"], 2)
        self.assertEqual(_json.loads(shape[0]["body"])["type"], "pen")


def json_dumps(obj):
    import json
    return json.dumps(obj)


def json_loads(payload):
    import json
    return json.loads(payload.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
