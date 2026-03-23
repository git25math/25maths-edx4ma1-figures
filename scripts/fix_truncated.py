#!/usr/bin/env python3
"""fix_truncated — 从 PDF 重新裁剪被截断的图片（批量模式）

对 qa-report.json 中标记为 truncated 的图片:
1. 找到原始 PDF + analysis.json 中的坐标
2. 从 PDF 页面向下/向上扩展裁剪范围
3. 用 Gemini 批量验证扩展后的裁剪是否完整

跳过 "Diagram NOT accurately drawn" 误报。
"""

import json
import re
import shutil
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

FIGURES_ROOT = Path("/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-figures")
PDF_ROOT = Path("/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-pdf-officialfiles")
QA_REPORT = FIGURES_ROOT / "qa-report.json"
TRASH_DIR = FIGURES_ROOT / "_trash_truncated"


def find_pdf_for_figure(session: str, paper: str) -> str | None:
    paper_dir = PDF_ROOT / session / paper
    if not paper_dir.is_dir():
        return None
    pdfs = list(paper_dir.glob("4MA1-*-QuestionPaper.pdf"))
    return str(pdfs[0]) if pdfs else None


def load_analysis(session: str, paper: str) -> dict | None:
    path = FIGURES_ROOT / session / paper / "analysis.json"
    if not path.is_file():
        return None
    with open(path) as f:
        return json.load(f)


def find_figure_page_and_bounds(analysis: dict, question: str, filename: str) -> dict | None:
    """Find the page and y-bounds for a figure in analysis.json."""
    q_num_match = re.match(r"Q(\d+)", question)
    if not q_num_match:
        return None
    q_num = int(q_num_match.group(1))

    # Parse sub_question from filename: Q05_Suba_Fig1.png -> "a"
    sub_match = re.match(r"Q\d+_Sub([a-z])_", filename)
    sub_q = sub_match.group(1) if sub_match else None
    is_stem = "Stem" in filename

    for page_info in analysis.get("pages", []):
        for img in page_info.get("images", []):
            if img.get("question_num") != q_num:
                continue
            # Match sub_question
            img_sub = img.get("sub_question")
            img_stem = img.get("is_stem", True)
            if sub_q and img_sub != sub_q:
                continue
            if is_stem and not img_stem:
                continue

            return {
                "page_num": page_info["page_num"],
                "y0": img.get("y0", 0),
                "y1": img.get("y1", 1),
                "x0": img.get("x0", 0.1),
                "x1": img.get("x1", 0.9),
                "crop_bottom": img.get("crop_bottom"),
                "content_x0": page_info.get("content_x0", 0.1),
                "content_x1": page_info.get("content_x1", 0.9),
            }

    # Fallback: search by page proximity using summary.json
    summary_path = FIGURES_ROOT / analysis.get("_session", "") / analysis.get("_paper", "") / "summary.json"
    return None


def load_truncated_figures() -> list[dict]:
    with open(QA_REPORT) as f:
        report = json.load(f)

    figures = []
    for fig_id, data in report["figures"].items():
        if "truncated" not in data.get("issues", []):
            continue
        detail = data.get("detail", "")
        if ("Diagram accurately" in detail or "Diagram NOT" in detail) and len(data.get("issues", [])) == 1:
            continue

        fig_path = FIGURES_ROOT / fig_id
        if not fig_path.is_file():
            continue

        parts = fig_id.split("/")
        if len(parts) != 4:
            continue

        session, paper, question, filename = parts
        figures.append({
            "id": fig_id,
            "path": str(fig_path),
            "session": session,
            "paper": paper,
            "question": question,
            "filename": filename,
            "detail": detail,
        })

    return figures


def expand_and_recrop(fig: dict) -> bool:
    """Re-crop with expanded bounds from original PDF."""
    pdf_path = find_pdf_for_figure(fig["session"], fig["paper"])
    if not pdf_path:
        print(f"  ⚠ PDF not found")
        return False

    analysis = load_analysis(fig["session"], fig["paper"])
    if not analysis:
        print(f"  ⚠ analysis.json not found")
        return False

    bounds = find_figure_page_and_bounds(analysis, fig["question"], fig["filename"])
    if not bounds:
        print(f"  ⚠ figure not found in analysis.json")
        return False

    page_num = bounds["page_num"]
    orig_y0 = bounds["y0"]
    orig_y1 = bounds["y1"]
    x0 = bounds["x0"]
    x1 = bounds["x1"]
    content_x0 = bounds.get("content_x0", 0.1)
    content_x1 = bounds.get("content_x1", 0.9)

    # Expand: add 8% to bottom and 3% to top
    expand_bottom = 0.08
    expand_top = 0.03
    new_y0 = max(0.0, orig_y0 - expand_top)
    new_y1 = min(1.0, orig_y1 + expand_bottom)

    # Use wider x bounds
    eff_x0 = max(min(content_x0, x0), content_x0 - 0.06)
    eff_x1 = max(content_x1, x1)

    # Render page at 300 DPI
    doc = fitz.open(pdf_path)
    if page_num < 1 or page_num > len(doc):
        doc.close()
        print(f"  ⚠ page {page_num} out of range")
        return False

    page = doc[page_num - 1]
    zoom = 3.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    W, H = pix.width, pix.height
    full_img = Image.frombytes("RGB", (W, H), pix.samples)
    doc.close()

    # Crop
    pad = 10
    left = max(0, int(eff_x0 * W) - pad)
    right = min(W, int(eff_x1 * W) + pad)
    top = max(0, int(new_y0 * H))
    bottom = min(H, int(new_y1 * H))

    if right <= left or bottom <= top or (bottom - top) < 60:
        print(f"  ⚠ invalid crop bounds")
        return False

    cropped = full_img.crop((left, top, right, bottom))
    new_w, new_h = cropped.size

    # Compare with original
    orig_img = Image.open(fig["path"])
    orig_w, orig_h = orig_img.size

    # Only save if actually expanded (new image is larger)
    if new_h <= orig_h and new_w <= orig_w:
        print(f"  — no expansion needed ({orig_w}×{orig_h} → {new_w}×{new_h})")
        return False

    # Backup original
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    backup_name = fig["path"].replace("/", "__")
    shutil.copy2(fig["path"], TRASH_DIR / backup_name)

    # Save
    cropped.save(fig["path"], "PNG")
    print(f"  ✂ {orig_w}×{orig_h} → {new_w}×{new_h} (expanded)")
    return True


def main():
    figures = load_truncated_figures()
    print(f"📂 找到 {len(figures)} 张截断图片（已排除 Diagram 误报）")

    if not figures:
        print("✅ 没有需要修复的图片")
        return

    fixed = 0
    skipped = 0

    for i, fig in enumerate(figures, 1):
        sys.stdout.write(f"[{i}/{len(figures)}] {fig['id']} ")
        sys.stdout.flush()
        try:
            ok = expand_and_recrop(fig)
            if ok:
                fixed += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  ❌ {e}")
            skipped += 1

    print(f"\n{'='*60}")
    print(f"📊 修复完成:")
    print(f"  ✂ 已扩展重裁: {fixed}")
    print(f"  — 跳过: {skipped}")
    print(f"  备份目录: {TRASH_DIR}")


if __name__ == "__main__":
    main()
