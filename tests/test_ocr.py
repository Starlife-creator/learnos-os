"""B2 试卷 OCR 测试：能力探测、图片/PDF 提取、降级路径、路径边界。"""
import json
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config, db, ocr
from handler import Handler

_TMP = Path(__file__).resolve().parent / ".tmp"
_TMP.mkdir(exist_ok=True)


class _FakeModule(types.ModuleType):
    pass


def _install_fake(name: str, **attrs):
    mod = _FakeModule(name)
    mod.__path__ = []  # 让 from pkg.sub import x 能走包属性
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    for sub, obj in attrs.items():
        if isinstance(obj, _FakeModule):
            sys.modules[f"{name}.{sub}"] = obj
    return mod


class TestOcr(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="ocr_", dir=_TMP)
        cls._orig_db = config.DB_PATH
        config.DB_PATH = Path(cls.temp_dir.name) / "ocr_test.db"
        db.DB_PATH = config.DB_PATH
        db.init_db()
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
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "PhysicsStudyOS"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def request_error(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "PhysicsStudyOS"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_probe_structure(self):
        status, r = self.request("/api/ocr/probe")
        self.assertEqual(status, 200)
        for k in ("paddleocr", "pdfminer", "renderer"):
            self.assertIn(k, r)
            self.assertIsInstance(r[k], bool)

    def test_extract_bad_path(self):
        code, r = self.request_error("/api/ocr/extract", "POST", {"path": ""})
        self.assertEqual(code, 400)
        code, r = self.request_error("/api/ocr/extract", "POST", {"path": "nope.png"})
        self.assertEqual(code, 400)
        code, r = self.request_error("/api/ocr/extract", "POST", {"path": "../outside.png"})
        self.assertEqual(code, 400)

    def test_pdf_text_layer(self):
        # 注入假 pdfminer：文本层 PDF → 无需 paddleocr
        fake = _install_fake("pdfminer", high_level=_FakeModule("pdfminer.high_level"))
        fake.high_level.extract_text = lambda path: "第一题：牛顿第二定律应用\n\n第二题：动量守恒"
        pdf = Path(self.temp_dir.name) / "text.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        r = ocr.extract_pdf(pdf)
        self.assertEqual(r["engine"], "text-layer")
        self.assertEqual(len(r["pages"]), 2)
        self.assertIn("牛顿第二定律", r["pages"][0]["text"])
        self.assertTrue(all(p["confidence"] == 1.0 for p in r["pages"]))

    def test_scanned_pdf_without_deps(self):
        # 无文本层 + 渲染器缺失 → ValueError 带降级指引
        for m in ("pdfminer", "pdfminer.high_level", "pypdfium2", "paddleocr", "PIL"):
            sys.modules.pop(m, None)
        pdf = Path(self.temp_dir.name) / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4 fakess")
        with self.assertRaises(ValueError) as ctx:
            ocr.extract_pdf(pdf)
        self.assertIn("pypdfium2", str(ctx.exception))

    def test_image_ocr_happy_path(self):
        # 注入假 PIL + paddleocr + pypdfium2 + numpy：覆盖扫描 PDF 渲染→OCR
        fake_pil = _FakeModule("PIL")
        fake_pil.Image = _FakeModule("PIL.Image")

        class FakeImage:
            def convert(self, mode):
                return self

        fake_pil.Image.open = lambda p: FakeImage()
        sys.modules["PIL"] = fake_pil

        # 渲染/页面/文档用普通类（ModuleType 的 __len__ 实例属性不生效）
        class Rendered:
            def to_pil(self):
                return fake_pil.Image.open(None)

        class Page:
            def render(self, scale=1.0):
                return Rendered()

        class Doc:
            def __len__(self):
                return 1

            def __getitem__(self, i):
                return Page()

            def close(self):
                return None

        fake_pdfium = _FakeModule("pypdfium2")
        fake_pdfium.PdfDocument = lambda p: Doc()
        sys.modules["pypdfium2"] = fake_pdfium

        fake_paddle = _FakeModule("paddleocr")
        fake_paddle.PaddleOCR = lambda **kw: _FakeModule("ocr")
        fake_paddle.__path__ = []
        sys.modules["paddleocr"] = fake_paddle

        np = _FakeModule("numpy")
        np_arr = [[0] * 3] * 4
        np.array = lambda img, dtype=None: np_arr
        np.zeros = lambda *a, **k: np_arr
        np.testing = _FakeModule("numpy.testing")
        sys.modules["numpy"] = np

        captured = {}

        def fake_ocr(self, img, cls=True):
            captured["img"] = img
            return [[(None, ("牛顿第二定律", 0.92)), (None, ("第2页内容", 0.88))]]
        sys.modules["paddleocr"].PaddleOCR().__class__.ocr = fake_ocr

        try:
            pdf = Path(self.temp_dir.name) / "scan2.pdf"
            pdf.write_bytes(b"%PDF-1.4 fakess")
            r = ocr.extract_pdf(pdf)
            self.assertEqual(r["engine"], "paddleocr")
            self.assertEqual(len(r["pages"]), 1)
            self.assertIn("牛顿第二定律", r["pages"][0]["text"])
            self.assertAlmostEqual(r["pages"][0]["confidence"], 0.9, places=2)
            self.assertIs(captured["img"], np_arr)
        finally:
            for m in ("PIL", "paddleocr", "pypdfium2", "numpy"):
                sys.modules.pop(m, None)


if __name__ == "__main__":
    unittest.main()