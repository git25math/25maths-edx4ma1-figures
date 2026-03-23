#!/usr/bin/env python3
"""fix_marks_trunc_label — 修复 marks_leak / truncated / wrong_label

marks_leak: 发 Gemini 判断从底部裁掉多少像素去除 (n) 标记
truncated: 从 PDF 扩展重裁（复用 fix_truncated 逻辑）
wrong_label: placeholder 图直接删除
"""

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import fitz
from PIL import Image

FIGURES_ROOT = Path("/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-figures")
PDF_ROOT = Path("/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-pdf-officialfiles")
QA_REPORT = FIGURES_ROOT / "qa-report.json"
TRASH_DIR = FIGURES_ROOT / "_trash_marks_trunc"

MARKS_TRIM_PROMPT = """\
These exam figure crops contain marks indicators like (1), (2), (3) or answer lines that should be removed.

For EACH image, check the BOTTOM and RIGHT edges for marks indicators like "(2)", "(3)", answer lines (dotted lines), or "Total for Question" text.

Tell me what percentage of the image height to trim from the bottom to remove these marks.

Rules:
- Only trim marks indicators "(n)" and answer lines — do NOT trim figure content
- "Diagram NOT accurately drawn" is figure content — do NOT trim
- If marks are embedded within the figure (not at edges), set trim to 0
- Be conservative — better to leave a small mark than cut figure content

Return ONLY valid JSON (no markdown):
{
  "results": [
    {
      "filename": "the_filename",
      "trim_bottom_pct": 8,
      "reason": "trimmed (2) marks indicator from bottom right"
    }
  ]
}
"""


def load_figures_by_issue(issue_type: str) -> list[dict]:
    with open(QA_REPORT) as f:
        report = json.load(f)

    figures = []
    for fig_id, data in report["figures"].items():
        if issue_type not in data.get("issues", []):
            continue
        # Skip Diagram false positives for truncated
        if issue_type == "truncated":
            detail = data.get("detail", "")
            if ("Diagram accurately" in detail or "Diagram NOT" in detail) and len(data.get("issues", [])) == 1:
                continue

        fig_path = FIGURES_ROOT / fig_id
        if not fig_path.is_file():
            continue

        parts = fig_id.split("/")
        if len(parts) != 4:
            continue

        figures.append({
            "id": fig_id,
            "path": str(fig_path),
            "session": parts[0],
            "paper": parts[1],
            "question": parts[2],
            "filename": parts[3],
            "detail": data.get("detail", ""),
        })
    return figures


def batch_marks_trim(figures: list[dict], batch_size: int = 10) -> dict:
    """Send batch to Gemini for marks trim analysis."""
    import google.generativeai as genai

    all_results = {}
    total_batches = (len(figures) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(figures))
        batch = figures[start:end]

        print(f"\n  [marks {batch_idx+1}/{total_batches}] 分析 {len(batch)} 张...")

        content_parts = []
        for fig in batch:
            content_parts.append(f"--- {fig['id']} ---")
            content_parts.append(Image.open(fig["path"]))
        content_parts.append(MARKS_TRIM_PROMPT)

        try:
            model = genai.GenerativeModel("gemini-2.5-flash")
            response = model.generate_content(content_parts)
            raw = response.text
            raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.MULTILINE)
            raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE)
            raw = raw.strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                s, e = raw.find("{"), raw.rfind("}")
                if s >= 0 and e > s:
                    data = json.loads(raw[s:e+1])
                else:
                    print(f"    ❌ JSON parse failed")
                    continue

            for r in data.get("results", []):
                all_results[r.get("filename", "")] = r
        except Exception as e:
            print(f"    ❌ API error: {e}")

        if batch_idx < total_batches - 1:
            time.sleep(2)

    return all_results


def apply_trim(fig_path: str, trim_bottom_pct: float) -> bool:
    if trim_bottom_pct <= 0:
        return False
    img = Image.open(fig_path)
    w, h = img.size
    new_bottom = h - int(h * trim_bottom_pct / 100)
    if new_bottom < 40:
        return False

    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fig_path, TRASH_DIR / fig_path.replace("/", "__"))
    trimmed = img.crop((0, 0, w, new_bottom))
    trimmed.save(fig_path, "PNG")
    return True


