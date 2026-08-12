"""B2 试卷 OCR：扫图版 PDF/图片 → 文本（paddleocr 可选，零第三方主线）。

降级路径（依赖缺失时明确提示，不静默降质量）：
- 文本层 PDF：pdfminer.six（可选）→ 直接提取，无 OCR 参与；
- 扫描版 PDF：pypdfium2（可选，渲染）+ paddleocr（可选）逐页识别；
- 依赖缺失 → 返回可读错误，指引走 LLM 视觉提取（/api/ai/extract-photo）或手动录入。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from config import LOG

_ocr_lock = threading.Lock()
_ocr_instance: Any = None


def probe() -> dict[str, bool]:
    """能力探测：paddleocr / pdfminer / pypdfium2 是否可用。"""
    out = {"paddleocr": False, "pdfminer": False, "renderer": False}
    for mod, key in (("paddleocr", "paddleocr"), ("pdfminer.high_level", "pdfminer"),
                     ("pypdfium2", "renderer")):
        try:
            __import__(mod)
            out[key] = True
        except ImportError:
            pass
    return out


def _paddle() -> Any:
    global _ocr_instance
    with _ocr_lock:
        if _ocr_instance is None:
            from paddleocr import PaddleOCR  # type: ignore
            _ocr_instance = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        return _ocr_instance


def _ocr_image_pil(img: Any, page_label: str) -> tuple[str, float]:
    """对 PIL 图像执行 OCR。返回 (文本, 平均置信度)。"""
    import numpy as np  # type: ignore
    result = _paddle().ocr(np.array(img.convert("RGB")), cls=True)
    lines: list[tuple[str, float]] = []
    for res in result or []:
        for item in res or []:
            if len(item) >= 2 and item[1] and isinstance(item[1], (list, tuple)) and len(item[1]) >= 2:
                text, conf = str(item[1][0]), float(item[1][1] or 0)
                if text.strip():
                    lines.append((text.strip(), conf))
    if not lines:
        LOG.info("OCR 未识别到文字: %s", page_label)
        return "", 0.0
    text = "\n".join(t for t, _ in lines)
    avg = round(sum(c for _, c in lines) / len(lines), 3)
    return text, avg


def extract_image(path: str | Path) -> dict[str, Any]:
    """图片 OCR（paddleocr 可选；缺失时 ValueError 提示降级路径）。"""
    fp = Path(path)
    if not fp.is_file():
        raise ValueError(f"文件不存在: {path}")
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        try:
            from paddleocr import PaddleOCR  # noqa: F401 — 触发明确错误
        except ImportError:
            pass
        raise ValueError("图片 OCR 需要 paddleocr + Pillow；缺失时可用 LLM 视觉提取（错题-拍照/看图作答）或手动录入")
    text, conf = _ocr_image_pil(Image.open(fp), fp.name)
    return {"text": text, "confidence": conf, "engine": "paddleocr"}


def _extract_text_layer(fp: Path) -> str | None:
    """pdfminer 文本层（可选）。无依赖或空白 → None。"""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
    except ImportError:
        return None
    out = (extract_text(str(fp)) or "").strip()
    return out or None


def extract_pdf(path: str | Path) -> dict[str, Any]:
    """PDF → 逐页文本。文本层优先；扫描版降级 OCR（依赖全缺则 ValueError）。"""
    fp = Path(path)
    if not fp.is_file():
        raise ValueError(f"文件不存在: {path}")
    text_layer = _extract_text_layer(fp)
    if text_layer:
        paras = [p.strip() for p in text_layer.split("\n\n") if p.strip()]
        return {"pages": [{"page": 1, "text": t, "confidence": 1.0} for t in paras],
                "engine": "text-layer", "source": "pdfminer(six)"}
    try:
        import pypdfium2 as pdfium  # type: ignore
    except ImportError:
        raise ValueError(
            "扫描版 PDF 需要 pypdfium2（渲染）+ paddleocr（识别）；均可选依赖，"
            "缺失时可先转图片走 LLM 视觉提取，或手动录入"
        )
    probe_state = probe()
    if not probe_state["paddleocr"]:
        raise ValueError(
            "扫描版 PDF 需要 paddleocr 识别文字；缺失时可先转图片走 LLM 视觉提取（错题-看图作答），或手动录入"
        )
    pdf = pdfium.PdfDocument(str(fp))
    pages: list[dict[str, Any]] = []
    try:
        n = len(pdf)
        for i in range(n):
            page = pdf[i]
            image = page.render(scale=2.0).to_pil()
            text, conf = _ocr_image_pil(image, f"{fp.name} 第{i + 1}页")
            pages.append({"page": i + 1, "text": text, "confidence": conf})
    finally:
        pdf.close()
    if not any(p["text"] for p in pages):
        raise ValueError("OCR 未从试卷中识别到文字（可能页码方向/清晰度问题）")
    return {"pages": pages, "engine": "paddleocr", "source": "pypdfium2+ocr"}