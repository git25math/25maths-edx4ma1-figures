#!/usr/bin/env python3
"""batch_crop — 批量裁剪 Edexcel 4MA1 试卷图片

扫描 PDF 源目录，自动匹配输出目录，调用 crop.py 处理每张试卷。
自动更新 progress.json 进度文件，支持跨对话接力。

用法:
  # 处理所有未处理的试卷（跳过已有 analysis.json 的）
  python3 batch_crop.py

  # 重新裁剪所有已有 analysis.json 的试卷（代码修复后使用）
  python3 batch_crop.py --re-crop

  # 强制重新分析+裁剪（重新调用 Gemini API）
  python3 batch_crop.py --force

  # 只处理指定 session
  python3 batch_crop.py --session 2024June

  # 只处理指定 session + paper
  python3 batch_crop.py --session 2024June --paper Paper1H

  # 只处理指定 session 范围
  python3 batch_crop.py --from 2023Jan --to 2024Nov

  # 预览会处理哪些试卷（不实际执行）
  python3 batch_crop.py --dry-run

  # 查看当前进度
  python3 batch_crop.py --status

  # 标记某张试卷验收通过
  python3 batch_crop.py --mark-passed --session 2024June --paper Paper1H

  # 标记某张试卷需要修复
  python3 batch_crop.py --mark-fix --session 2024June --paper Paper1H --issue "Q3 diagram truncated"
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────────
PDF_ROOT = "/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-pdf-officialfiles"
FIGURES_ROOT = "/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-figures"
PROGRESS_FILE = Path(FIGURES_ROOT) / "progress.json"
CROP_SCRIPT = Path(__file__).parent / "crop.py"

# Session 排序权重
# Edexcel sessions: 2017SP, 2018June, 2019Jan, 2019June, 2020Jan, etc.
MONTH_ORDER = {"SP": 0, "Jan": 1, "June": 2, "Nov": 3}


def session_sort_key(session_name: str) -> tuple:
    """将 '2024June' 转为 (2024, 2) 用于排序。"""
    for month, order in MONTH_ORDER.items():
        if session_name.endswith(month):
            year = session_name[: -len(month)]
            return (int(year), order)
    return (9999, 9)


def load_progress() -> dict:
    """加载进度文件，不存在则创建空模板。"""
    if PROGRESS_FILE.is_file():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {
        "version": "1.0",
        "project": "Edexcel 4MA1 Figure Extraction",
        "created": str(date.today()),
        "last_updated": str(date.today()),
        "tool_version": "crop.py v1.0",
        "stats": {
            "total_sessions": 0,
            "total_papers": 0,
            "completed": 0,
            "in_progress": 0,
            "failed": 0,
            "pending": 0,
        },
        "status_flow": "pending → cropped → verifying → passed | needs_fix → re_cropped → verifying → passed",
        "sessions": {},
        "bugs": [],
        "resume_instructions": "Read this file to know current state. Process the first session with status != 'complete'.",
    }


def save_progress(progress: dict):
    """保存进度文件。"""
    progress["last_updated"] = str(date.today())

    all_papers = discover_papers()
    real_total = len(all_papers)

    completed = 0
    in_progress = 0
    failed = 0
    for session_data in progress["sessions"].values():
        papers = session_data.get("papers", {})
        for paper_data in papers.values():
            status = paper_data.get("status", "pending")
            if status == "passed":
                completed += 1
            elif status in ("needs_fix", "failed"):
                failed += 1
            elif status in ("cropped", "verifying", "re_cropped"):
                in_progress += 1

    progress["stats"]["total_papers"] = real_total
    progress["stats"]["total_sessions"] = len({p["session"] for p in all_papers})
    progress["stats"]["completed"] = completed
    progress["stats"]["in_progress"] = in_progress
    progress["stats"]["failed"] = failed
    progress["stats"]["pending"] = real_total - completed - in_progress - failed

    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


def update_paper_status(
    progress: dict,
    session: str,
    paper: str,
    status: str,
    figures: int = 0,
    issues: list = None,
):
    """更新单张试卷的进度状态。"""
    if session not in progress["sessions"]:
        progress["sessions"][session] = {"status": "in_progress", "papers": {}}
    papers = progress["sessions"][session]["papers"]
    if paper not in papers:
        papers[paper] = {"status": "pending", "figures": 0, "issues": []}
    papers[paper]["status"] = status
    if figures > 0:
        papers[paper]["figures"] = figures
    if status == "passed":
        papers[paper]["verified_at"] = str(date.today())
    if issues:
        papers[paper]["issues"].extend(issues)

    all_passed = all(p.get("status") == "passed" for p in papers.values())
    if all_passed and len(papers) > 0:
        progress["sessions"][session]["status"] = "complete"


def discover_papers() -> list[dict]:
    """扫描 PDF_ROOT，返回所有可处理的试卷信息。"""
    papers = []
    pdf_root = Path(PDF_ROOT)

    for session_dir in sorted(pdf_root.iterdir()):
        if not session_dir.is_dir():
            continue
        session = session_dir.name

        for paper_dir in sorted(session_dir.iterdir()):
            if not paper_dir.is_dir():
                continue
            paper = paper_dir.name

            # Edexcel PDF naming: 4MA1-*-QuestionPaper.pdf
            qp_pdfs = list(paper_dir.glob("4MA1-*-QuestionPaper.pdf"))
            if not qp_pdfs:
                continue

            pdf_path = qp_pdfs[0]
            figures_dir = Path(FIGURES_ROOT) / session / paper
            analysis_path = figures_dir / "analysis.json"

            papers.append({
                "session": session,
                "paper": paper,
                "pdf_path": str(pdf_path),
                "figures_dir": str(figures_dir),
                "has_analysis": analysis_path.is_file(),
            })

    return papers


def filter_papers(
    papers: list[dict],
    session: str = None,
    paper: str = None,
    from_session: str = None,
    to_session: str = None,
) -> list[dict]:
    """按条件过滤试卷列表。"""
    result = papers

    if session:
        result = [p for p in result if p["session"] == session]
    if paper:
        result = [p for p in result if p["paper"] == paper]
    if from_session:
        from_key = session_sort_key(from_session)
        result = [p for p in result if session_sort_key(p["session"]) >= from_key]
    if to_session:
        to_key = session_sort_key(to_session)
        result = [p for p in result if session_sort_key(p["session"]) <= to_key]

    return result


def run_crop(
    pdf_path: str,
    figures_dir: str,
    analysis_json: str = None,
    force: bool = False,
) -> tuple[bool, int]:
    """运行 crop.py 处理单张试卷。

    Returns (success: bool, figure_count: int).
    """
    cmd = [
        sys.executable, str(CROP_SCRIPT),
        pdf_path,
        "--output-dir", figures_dir,
    ]
    if analysis_json:
        cmd += ["--analysis-json", analysis_json]
    if force:
        cmd += ["--force"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  ❌ 失败: {result.stderr.strip()[:200]}")
        return False, 0

    fig_count = 0
    for line in result.stdout.splitlines():
        if line.startswith("✓ Q") or line.startswith("✓ Unknown"):
            fig_count += 1
        print(f"  {line}")

    return True, fig_count


def print_status(progress: dict):
    """打印当前进度概览。"""
    stats = progress["stats"]
    print(f"\n{'='*60}")
    print(f"📊 Edexcel 4MA1 Figure Extraction Progress")
    print(f"{'='*60}")
    print(f"  总试卷: {stats['total_papers']}")
    print(f"  ✅ 已通过: {stats['completed']}")
    print(f"  🔄 进行中: {stats['in_progress']}")
    print(f"  ❌ 需修复: {stats['failed']}")
    print(f"  ⏳ 待处理: {stats['pending']}")
    print(f"  上次更新: {progress['last_updated']}")
    print()

    for session_name in sorted(progress["sessions"], key=session_sort_key, reverse=True):
        session_data = progress["sessions"][session_name]
        papers = session_data.get("papers", {})
        passed = sum(1 for p in papers.values() if p.get("status") == "passed")
        total = len(papers)
        status_icon = "✅" if session_data.get("status") == "complete" else "🔄"
        print(f"  {status_icon} {session_name}: {passed}/{total} passed")

        for paper_name, paper_data in sorted(papers.items()):
            if paper_data.get("status") not in ("passed", "pending"):
                print(f"      ⚠ {paper_name}: {paper_data['status']}")
            if paper_data.get("issues"):
                for issue in paper_data["issues"]:
                    print(f"        → {issue}")
    print()

    all_papers = discover_papers()
    for p in all_papers:
        session = p["session"]
        paper = p["paper"]
        if session in progress["sessions"]:
            papers = progress["sessions"][session].get("papers", {})
            if paper in papers and papers[paper].get("status") == "passed":
                continue
        if not p["has_analysis"]:
            print(f"  📌 下一个待处理: {session}/{paper}")
            break


def main():
    parser = argparse.ArgumentParser(
        description="批量裁剪 Edexcel 4MA1 试卷图片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--session", "-s", help="指定 session (如 2024June)")
    parser.add_argument("--paper", "-p", help="指定 paper (如 Paper1H)")
    parser.add_argument("--from", dest="from_session", help="起始 session (含)")
    parser.add_argument("--to", dest="to_session", help="结束 session (含)")
    parser.add_argument("--re-crop", action="store_true",
                        help="重新裁剪已有 analysis.json 的试卷（不重新调用 API）")
    parser.add_argument("--force", action="store_true",
                        help="强制重新分析+裁剪（重新调用 Gemini API）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只显示会处理哪些试卷，不实际执行")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="遇到错误继续处理下一张（默认停止）")
    parser.add_argument("--status", action="store_true",
                        help="查看当前进度")
    parser.add_argument("--mark-passed", action="store_true",
                        help="标记指定试卷验收通过（需配合 --session --paper）")
    parser.add_argument("--mark-fix", action="store_true",
                        help="标记指定试卷需修复（需配合 --session --paper --issue）")
    parser.add_argument("--issue", help="问题描述（配合 --mark-fix 使用）")

    args = parser.parse_args()
    progress = load_progress()

    # ── 状态查看 ──
    if args.status:
        print_status(progress)
        return

    # ── 标记验收通过 ──
    if args.mark_passed:
        if not args.session or not args.paper:
            print("❌ --mark-passed 需要配合 --session 和 --paper 使用")
            sys.exit(1)
        figures_dir = Path(FIGURES_ROOT) / args.session / args.paper
        fig_count = len(list(figures_dir.rglob("*.png"))) if figures_dir.is_dir() else 0
        update_paper_status(progress, args.session, args.paper, "passed", figures=fig_count)
        save_progress(progress)
        print(f"✅ {args.session}/{args.paper} 已标记为 passed ({fig_count} figures)")
        return

    # ── 标记需修复 ──
    if args.mark_fix:
        if not args.session or not args.paper:
            print("❌ --mark-fix 需要配合 --session 和 --paper 使用")
            sys.exit(1)
        issues = [args.issue] if args.issue else ["需要修复"]
        update_paper_status(progress, args.session, args.paper, "needs_fix", issues=issues)
        save_progress(progress)
        print(f"⚠ {args.session}/{args.paper} 已标记为 needs_fix: {args.issue or '需要修复'}")
        return

    # ── 发现所有试卷 ──
    all_papers = discover_papers()
    print(f"📂 扫描到 {len(all_papers)} 张试卷")

    papers = filter_papers(
        all_papers,
        session=args.session,
        paper=args.paper,
        from_session=args.from_session,
        to_session=args.to_session,
    )

    if not papers:
        print("⚠ 没有匹配的试卷")
        sys.exit(0)

    # 确定处理策略
    if args.force:
        to_process = papers
        mode = "force"
    elif args.re_crop:
        to_process = [p for p in papers if p["has_analysis"]]
        mode = "re-crop"
    else:
        to_process = [p for p in papers if not p["has_analysis"]]
        mode = "new"

    already_done = len([p for p in papers if p["has_analysis"]])
    print(f"  已处理: {already_done}")
    print(f"  待处理: {len(to_process)}")
    print(f"  模式: {mode}")
    print()

    if not to_process:
        print("✅ 所有试卷已处理完毕！")
        if not args.re_crop and not args.force:
            print("  提示: 使用 --re-crop 可重新裁剪（代码修复后），--force 可重新调用 API")
        sys.exit(0)

    if args.dry_run:
        print("📋 预览（--dry-run 模式，不实际执行）:")
        for p in to_process:
            status = "🔄 re-crop" if p["has_analysis"] else "🆕 new"
            print(f"  {status} {p['session']}/{p['paper']}")
        sys.exit(0)

    # ── 执行处理 ──
    success_count = 0
    fail_count = 0
    total_figures = 0

    for i, p in enumerate(to_process, 1):
        session, paper = p["session"], p["paper"]
        print(f"\n{'='*60}")
        print(f"[{i}/{len(to_process)}] {session}/{paper}")
        print(f"  PDF: {p['pdf_path']}")
        print(f"  输出: {p['figures_dir']}")

        analysis_json = None
        if mode == "re-crop" and p["has_analysis"]:
            analysis_json = os.path.join(p["figures_dir"], "analysis.json")
            print(f"  复用: {analysis_json}")
        elif mode != "force" and p["has_analysis"]:
            analysis_json = os.path.join(p["figures_dir"], "analysis.json")

        ok, fig_count = run_crop(
            p["pdf_path"], p["figures_dir"],
            analysis_json=analysis_json,
            force=(mode == "force"),
        )

        if ok:
            success_count += 1
            total_figures += fig_count
            update_paper_status(progress, session, paper, "cropped", figures=fig_count)
            save_progress(progress)
        else:
            fail_count += 1
            update_paper_status(progress, session, paper, "failed")
            save_progress(progress)
            if not args.continue_on_error:
                print(f"\n❌ 处理失败，停止。使用 --continue-on-error 可继续处理其余试卷。")
                break

    print(f"\n{'='*60}")
    print(f"📊 处理完成:")
    print(f"  成功: {success_count}")
    print(f"  失败: {fail_count}")
    print(f"  总图片: {total_figures}")
    print(f"  跳过: {len(papers) - len(to_process)}")
    print(f"\n  进度已保存到: {PROGRESS_FILE}")
    print(f"  提示: 使用 --status 查看总进度")


if __name__ == "__main__":
    main()
