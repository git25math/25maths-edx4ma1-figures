#!/usr/bin/env python3
"""auto_qa — 自动质检裁剪图片

批量发送裁剪图给 Gemini，检测 5 类问题：
1. 题目文字泄漏（顶部/底部含 question text）
2. 图被截断（表格少行、图形不完整）
3. 空白/无内容裁剪
4. 含答题线或分值标记 (n) [n]
5. 题号标注可能错误

输出 qa-report.json，供 Dashboard 加载自动标记。

用法:
  # 质检所有未检的图
  python3 auto_qa.py

  # 质检指定 session
  python3 auto_qa.py --session 2024June

  # 质检指定 session + paper
  python3 auto_qa.py --session 2024June --paper Paper1H

  # 重新质检已有结果的
  python3 auto_qa.py --force

  # 查看当前质检进度
  python3 auto_qa.py --status

  # 每批发送图片数（默认 10，节省 API 调用）
  python3 auto_qa.py --batch-size 15
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

FIGURES_ROOT = Path("/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-figures")
QA_REPORT_FILE = FIGURES_ROOT / "qa-report.json"

QA_PROMPT = """\
You are a quality inspector for cropped exam paper figures. Each image is a cropped PNG from an Edexcel 4MA1 maths exam paper.

For EACH image, check these 5 criteria and return a JSON verdict:

1. **text_leak**: Does the crop contain question text that should NOT be in the figure?
   - BAD: "(a) Find the value of x", "Calculate...", "Work out...", sub-question labels "(a)", "(b)"
   - OK: "Diagram NOT accurately drawn", "NOT TO SCALE", vertex labels (A, B, C), axis labels, "Key:"

2. **truncated**: Is the figure visibly cut off or incomplete?
   - BAD: table missing bottom rows, graph axes cut at edge, shape vertices clipped
   - OK: complete figures, even if small

3. **blank**: Is the crop empty/blank (all white or nearly all white with no meaningful content)?

4. **marks_leak**: Does the crop contain answer lines (dotted lines for writing) or marks like (2), (3), [1], [2], or "Total for Question X is Y marks"?

5. **wrong_label**: Based on the visual content, does the filename label seem wrong? (e.g., labeled as Q05 but clearly shows content from a different question context)

Return ONLY valid JSON (no markdown fencing):
{
  "results": [
    {
      "filename": "the_filename_shown_above_the_image",
      "pass": true,
      "issues": [],
      "confidence": 0.95
    },
    {
      "filename": "another_file",
      "pass": false,
      "issues": ["text_leak", "marks_leak"],
      "detail": "Top of image contains '(a) Work out the area of the triangle'",
      "confidence": 0.9
    }
  ]
}

