#!/usr/bin/env python3
"""exam_cropper — Edexcel 4MA1 试卷 PDF 图片智能裁剪工具

Uses Gemini API to detect figure locations in exam PDFs,
then renders pages via PyMuPDF and crops images with auto-naming.

Output structure: 25maths-edx4ma1-figures/{session}/{paper}/{Qnn}/{Qnn}.png
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image


# Base output directory
FIGURES_ROOT = "/Users/zhuxingzhe/Project/ExamBoard/25maths-edx4ma1-figures"

ANALYSIS_PROMPT = """\
You are analyzing an Edexcel 4MA1 (IGCSE Mathematics A) exam paper. For each page image, identify every figure/diagram/graph/image.

Return ONLY valid JSON (no markdown fencing) with this structure:
{
  "pages": [
    {
      "page_num": 1,
      "content_x0": 0.10,
      "content_x1": 0.88,
      "images": [
        {
          "question_num": 5,
          "sub_question": null,
          "is_stem": true,
          "fig_idx": 1,
          "x0": 0.15, "y0": 0.30, "x1": 0.75, "y1": 0.55,
          "crop_top": 0.28,
          "crop_bottom": 0.57
        }
      ]
    }
  ]
}

CRITICAL coordinate rules — all values are fractions of page width/height (0.0–1.0):

Page-level fields:
- content_x0: the x-coordinate (fraction of page width) where the BODY TEXT starts,
  EXCLUDING the question number column on the left. In Edexcel papers, question numbers
  (e.g. "5", "12") sit in a narrow left column; body text starts to the right of that.
  Typically around 0.08–0.12.
- content_x1: the x-coordinate where body text ends on the right side.
  Typically around 0.88–0.92.

Figure bounding box (x0, y0, x1, y1):
- x0, y0 = top-left corner; x1, y1 = bottom-right corner.
- The bounding box must TIGHTLY wrap the figure's visual content only.
- DO include: axis labels, tick marks, vertex labels (A, B, P, Q), "NOT TO SCALE",
  "Diagram NOT accurately drawn", legends, or explanatory text directly attached to the figure.
- DO NOT include: question text lines above/below the figure, sub-question labels like "(a)",
  answer lines/spaces, marks in parentheses like "(2)", total marks lines like
  "(Total for Question X is Y marks)", or any whitespace beyond the figure.
- For tables: x0 should be the left edge of the table border, x1 the right edge.
- CRITICAL: y0 must be the TOP edge of the figure (e.g., top of axis arrow, top table border).
  y1 must be the BOTTOM edge (e.g., bottom axis label, bottom table border, or caption like
  "Diagram NOT accurately drawn" if directly under the figure). Do NOT extend y1 to include
  question text, answer blanks, or the next sub-question.

Crop boundaries (crop_top, crop_bottom):
- crop_top = y-coordinate of BOTTOM EDGE of last question text line ABOVE the figure.
- crop_bottom = y-coordinate of TOP EDGE of first question text line BELOW the figure.
- Figure captions ("NOT TO SCALE", "Diagram NOT accurately drawn", legends) are part of the
  figure, NOT question text. crop_bottom must be BELOW such captions.

Other fields:
- question_num: the question number (integer). null if unknown.
- sub_question: sub-part letter if specific to a sub-part (e.g. "a", "b"). null for stem.
- is_stem: true if the figure belongs to the question stem.
- fig_idx: 1-based index when a question has multiple figures.

