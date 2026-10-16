# -*- coding: utf-8 -*-
"""OCR 能力实测探针。

思路：把有文本层的 PDF 渲染成图片（模拟扫描件），跑本地 OCR，
再与 PDF 原生文本层做基准比对 —— 这样能真实量化：
  1. 字符级准确率（识别对不对）
  2. 阅读顺序正确性（双栏版面会不会串行）
  3. 速度

用法：python tools/ocr_probe.py
"""

from __future__ import annotations

import difflib
import re
import sys
import time
from pathlib import Path

import fitz  # pymupdf

OUT = Path(__file__).resolve().parent / "_ocr_out"


def norm(s: str) -> str:
    """去掉所有空白，便于字符级比对。"""
    return re.sub(r"\s+", "", s or "")


def sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def render_page(pdf: Path, page_no: int, dpi: int = 200) -> tuple[Path, str, list[str]]:
    doc = fitz.open(str(pdf))
    page = doc[page_no - 1]
    pix = page.get_pixmap(dpi=dpi)
    OUT.mkdir(parents=True, exist_ok=True)
    png = OUT / f"{pdf.stem}_p{page_no}_{dpi}dpi.png"
    pix.save(str(png))
    text = page.get_text("text")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    doc.close()
    return png, text, lines


def run_ocr(png: Path) -> tuple[list[str], float]:
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    t0 = time.time()
    result, _elapse = engine(str(png))
    dt = time.time() - t0
    lines = [(r[1] or "").strip() for r in (result or [])]
    return [l for l in lines if l], dt


def report(name: str, pdf: Path, page_no: int, dpi: int = 200) -> dict:
    print(f"\n{'='*76}\n{name}  |  {pdf.name} 第 {page_no} 页 @ {dpi} DPI\n{'='*76}")

    png, text, gt_lines = render_page(pdf, page_no, dpi)
    print(f"图片尺寸: {png.stat().st_size/1024:.0f} KB")

    ocr_lines, dt = run_ocr(png)

    gt = norm("\n".join(gt_lines))
    got = norm("\n".join(ocr_lines))

    # 字符级相似度（注意：OCR 输出顺序可能不同，这里反映的是"字符集合的接近程度"）
    s_all = sim(gt, got)
    # 前 15 行的逐行匹配率
    hits = 0
    for g in gt_lines[:15]:
        if any(sim(norm(g), norm(o)) > 0.8 for o in ocr_lines):
            hits += 1

    print(f"文本层行数: {len(gt_lines)}    OCR 行数: {len(ocr_lines)}")
    print(f"整页字符级相似度: {s_all:.4f}")
    print(f"前 15 行逐行命中率: {hits}/{min(15, len(gt_lines))} = {hits/max(1,min(15,len(gt_lines))):.0%}")
    print(f"耗时: {dt:.2f}s")

    print("\n--- 文本层前 8 行（基准，正确阅读顺序）---")
    for l in gt_lines[:8]:
        print(f"  | {l[:88]}")
    print("\n--- OCR 前 8 行（注意顺序）---")
    for l in ocr_lines[:8]:
        print(f"  | {l[:88]}")

    return {"name": name, "sim": s_all, "lines_gt": len(gt_lines),
            "lines_ocr": len(ocr_lines), "sec": dt}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    en_pdf = root / "sample_papers" / "1706.03762.pdf"
    zh_pdf = Path("E:/aicoding/司法鉴定AI辅助系统_技术实现方案.pdf")

    results = []
    if en_pdf.exists():
        results.append(report("英文论文 · 双栏 · 含公式", en_pdf, 1))
        results.append(report("英文论文 · 双栏 · 正文页", en_pdf, 3))
    else:
        print(f"跳过英文样本：{en_pdf} 不存在")

    if zh_pdf.exists():
        results.append(report("中文文档 · 单栏", zh_pdf, 1))
    else:
        print(f"跳过中文样本：{zh_pdf} 不存在")

    print(f"\n\n{'='*76}\n汇总\n{'='*76}")
    print(f"{'样本':<26}{'字符相似度':>12}{'基准行':>8}{'OCR行':>8}{'耗时(s)':>10}")
    for r in results:
        print(f"{r['name']:<26}{r['sim']:>12.4f}{r['lines_gt']:>8}{r['lines_ocr']:>8}{r['sec']:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
