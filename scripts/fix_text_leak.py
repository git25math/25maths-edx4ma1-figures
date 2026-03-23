#!/usr/bin/env python3
"""fix_text_leak — 自动修复 text_leak 问题

读取 qa-report.json 中标记为 text_leak 的图片，
批量发送给 Gemini 判断从顶/底裁掉多少像素，然后自动裁切。

跳过 "Diagram NOT accurately drawn" 误报。
"""

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

FIGURES_ROOT = Path("/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-figures")
QA_REPORT = FIGURES_ROOT / "qa-report.json"
TRASH_DIR = FIGURES_ROOT / "_trash_text_leak"

TRIM_PROMPT = """\
You are trimming exam paper figures that have question text leaking into the crop.

For EACH image, look at the TOP and BOTTOM edges. If there is question text (not figure content) at the top or bottom, tell me exactly what percentage of the image height to trim from the top and/or bottom.

Rules:
- "Diagram NOT accurately drawn" or fragments like "Diagram accurately" are FIGURE CAPTIONS — do NOT trim them
- Vertex labels (A, B, C), axis labels, "NOT TO SCALE", "Key:" are FIGURE CONTENT — do NOT trim
- Question text, sub-question labels "(a)", "(b)", answer instructions, marks "(2)" — these should be trimmed
- Only trim if you're confident. If unsure, set trim to 0.

Return ONLY valid JSON (no markdown fencing):
{
  "results": [
    {
      "filename": "the_filename",
      "trim_top_pct": 0,
      "trim_bottom_pct": 0,
      "reason": "brief description of what was trimmed or 'no trim needed'"
    }
  ]
}

trim_top_pct: percentage of image height to remove from top (0-50)
trim_bottom_pct: percentage of image height to remove from bottom (0-50)
If no trim needed, set both to 0.
"""


def load_text_leak_figures() -> list[dict]:
    """Find all text_leak figures, excluding Diagram NOT false positives."""
    with open(QA_REPORT) as f:
        report = json.load(f)

    figures = []
    for fig_id, data in report["figures"].items():
        if "text_leak" not in data.get("issues", []):
            continue
        # Skip "Diagram accurately" false positives
        detail = data.get("detail", "")
        if "Diagram accurately" in detail and len(data.get("issues", [])) == 1:
            continue

        fig_path = FIGURES_ROOT / fig_id
        if not fig_path.is_file():
            continue

        figures.append({
            "id": fig_id,
            "path": str(fig_path),
            "detail": detail,
        })

    return figures


def batch_trim_analysis(figures: list[dict]) -> list[dict]:
    """Send batch to Gemini for trim analysis."""
    import google.generativeai as genai

    content_parts = []
    for fig in figures:
        content_parts.append(f"--- {fig['id']} ---")
        pil_img = Image.open(fig["path"])
        content_parts.append(pil_img)

    content_parts.append(TRIM_PROMPT)

    model = genai.GenerativeModel("gemini-2.5-flash")
    response = model.generate_content(content_parts)

    raw = response.text
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(raw[start:end + 1])
        else:
            print(f"  ❌ JSON 解析失败", file=sys.stderr)
            return []

    return data.get("results", [])


def apply_trim(fig_path: str, trim_top_pct: float, trim_bottom_pct: float) -> bool:
    """Trim image and save, backing up original."""
    if trim_top_pct <= 0 and trim_bottom_pct <= 0:
        return False

    img = Image.open(fig_path)
    w, h = img.size

    top_px = int(h * trim_top_pct / 100)
    bottom_px = int(h * trim_bottom_pct / 100)

    new_top = top_px
    new_bottom = h - bottom_px

    if new_bottom - new_top < 40:
        print(f"  ⚠ 裁切后过小，跳过", file=sys.stderr)
        return False

    # Backup
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    backup_name = fig_path.replace("/", "__")
    shutil.copy2(fig_path, TRASH_DIR / backup_name)

    # Trim
    trimmed = img.crop((0, new_top, w, new_bottom))
    trimmed.save(fig_path, "PNG")

    return True


def main():
    figures = load_text_leak_figures()
    print(f"📂 找到 {len(figures)} 张 text_leak 图片（已排除 Diagram 误报）")

    if not figures:
        print("✅ 没有需要修复的图片")
        return

    batch_size = 10
    total_batches = (len(figures) + batch_size - 1) // batch_size
    trimmed = 0
    skipped = 0

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(figures))
        batch = figures[start:end]

        print(f"\n[{batch_idx + 1}/{total_batches}] 分析 {len(batch)} 张图...")

        try:
            results = batch_trim_analysis(batch)
        except Exception as e:
            print(f"  ❌ API 失败: {e}")
            continue

        result_by_name = {}
        for r in results:
            fn = r.get("filename", "")
            result_by_name[fn] = r

        for fig in batch:
            r = result_by_name.get(fig["id"]) or result_by_name.get(Path(fig["path"]).name)
            if not r:
                print(f"  ⚠ {fig['id']} — 无 Gemini 结果")
                skipped += 1
                continue

            top_pct = r.get("trim_top_pct", 0) or 0
            bot_pct = r.get("trim_bottom_pct", 0) or 0
            reason = r.get("reason", "")

            if top_pct <= 0 and bot_pct <= 0:
                print(f"  — {fig['id']} (no trim: {reason})")
                skipped += 1
                continue

            ok = apply_trim(fig["path"], top_pct, bot_pct)
            if ok:
                trimmed += 1
                print(f"  ✂ {fig['id']} top={top_pct}% bot={bot_pct}% ({reason})")
            else:
                skipped += 1

        if batch_idx < total_batches - 1:
            time.sleep(2)

    print(f"\n{'='*60}")
    print(f"📊 修复完成:")
    print(f"  ✂ 已裁切: {trimmed}")
    print(f"  — 跳过: {skipped}")
    print(f"  备份目录: {TRASH_DIR}")


if __name__ == "__main__":
    main()