What to include: geometry diagrams, coordinate grids/axes WITH plotted data or shapes,
function graphs, data tables, stem-and-leaf diagrams, Venn diagrams, bar charts, pie charts,
frequency polygons, box plots, cumulative frequency diagrams, scatter diagrams, etc.
What to exclude:
- Decorative elements, logos, page headers/footers
- BLANK coordinate axes with no plotted data (these are answer spaces for students to draw on)
- Empty answer lines or spaces
- Formula sheets
If a page has no figures, include it with an empty images array.
"""


def parse_pdf_name(pdf_path: str) -> tuple[str, str] | None:
    """Parse Edexcel PDF filename like '4MA1-1F-2024June-QuestionPaper.pdf'.

    Returns (session_dir, paper_dir) or None if not parseable.
    """
    stem = Path(pdf_path).stem  # e.g. '4MA1-1F-2024June-QuestionPaper'
    m = re.match(r"4MA1-(\d[A-Z]+)-(\w+)-QuestionPaper", stem)
    if not m:
        return None
    paper_code, session = m.groups()
    return session, f"Paper{paper_code}"


def detect_content_bounds(pdf_path: str) -> tuple[float, float]:
    """Detect body text left/right boundaries from PDF text positions.

    Returns (content_x0, content_x1) as fractions of page width.
    - Left: most common x0 of long text spans (excludes question number column)
    - Right: just to the left of marks like (1), (2) (excludes marks column)
    """
    from collections import Counter
    doc = fitz.open(pdf_path)
    W = doc[0].rect.width
    x0_values = []
    marks_x0_values = []

    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        for b in blocks:
            if b["type"] == 0:
                for line in b["lines"]:
                    full_text = "".join(s["text"] for s in line["spans"]).strip()
                    # Edexcel uses (n) for marks and (Total for Question X is Y marks)
                    if re.match(r"^\(\d+\)$", full_text):
                        marks_x0_values.append(round(line["bbox"][0] / W, 3))
                        continue
                    if "Total for Question" in full_text:
                        continue
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if text and len(text) > 3:
                            x0_values.append(round(span["bbox"][0] / W, 3))
    doc.close()

    if not x0_values:
        return 0.10, 0.88

    # Left: body text start position
    # Edexcel body text starts at 0.08–0.14
    body_region = [v for v in x0_values if 0.08 <= v <= 0.14]
    if body_region:
        x0_counter = Counter(body_region)
        content_x0 = x0_counter.most_common(1)[0][0]
    else:
        left_region = [v for v in x0_values if 0.05 <= v <= 0.25]
        if left_region:
            x0_counter = Counter(left_region)
        else:
            x0_counter = Counter(x0_values)
        content_x0 = x0_counter.most_common(1)[0][0]

    # Right: left edge of marks column minus a small gap
    if marks_x0_values:
        marks_x0 = min(marks_x0_values)
        content_x1 = marks_x0 - 0.01
    else:
        content_x1 = 0.88

    return content_x0, content_x1


def get_page_text_map(pdf_path: str) -> dict:
    """Extract text lines with positions for each page.

    Returns dict: page_num (1-based) -> list of {text, y0, y1, x_center, x0}
    All y values are fractions of page height (0.0-1.0).

    Uses dict mode first; falls back to blocks mode if bbox values are corrupt.
    """
    doc = fitz.open(pdf_path)
    text_map = {}

    for page_idx, page in enumerate(doc):
        W, H = page.rect.width, page.rect.height
        lines = []

        # Try dict mode first
        dict_ok = True
        for b in page.get_text("dict")["blocks"]:
            if b["type"] == 0:
                bbox = b["bbox"]
                if abs(bbox[0]) > 100000 or abs(bbox[1]) > 100000:
                    dict_ok = False
                    break
                for line in b["lines"]:
                    full_text = "".join(s["text"] for s in line["spans"]).strip()
                    if not full_text:
                        continue
                    lbbox = line["bbox"]
                    lines.append({
                        "text": full_text,
                        "y0": lbbox[1] / H,
                        "y1": lbbox[3] / H,
                        "x_center": (lbbox[0] + lbbox[2]) / 2 / W,
                        "x0": lbbox[0] / W,
                    })

        if not dict_ok:
            lines = []
            for b in page.get_text("blocks"):
                x0, y0, x1, y1, text, block_no, block_type = b
                if block_type != 0 or abs(x0) > 100000:
                    continue
                text = text.strip()
                if not text:
                    continue
                for line_text in text.split("\n"):
                    line_text = line_text.strip()
                    if not line_text:
                        continue
                    lines.append({
                        "text": line_text,
                        "y0": y0 / H,
                        "y1": y1 / H,
                        "x_center": (x0 + x1) / 2 / W,
                        "x0": x0 / W,
                    })

        text_map[page_idx + 1] = lines

    doc.close()
    return text_map


def find_page_number_bottom(text_lines: list) -> float:
    """Find bottom y of page number at top of page.

    Edexcel page numbers are typically at the top, centered or right-aligned.
    Returns y1 fraction, or 0.0 if not found.
    """
    for line in text_lines:
        text = line["text"]
        # Edexcel: page number at top, or header like "4  *P12345A0428*"
        if (re.match(r"^\d{1,2}$", text)
                and line["y0"] < 0.08):
            return line["y1"]
        # Edexcel header pattern with asterisks
        if re.match(r"^\*P\d+", text) and line["y0"] < 0.06:
            return line["y1"]
    return 0.0


def is_question_text(text: str, x0: float = 0.0, content_x0: float = 0.10) -> bool:
    """Check if a text line is question/sub-question text (not a figure label).

    Adapted for Edexcel 4MA1 papers:
    - Marks use parentheses (1), (2) instead of brackets [1], [2]
    - "Total for Question X is Y marks" lines
    - "Diagram NOT accurately drawn" is a figure caption (NOT question text)
    """
    text = text.strip()
    if not text:
        return False

    # Standalone question number — ONLY if in the question number column
    if re.match(r"^\d{1,2}$", text) and x0 < content_x0:
        return True

    if len(text) < 2:
        return False

    # Sub-question labels: (a) ..., (b) ..., (i) ..., (ii) ...
    if re.match(r"^\([a-z]\)", text) or re.match(r"^\([ivx]+\)", text):
        return True

    # Question number prefix: "16 The speed..." or "16 (a) In the Venn..."
    m = re.match(r"^\d{1,2}\s+(.+)", text)
    if m and x0 < content_x0:
        text = m.group(1)
        if re.match(r"^\([a-z]\)", text) or re.match(r"^\([ivx]+\)", text):
            return True

    # Answer lines (dotted/dashed lines)
    cleaned = text.replace(" ", "")
    if re.match(r"^[.·…_]{25,}", cleaned):
        return True
    dot_groups = re.findall(r"[.·…_]+", cleaned)
    if len(dot_groups) >= 2 and sum(len(g) for g in dot_groups) >= 20:
        return True
    if re.match(r"^[a-zA-Z]\s*=\s*[.·…_]{8,}", text):
        return True
    if re.match(r"^\(\s*[.·…_]{5,}", text):
        return True
    if re.match(r"^[.·…_]{2,}\s*[a-zA-Z]{1,4}\s*[.·…_]{2,}", text):
        return True
    m_dot = re.match(r"^[.·…_]{10,24}([^.·…_\s].*)", cleaned)
    if m_dot:
        suffix = re.sub(r"\(\d+\)", "", m_dot.group(1)).strip()
        if len(suffix) <= 2:
            return True

    # Edexcel marks: (1), (2), (3), etc.
    if re.match(r"^\(\d+\)$", text):
        return True
    # Total marks line
    if re.match(r"^\(Total for Question", text):
        return True

    # Question text patterns
    if len(text) >= 10:
        q_starts = [
            "Find ", "Find\n", "Calculate ", "Show that", "Write down", "Work out",
            "Give your", "Give a reason", "Explain", "Describe", "State ",
            "Determine", "Simplify", "Solve", "Factorise", "Draw ",
            "Sketch ", "Complete the", "Fill in", "Use your", "Using ",
            "On the ", "On your ", "Measure ", "Construct ", "Plot ",
            "Label ", "Enlarge ", "Rotate ", "Reflect ", "Translate ",
            "Estimate ", "Round ", "Convert ", "Make ", "Express ",
            "The diagram", "The table", "The graph",
            "The speed", "The distance", "The bearing",
            "The line", "The points", "The ratio",
            "The scale", "The probability", "The equation",
            "The height", "The area", "The volume",
            "The frequency", "The cumulative",
            "The results", "The information", "The number",
            "The lengths", "The masses", "The ages",
            "The travel", "The total", "The cross",
            "The perimeter", "The surface", "The stem",
            "The box", "The bar", "The pie", "The histogram",
            "The scatter", "The cumul",
            "The Venn", "The shape", "The sector",
            "Some ", "All ", "Angle ",
            "You must show", "You must not",
            "Answer", "NOT TO",
            "By ", "This ", "These ", "Each ",
            "A straight", "A circle", "A rectangle",
            "A bag", "A box", "A card", "A dice", "A fair",
            "A spinner", "A number", "A coin", "A solid",
            "Shape ", "Shapes ",
            "The region", "The solid", "The container",
            "The cost", "The price", "The mass", "The weight",
            "The pictogram", "The histogram", "The pie chart",
            "The bar chart",
            "The map", "The plan", "The grid",
            "She ", "He ", "It ", "They ", "There ",
            "One ", "Two ", "Three ", "Four ", "Five ",
            "Six ", "Seven ", "Eight ", "Nine ", "Ten ",
            "In the ", "In this ", "For ",
            "On each ", "On a ",
            "Triangle ", "Rectangle ", "Circle ",
            "Mr ", "Mrs ",
            "Shade ", "You ",
            "A cone ", "A pyramid ", "A hemisphere ",
            "Points ", "Point ",
            "The first ", "The second ",
            "The area ", "The volume ", "The perimeter ",
            "The height ", "The length ", "The width ",
            "The radius ", "The diameter ",
            "ABCD", "PQRS", "EFGH", "KLMN", "OABC",
            "OAB ", "OPQ ", "ORT ",
            "E, F", "P, Q", "A, B", "X, Y",
            "A and B", "P and Q", "X and Y",
            "In triangle", "In Triangle",
            "ABC,", "DEF,", "PQR,", "ABC ", "PQR ", "OAB,",
            "AB is", "BC is", "AC is", "PQ is", "QR is",
            "AB and", "BC and", "CD and",
            "Here is", "Here are",
            "Below is", "Below are",
            "Show your", "Show that",
            "Correct ", "Incorrect",
            "Write ", "Read ", "List ",
            "How many", "How much", "How far", "How long",
            "What is", "What are", "What was",
            "Which ", "Where ", "When ",
            "Is it", "Are there",
        ]
        # Continuation patterns
        if re.search(r"\b(is equal to|is the same as|are shown|are given|has been drawn|lies on the|lie on a|is a natural|is a positive|is an integer|is a solid|is a straight|is a pentagon|is a hexagon|is a quadrilateral|is a triangle|is a parallelogram|is a trapezium|is a rhombus|is a kite|is a sector|is a prism|is a cylinder|has area|has perimeter|has radius|has diameter|are points on|is a point on|is the midpoint|is the centre|shows the|shows information|shows that|represents|on the grid|on the diagram|on your diagram|draw the graph|draw a line|draw the line)\b", text):
            return True
        # Set definitions
        if re.match(r"^.{1,3}\s*=\s*\{", text):
            return True
        # Short continuation fragments
        if re.match(r"^\d+\s+[a-z].*\.$", text) and len(text) < 30:
            return True
        # Edexcel-specific: copyright/paper codes
        if "Pearson" in text or "Edexcel" in text or re.match(r"^4MA1/", text):
            return True
        if re.match(r"^\*P\d+", text):
            return True
        # Exclude known figure captions
        fig_captions = [
            "NOT TO SCALE",
            "Diagram NOT accurately drawn",
            "Diagram NOT",
            "Key:", "Key :",
            "O is the origin",
            "is the point",
        ]
        for fc in fig_captions:
            if fc in text:
                return False

        for qs in q_starts:
            if text.startswith(qs):
                return True

        # Long lowercase text is likely question continuation
        if len(text) > 15 and text[0].islower():
            return True

    # Short question patterns
    if text.startswith("Find n") or text.startswith("Find\n"):
        return True
    if text in ("Find", "Calculate", "Simplify", "Solve"):
        return True

    # Edexcel marks (n) — already handled above
    # Marks bracket [1], [2] (some Edexcel papers may also use this)
    if re.match(r"^\[\d+\]$", text):
        return True

    return False


def refine_vertical_bounds(
    fig_y0: float, fig_y1: float, page_lines: list,
    page_num_bottom: float, content_x0: float = 0.10,
    content_x1: float = 0.88,
    original_y1: float = None,
    crop_bottom: float = None,
) -> tuple[float, float]:
    """Refine Gemini's y0/y1 using PyMuPDF text layer.

    Strategy:
    1. Clamp y0 below page number/header
    2. Top expansion: include non-question-text lines above y0 (catches table headers)
    3. Top exclusion: push y0 below question text
    4. Bottom zone: scan for question text, but skip marks column
    5. Key line protection: if "Key:" found near y1, ensure it's included
    """
    # 1. Ensure y0 is below page number
    if page_num_bottom > 0 and fig_y0 < page_num_bottom + 0.01:
        fig_y0 = page_num_bottom + 0.01

    fig_height = fig_y1 - fig_y0
    if fig_height <= 0.02:
        return fig_y0, fig_y1

    # 2. Top expansion
    expand_scan_top = max(page_num_bottom + 0.01, fig_y0 - 0.06)
    above_lines = []
    for line in page_lines:
        if line["y0"] < expand_scan_top or line["y0"] >= fig_y0:
            continue
        is_qt = is_question_text(line["text"], line.get("x0", 0.5), content_x0)
        above_lines.append((line, is_qt))
    above_lines.sort(key=lambda x: x[0]["y0"], reverse=True)
    qt_y_set = {l["y0"] for l, qt in above_lines if qt}
    for line, is_qt in above_lines:
        if is_qt:
            break
        if any(abs(line["y0"] - qt_y) < 0.005 for qt_y in qt_y_set):
            break
        fig_y0 = min(fig_y0, line["y0"])

    # 3. Top exclusion
    fig_height = fig_y1 - fig_y0
    top_zone = fig_y0 + fig_height * 0.25
    for line in sorted(page_lines, key=lambda l: l["y0"]):
        if line["y1"] < fig_y0 - 0.02:
            continue
        if line["y0"] > top_zone:
            break
        if is_question_text(line["text"], line.get("x0", 0.5), content_x0):
            text = line["text"].strip()
            x0 = line.get("x0", 0.5)
            if x0 < content_x0 and len(text) <= 5:
                continue
            if re.match(r"^\([a-z]\)$", text) or re.match(r"^\([ivx]+\)$", text):
                continue
            fig_y0 = max(fig_y0, line["y1"] + 0.01)

    # 4. Bottom zone
    fig_height = fig_y1 - fig_y0
    scan_start = fig_y1 - fig_height * 0.55
    scan_end = fig_y1 + (0.07 if fig_height < 0.2 else 0.03)

    bottom_zone_lines = []
    for line in page_lines:
        if line["y0"] < scan_start or line["y0"] > scan_end:
            continue
        bottom_zone_lines.append(line)

    non_qt_ys = set()
    for line in bottom_zone_lines:
        if not is_question_text(line["text"], line.get("x0", 0.5), content_x0):
            non_qt_ys.add(line["y0"])

    bottom_question_lines = []
    for line in bottom_zone_lines:
        if line.get("x0", 0) > content_x1:
            continue
        if is_question_text(line["text"], line.get("x0", 0.5), content_x0):
            text = line["text"].strip()
            # Skip standalone marks — they shouldn't truncate figures
            if re.match(r"^\(\d+\)$", text):
                continue
            if re.match(r"^\[\d+\]$", text):
                continue
            bottom_question_lines.append(line)

    if bottom_question_lines:
        earliest = min(bottom_question_lines, key=lambda l: l["y0"])
        fig_y1 = min(fig_y1, earliest["y0"] - 0.01)

    # 4.5. Bottom expansion
    expand_scan_bottom = min(1.0, fig_y1 + 0.06)
    if crop_bottom is not None:
        expand_scan_bottom = min(expand_scan_bottom, crop_bottom)
    below_lines = []
    for line in page_lines:
        if line["y0"] < fig_y1 or line["y0"] > expand_scan_bottom:
            continue
        text = line["text"].strip()
        if re.match(r"^\(\d+\)$", text) or re.match(r"^\[\d+\]$", text):
            below_lines.append((line, True))
            continue
        is_qt = is_question_text(text, line.get("x0", 0.5), content_x0)
        if not is_qt and len(text) > 20:
            is_qt = True
        below_lines.append((line, is_qt))
    below_lines.sort(key=lambda x: x[0]["y0"])
    qt_y_set_below = {l["y0"] for l, qt in below_lines if qt}
    for line, is_qt in below_lines:
        if is_qt:
            break
        if any(abs(line["y0"] - qt_y) < 0.005 for qt_y in qt_y_set_below):
            break
        fig_y1 = max(fig_y1, min(line["y1"], expand_scan_bottom))

    # 5. Key line protection
    for line in page_lines:
        text = line["text"].strip()
        if ("Key:" in text or "Key :" in text) and fig_y1 - 0.03 < line["y0"] < fig_y1 + 0.06:
            if line["y1"] > fig_y1:
                new_y1 = line["y1"] + 0.005
                if crop_bottom is not None:
                    new_y1 = min(new_y1, crop_bottom)
                fig_y1 = new_y1
                break

    return fig_y0, fig_y1


def parse_page_range(spec: str, max_page: int) -> list[int]:
    """Parse page range like '1-5' or '2,4,6' into 0-based page indices."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            for p in range(a, b + 1):
                if 1 <= p <= max_page:
                    pages.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= max_page:
                pages.add(p - 1)
    return sorted(pages)


