#!/usr/bin/env python3
"""
Generate figure-map.json for Edexcel 4MA1 figures (GitHub Pages URLs).

Unlike the CIE version, there is no Part A (copy to LaTeX) since
Edexcel papers don't have a LaTeX PastPapers directory structure.
"""

import json
import sys
from pathlib import Path

FIGURES_ROOT = Path("/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-figures")
GITHUB_PAGES_BASE = "https://git25math.github.io/25maths-edx4ma1-figures"


def load_summary(paper_dir: Path) -> list:
    """Load summary.json from a paper directory."""
    summary_path = paper_dir / "summary.json"
    if not summary_path.exists():
        return []
    with open(summary_path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "images" in data:
        return data["images"]
    return []


def generate_figure_map():
    """Generate figure-map.json for ExamHub with GitHub Pages URLs."""
    figure_map = {}

    for session_dir in sorted(FIGURES_ROOT.iterdir()):
        if not session_dir.is_dir() or session_dir.name.startswith(".") or session_dir.name == "scripts":
            continue
        session = session_dir.name

        for paper_dir in sorted(session_dir.iterdir()):
            if not paper_dir.is_dir() or not paper_dir.name.startswith("Paper"):
                continue
            paper = paper_dir.name
            paper_key = f"{session}/{paper}"

            # Load summary.json for metadata
            summaries = load_summary(paper_dir)
            summary_by_file = {}
            for entry in summaries:
                fn = entry.get("filename", "")
                summary_by_file[fn] = entry

            paper_figures = {}

            for q_dir in sorted(paper_dir.iterdir()):
                if not q_dir.is_dir() or not q_dir.name.startswith("Q"):
                    continue
                q_name = q_dir.name

                figures = []
                for png_file in sorted(q_dir.glob("*.png")):
                    rel_path = f"{q_name}/{png_file.name}"
                    url = f"{GITHUB_PAGES_BASE}/{session}/{paper}/{q_name}/{png_file.name}"

                    fig_entry = {
                        "filename": png_file.name,
                        "url": url,
                    }

                    # Enrich with summary metadata if available
                    meta = summary_by_file.get(rel_path, {})
                    if meta:
                        fig_entry["question_num"] = meta.get("question_num")
                        fig_entry["sub_question"] = meta.get("sub_question")
                        fig_entry["is_stem"] = meta.get("is_stem")
                        fig_entry["fig_idx"] = meta.get("fig_idx")
                        fig_entry["width"] = meta.get("width")
                        fig_entry["height"] = meta.get("height")
                        fig_entry["page"] = meta.get("page")

                    figures.append(fig_entry)

                if figures:
                    paper_figures[q_name] = figures

            if paper_figures:
                figure_map[paper_key] = paper_figures

    return figure_map


def main():
    print("=" * 60)
    print("Generate figure-map.json for Edexcel 4MA1")
    print("=" * 60)

    figure_map = generate_figure_map()

    output_path = FIGURES_ROOT / "figure-map.json"
    with open(output_path, "w") as f:
        json.dump(figure_map, f, indent=2, ensure_ascii=False)

    total_papers = len(figure_map)
    total_questions = sum(len(qs) for qs in figure_map.values())
    total_figures = sum(
        len(figs) for qs in figure_map.values() for figs in qs.values()
    )
    print(f"  Papers:    {total_papers}")
    print(f"  Questions: {total_questions}")
    print(f"  Figures:   {total_figures}")
    print(f"  Output:    {output_path}")
    print()
    print("Done!")


if __name__ == "__main__":
    main()