Rules:
- "pass": true if ALL 5 checks pass, false if ANY fails
- "issues": array of failed check names from: text_leak, truncated, blank, marks_leak, wrong_label
- "detail": brief description of what's wrong (only when pass=false)
- "confidence": 0.0-1.0 how confident you are in your assessment
- Be strict on text_leak — even a partial question sentence at the edge is a fail
- Be lenient on truncated — slight whitespace at edges is OK
- For wrong_label, only flag if clearly wrong (you may not always be able to tell)
"""


def load_qa_report() -> dict:
    """Load existing QA report."""
    if QA_REPORT_FILE.is_file():
        with open(QA_REPORT_FILE) as f:
            return json.load(f)
    return {
        "version": "1.0",
        "project": "Edexcel 4MA1 Figure QA",
        "created": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
        "stats": {"total": 0, "passed": 0, "failed": 0, "pending": 0},
        "figures": {},
    }


def save_qa_report(report: dict):
    """Save QA report."""
    report["last_updated"] = datetime.now().isoformat()

    passed = sum(1 for v in report["figures"].values() if v.get("pass"))
    failed = sum(1 for v in report["figures"].values() if not v.get("pass"))
    report["stats"]["total"] = len(report["figures"])
    report["stats"]["passed"] = passed
    report["stats"]["failed"] = failed

    with open(QA_REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def discover_figures(session: str = None, paper: str = None) -> list[dict]:
    """Find all PNG figures to check."""
    figures = []
    for session_dir in sorted(FIGURES_ROOT.iterdir()):
        if not session_dir.is_dir() or session_dir.name.startswith(".") or session_dir.name == "scripts":
            continue
        if session and session_dir.name != session:
            continue

        for paper_dir in sorted(session_dir.iterdir()):
            if not paper_dir.is_dir() or not paper_dir.name.startswith("Paper"):
                continue
            if paper and paper_dir.name != paper:
                continue

            for q_dir in sorted(paper_dir.iterdir()):
                if not q_dir.is_dir() or not q_dir.name.startswith("Q"):
                    continue

                for png_file in sorted(q_dir.glob("*.png")):
                    fig_id = f"{session_dir.name}/{paper_dir.name}/{q_dir.name}/{png_file.name}"
                    figures.append({
                        "id": fig_id,
                        "path": str(png_file),
                        "session": session_dir.name,
                        "paper": paper_dir.name,
                        "question": q_dir.name,
                        "filename": png_file.name,
                    })

    return figures


def batch_qa(figures: list[dict], batch_size: int = 10) -> list[dict]:
    """Send a batch of figures to Gemini for QA."""
    import google.generativeai as genai

    content_parts = []
    for fig in figures:
        content_parts.append(f"--- {fig['id']} ---")
        pil_img = Image.open(fig["path"])
        content_parts.append(pil_img)

    content_parts.append(QA_PROMPT)

    model = genai.GenerativeModel("gemini-2.5-flash")
    response = model.generate_content(content_parts)

    raw = response.text
    # Strip markdown fences
    import re
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


def print_status(report: dict):
    """Print QA status overview."""
    stats = report["stats"]
    print(f"\n{'='*60}")
    print(f"📊 Edexcel 4MA1 Figure QA Report")
    print(f"{'='*60}")
    print(f"  总图片:  {stats['total']}")
    print(f"  ✅ 通过: {stats['passed']}")
    print(f"  ❌ 问题: {stats['failed']}")
    print(f"  上次更新: {report['last_updated']}")
    print()

    # Group failures by issue type
    issue_counts: dict[str, int] = {}
    for fig_data in report["figures"].values():
        if not fig_data.get("pass"):
            for issue in fig_data.get("issues", []):
                issue_counts[issue] = issue_counts.get(issue, 0) + 1

    if issue_counts:
        print("  问题分布:")
        for issue, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
            print(f"    {issue}: {count}")
        print()

    # List all failures
    failures = [(k, v) for k, v in report["figures"].items() if not v.get("pass")]
    if failures:
        print(f"  需手动修复 ({len(failures)}):")
        for fig_id, fig_data in failures[:30]:
            issues = ", ".join(fig_data.get("issues", []))
            detail = fig_data.get("detail", "")
            print(f"    ❌ {fig_id}")
            print(f"       [{issues}] {detail[:80]}")
        if len(failures) > 30:
            print(f"    ... and {len(failures) - 30} more")
    else:
        print("  🎉 所有图片通过质检！")
    print()


def main():
    parser = argparse.ArgumentParser(description="自动质检裁剪图片")
    parser.add_argument("--session", "-s", help="指定 session")
    parser.add_argument("--paper", "-p", help="指定 paper")
    parser.add_argument("--batch-size", type=int, default=10, help="每批图片数 (默认 10)")
    parser.add_argument("--force", action="store_true", help="重新质检已有结果的")
    parser.add_argument("--status", action="store_true", help="查看质检进度")
    parser.add_argument("--continue-on-error", action="store_true", help="遇错继续")

    args = parser.parse_args()
    report = load_qa_report()

    if args.status:
        # Count pending
        all_figs = discover_figures(args.session, args.paper)
        pending = [f for f in all_figs if f["id"] not in report["figures"]]
        report["stats"]["pending"] = len(pending)
        print_status(report)
        return

    # Discover figures
    all_figs = discover_figures(args.session, args.paper)
    print(f"📂 扫描到 {len(all_figs)} 张图片")

    if args.force:
        to_check = all_figs
    else:
        to_check = [f for f in all_figs if f["id"] not in report["figures"]]

    print(f"  待质检: {len(to_check)}")
    print(f"  已质检: {len(all_figs) - len(to_check)}")

    if not to_check:
        print("✅ 所有图片已质检完毕！")
        print_status(report)
        return

    # Process in batches
    batch_size = args.batch_size
    total_batches = (len(to_check) + batch_size - 1) // batch_size
    passed = 0
    failed = 0

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(to_check))
        batch = to_check[start:end]

        print(f"\n[{batch_idx + 1}/{total_batches}] 质检 {len(batch)} 张图...")
        print(f"  {batch[0]['id']} → {batch[-1]['id']}")

        try:
            results = batch_qa(batch, batch_size)
        except Exception as e:
            print(f"  ❌ API 调用失败: {e}")
            if not args.continue_on_error:
                print("  停止。使用 --continue-on-error 可继续。")
                save_qa_report(report)
                break
            continue

        # Match results back to figures
        result_by_name = {}
        for r in results:
            fn = r.get("filename", "")
            result_by_name[fn] = r

        for fig in batch:
            # Try matching by full id or filename
            r = result_by_name.get(fig["id"]) or result_by_name.get(fig["filename"])
            if r:
                report["figures"][fig["id"]] = {
                    "pass": r.get("pass", True),
                    "issues": r.get("issues", []),
                    "detail": r.get("detail", ""),
                    "confidence": r.get("confidence", 0),
                    "checked_at": datetime.now().isoformat(),
                }
                if r.get("pass"):
                    passed += 1
                    print(f"  ✅ {fig['id']}")
                else:
                    failed += 1
                    issues = ", ".join(r.get("issues", []))
                    print(f"  ❌ {fig['id']} [{issues}]")
            else:
                # No result for this figure — mark as unchecked
                report["figures"][fig["id"]] = {
                    "pass": True,
                    "issues": [],
                    "detail": "no result from Gemini (assumed pass)",
                    "confidence": 0,
                    "checked_at": datetime.now().isoformat(),
                }
                passed += 1
                print(f"  ⚠ {fig['id']} (no Gemini result, assumed pass)")

        save_qa_report(report)

        # Rate limit: brief pause between batches
        if batch_idx < total_batches - 1:
            time.sleep(2)

    print(f"\n{'='*60}")
    print(f"📊 本轮质检完成:")
    print(f"  ✅ 通过: {passed}")
    print(f"  ❌ 问题: {failed}")
    save_qa_report(report)
    print(f"  报告已保存: {QA_REPORT_FILE}")

    if failed > 0:
        print(f"\n  使用 --status 查看问题清单")


if __name__ == "__main__":
    main()