def analyze_pdf(pdf_path: str, page_indices: list[int] | None = None) -> dict:
    """Render PDF pages as images and send to Gemini for figure detection."""
    import google.generativeai as genai

    doc = fitz.open(pdf_path)
    all_pages = page_indices if page_indices else list(range(len(doc)))

    pil_images = []
    for pi in all_pages:
        page = doc[pi]
        pix = page.get_pixmap(dpi=150)
        pil_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        pil_images.append((pi + 1, pil_img))
    doc.close()

    content_parts = []
    for page_num, pil_img in pil_images:
        content_parts.append(f"--- Page {page_num} ---")
        content_parts.append(pil_img)
    content_parts.append(ANALYSIS_PROMPT)

    print("📨 发送 PDF 页面至 Gemini 分析...")
    model = genai.GenerativeModel("gemini-2.5-flash")
    response = model.generate_content(content_parts)

    raw = response.text
    raw = re.sub(r"^```(?:json)?\s*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```\s*$", "", raw, flags=re.MULTILINE)
    raw = raw.strip()

    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                analysis = json.loads(raw[start:end + 1])
            except json.JSONDecodeError as e:
                print(f"❌ Gemini 返回的 JSON 解析失败: {e}", file=sys.stderr)
                print(f"原始响应:\n{raw[:800]}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"❌ Gemini 返回中未找到 JSON", file=sys.stderr)
            print(f"原始响应:\n{raw[:800]}", file=sys.stderr)
            sys.exit(1)

    total = sum(len(p.get("images", [])) for p in analysis.get("pages", []))
    print(f"✓ 分析完成，共识别 {total} 张图片")
    return analysis


def format_name(fmt: str, q_num, sub_q, is_stem: bool, fig_idx: int, page_num: int) -> str:
    """Apply naming template. Q numbers are zero-padded to 2 digits."""
    if q_num is not None:
        q_str = f"Q{int(q_num):02d}"
    else:
        q_str = "Unknown"

    if sub_q is not None:
        sub_str = f"Sub{sub_q}"
    else:
        sub_str = "Stem"

    name = fmt.replace("{Q}", q_str)
    name = name.replace("{Sub}", sub_str)
    name = name.replace("{Fig}", str(fig_idx))
    name = name.replace("{Page}", str(page_num))
    return name


def crop_images(
    pdf_path: str,
    analysis: dict,
    output_dir: str,
    name_fmt: str,
    zoom: float,
    pad_top: int,
    pad_bot: int,
    pad_lr: int = 10,
    per_question_dirs: bool = False,
) -> list[dict]:
    """Render pages and crop detected figures."""
    content_x0, content_x1 = detect_content_bounds(pdf_path)
    print(f"📏 正文边界: x0={content_x0:.3f}, x1={content_x1:.3f}")

    text_map = get_page_text_map(pdf_path)

    doc = fitz.open(pdf_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    summary = []
    used_names = set()

    for page_info in analysis.get("pages", []):
        page_num = page_info["page_num"]
        page_idx = page_num - 1
        if page_idx < 0 or page_idx >= len(doc):
            print(f"⚠ 跳过不存在的页码 {page_num}", file=sys.stderr)
            continue

        page = doc[page_idx]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        W, H = pix.width, pix.height

        full_img = Image.frombytes("RGB", (W, H), pix.samples)

        page_lines = text_map.get(page_num, [])
        page_num_bottom = find_page_number_bottom(page_lines)

        for img_info in page_info.get("images", []):
            fig_y0 = img_info["y0"]
            fig_y1 = img_info["y1"]

            original_y0 = fig_y0
            original_y1 = fig_y1

            crop_bottom = img_info.get("crop_bottom")
            crop_top = img_info.get("crop_top")
            if crop_bottom and crop_bottom > fig_y1 + 0.02:
                fig_y1 = crop_bottom - 0.01
            if crop_top and crop_top < fig_y0 - 0.02:
                fig_y0 = crop_top + 0.01

            fig_y0, fig_y1 = refine_vertical_bounds(
                fig_y0, fig_y1, page_lines, page_num_bottom, content_x0,
                content_x1=content_x1, original_y1=original_y1,
                crop_bottom=crop_bottom,
            )

            fig_x0 = img_info.get("x0", content_x0)
            fig_x1 = img_info.get("x1", content_x1)
            eff_x0 = max(min(content_x0, fig_x0), content_x0 - 0.06)
            eff_x1 = max(content_x1, fig_x1)
            left = int(eff_x0 * W) - pad_lr
            right = int(eff_x1 * W) + pad_lr

            pad_frac = 0.005
            top = int((fig_y0 - pad_frac) * H) - pad_top
            bottom = int((fig_y1 + pad_frac) * H) + pad_bot

            left = max(0, left)
            right = min(W, right)
            top = max(0, top)
            bottom = min(H, bottom)

            if right <= left or bottom <= top:
                print(f"⚠ 页 {page_num} 图片坐标无效，跳过", file=sys.stderr)
                continue

            if (bottom - top) < 80:
                q = img_info.get("question_num", "?")
                print(f"⚠ 页 {page_num} Q{q} 裁切结果过小 ({bottom-top}px)，跳过",
                      file=sys.stderr)
                continue

            pil_img = full_img.crop((left, top, right, bottom))

            q_num = img_info.get("question_num")
            sub_q = img_info.get("sub_question")
            is_stem = img_info.get("is_stem", True)
            fig_idx = img_info.get("fig_idx", 1)

            fname = format_name(name_fmt, q_num, sub_q, is_stem, fig_idx, page_num)
            while fname in used_names:
                fig_idx += 1
                fname = format_name(name_fmt, q_num, sub_q, is_stem, fig_idx, page_num)
            used_names.add(fname)
            fname = fname + ".png"

            if per_question_dirs and q_num is not None:
                q_dir = out / f"Q{int(q_num):02d}"
                q_dir.mkdir(parents=True, exist_ok=True)
                save_path = q_dir / fname
            else:
                save_path = out / fname

            pil_img.save(str(save_path), "PNG")

            w, h = pil_img.size
            rel_path = save_path.relative_to(out)
            print(f"✓ {rel_path} ({w}×{h} px)")

            summary.append({
                "filename": str(rel_path),
                "page": page_num,
                "question_num": q_num,
                "sub_question": sub_q,
                "is_stem": is_stem,
                "fig_idx": fig_idx,
                "width": w,
                "height": h,
            })

    doc.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Edexcel 4MA1 试卷 PDF 图片智能裁剪工具 — Gemini + PyMuPDF",
    )
    parser.add_argument("pdf", help="输入 PDF 文件路径")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="输出目录 (默认自动解析到 25maths-edx4ma1-figures/)")
    parser.add_argument("--name-fmt", default="{Q}_{Sub}_Fig{Fig}",
                        help="命名模板 (默认 {Q}_{Sub}_Fig{Fig})")
    parser.add_argument("--pad-top", type=int, default=0, help="上边距额外像素 (默认 0)")
    parser.add_argument("--pad-bot", type=int, default=0, help="下边距额外像素 (默认 0)")
    parser.add_argument("--pad-lr", type=int, default=10, help="左右边距额外像素 (默认 10)")
    parser.add_argument("--zoom", type=float, default=3.0, help="渲染缩放倍率 (默认 3.0，约 300 DPI)")
    parser.add_argument("--page", default=None, help="页码范围，如 1-5 或 2,4,6 (默认全部)")
    parser.add_argument("--analysis-json", default=None,
                        help="跳过 API 调用，直接使用已有的分析 JSON 文件")
    parser.add_argument("--force", action="store_true",
                        help="忽略已有 analysis.json，强制重新调用 Gemini API")
    parser.add_argument("--flat", action="store_true",
                        help="不按题号分子目录，所有图片平铺在输出目录")

    args = parser.parse_args()

    pdf_path = args.pdf
    if not os.path.isfile(pdf_path):
        print(f"❌ 文件不存在: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    # Determine output directory
    if args.output_dir:
        output_dir = args.output_dir
    else:
        parsed = parse_pdf_name(pdf_path)
        if parsed:
            session_dir, paper_dir = parsed
            output_dir = os.path.join(FIGURES_ROOT, session_dir, paper_dir)
            print(f"📁 输出目录: {output_dir}")
        else:
            output_dir = "./output_images"
            print(f"⚠ 无法从文件名解析 session/paper，使用默认目录: {output_dir}",
                  file=sys.stderr)

    # Determine page range
    doc = fitz.open(pdf_path)
    max_pages = len(doc)
    doc.close()

    page_indices = None
    if args.page:
        page_indices = parse_page_range(args.page, max_pages)
        if not page_indices:
            print(f"❌ 无效页码范围: {args.page}", file=sys.stderr)
            sys.exit(1)

    # Get analysis
    auto_analysis_path = Path(output_dir) / "analysis.json"
    analysis_source = args.analysis_json or (
        str(auto_analysis_path) if auto_analysis_path.exists() and not args.force else None
    )
    if analysis_source:
        with open(analysis_source) as f:
            analysis = json.load(f)
        total = sum(len(p.get("images", [])) for p in analysis.get("pages", []))
        print(f"✓ 从 {analysis_source} 加载分析结果，共 {total} 张图片")
    else:
        if args.force:
            print("🔄 --force 模式，重新调用 Gemini API...")
        analysis = analyze_pdf(pdf_path, page_indices)

        analysis_path = Path(output_dir) / "analysis.json"
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        with open(analysis_path, "w") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)
        print(f"✓ 分析结果已保存: {analysis_path}")

    # Crop
    per_question_dirs = not args.flat
    summary = crop_images(
        pdf_path, analysis, output_dir,
        args.name_fmt, args.zoom, args.pad_top, args.pad_bot, args.pad_lr,
        per_question_dirs,
    )

    # Write summary
    summary_path = Path(output_dir) / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\n🎉 完成！共导出 {len(summary)} 张图片 → {output_dir}/")
    print(f"summary.json 已生成")


if __name__ == "__main__":
    main()