def expand_recrop(fig: dict) -> bool:
    """Re-crop truncated figure with expanded bounds from PDF."""
    pdf_dir = PDF_ROOT / fig["session"] / fig["paper"]
    if not pdf_dir.is_dir():
        return False
    pdfs = list(pdf_dir.glob("4MA1-*-QuestionPaper.pdf"))
    if not pdfs:
        return False

    analysis_path = FIGURES_ROOT / fig["session"] / fig["paper"] / "analysis.json"
    if not analysis_path.is_file():
        return False
    with open(analysis_path) as f:
        analysis = json.load(f)

    q_match = re.match(r"Q(\d+)", fig["question"])
    if not q_match:
        return False
    q_num = int(q_match.group(1))

    # Find figure in analysis
    found = None
    for page_info in analysis.get("pages", []):
        for img in page_info.get("images", []):
            if img.get("question_num") == q_num:
                found = {"page_num": page_info["page_num"], "img": img}
                break
        if found:
            break

    if not found:
        return False

    page_num = found["page_num"]
    y0 = found["img"].get("y0", 0)
    y1 = found["img"].get("y1", 1)
    x0 = found["img"].get("x0", 0.1)
    x1 = found["img"].get("x1", 0.9)

    # Expand 10% bottom, 5% top
    new_y0 = max(0.0, y0 - 0.05)
    new_y1 = min(1.0, y1 + 0.10)

    doc = fitz.open(str(pdfs[0]))
    if page_num < 1 or page_num > len(doc):
        doc.close()
        return False

    page = doc[page_num - 1]
    pix = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0))
    W, H = pix.width, pix.height
    full_img = Image.frombytes("RGB", (W, H), pix.samples)
    doc.close()

    pad = 10
    left = max(0, int(x0 * W) - pad)
    right = min(W, int(x1 * W) + pad)
    top = max(0, int(new_y0 * H))
    bottom = min(H, int(new_y1 * H))

    if right <= left or bottom <= top:
        return False

    cropped = full_img.crop((left, top, right, bottom))
    new_w, new_h = cropped.size

    orig_img = Image.open(fig["path"])
    orig_w, orig_h = orig_img.size

    if new_h <= orig_h and new_w <= orig_w:
        return False

    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fig["path"], TRASH_DIR / fig["path"].replace("/", "__"))
    cropped.save(fig["path"], "PNG")
    return True


def main():
    # 1. wrong_label — delete placeholders
    print("=" * 60)
    print("Phase 1: wrong_label — 删除 placeholder 图")
    wl = load_figures_by_issue("wrong_label")
    print(f"  找到 {len(wl)} 张")
    for fig in wl:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(fig["path"], str(TRASH_DIR / fig["path"].replace("/", "__")))
        # Remove empty parent dirs
        parent = Path(fig["path"]).parent
        try:
            parent.rmdir()
        except OSError:
            pass
        print(f"  🗑 {fig['id']}")

    # 2. marks_leak — Gemini trim
    print()
    print("=" * 60)
    print("Phase 2: marks_leak — 裁掉底部分值标记")
    ml = load_figures_by_issue("marks_leak")
    print(f"  找到 {len(ml)} 张")

    if ml:
        results = batch_marks_trim(ml)
        trimmed = 0
        skipped = 0
        for fig in ml:
            r = results.get(fig["id"]) or results.get(fig["filename"])
            if not r:
                print(f"  ⚠ {fig['id']} — no result")
                skipped += 1
                continue
            bot = r.get("trim_bottom_pct", 0) or 0
            if bot <= 0:
                print(f"  — {fig['id']} (no trim: {r.get('reason','')})")
                skipped += 1
                continue
            ok = apply_trim(fig["path"], bot)
            if ok:
                trimmed += 1
                print(f"  ✂ {fig['id']} bot={bot}% ({r.get('reason','')})")
            else:
                skipped += 1
        print(f"\n  marks_leak: {trimmed} 裁切, {skipped} 跳过")

    # 3. truncated — PDF expand
    print()
    print("=" * 60)
    print("Phase 3: truncated — 从 PDF 扩展重裁")
    tr = load_figures_by_issue("truncated")
    print(f"  找到 {len(tr)} 张")

    fixed = 0
    skipped = 0
    for i, fig in enumerate(tr, 1):
        sys.stdout.write(f"  [{i}/{len(tr)}] {fig['id']} ")
        sys.stdout.flush()
        try:
            ok = expand_recrop(fig)
            if ok:
                fixed += 1
                print("✂ expanded")
            else:
                skipped += 1
                print("— skip")
        except Exception as e:
            skipped += 1
            print(f"❌ {e}")

    print(f"\n  truncated: {fixed} 扩展, {skipped} 跳过")

    print(f"\n{'='*60}")
    print(f"📊 全部完成，备份目录: {TRASH_DIR}")


if __name__ == "__main__":
    main()
