from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import fitz

from .database import BASE_DIR, RESULT_DIR, get_connection


MAGIC_PDF = BASE_DIR / ".venv" / "bin" / "magic-pdf"


def run_recognition(document_id: str) -> None:
    started = time.perf_counter()
    with get_connection() as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()

    if doc is None:
        return

    pdf_path = Path(doc["pdf_path"])
    output_dir = Path(doc["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    _update_document(document_id, status="processing", error=None)

    try:
        command = [
            str(MAGIC_PDF),
            "-p",
            str(pdf_path),
            "-o",
            str(output_dir),
            "-m",
            doc["method"],
        ]
        subprocess.run(
            command,
            cwd=BASE_DIR,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        result_root = _find_result_root(output_dir, pdf_path.stem, doc["method"])
        markdown_path = result_root / f"{pdf_path.stem}.md"
        middle_path = result_root / f"{pdf_path.stem}_middle.json"
        content_list_path = result_root / f"{pdf_path.stem}_content_list.json"
        markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
        supplemented = supplement_visual_crops(pdf_path, markdown, content_list_path, result_root)
        markdown = insert_semantic_image_refs(markdown, supplemented)
        markdown = remove_supplemented_visual_text(markdown, middle_path, supplemented)
        markdown_clean = clean_markdown(markdown)
        assets = extract_asset_inventory(markdown, content_list_path, result_root)
        blocks = extract_positioned_blocks(pdf_path, middle_path, content_list_path, supplemented)
        duration = round(time.perf_counter() - started, 2)

        _update_document(
            document_id,
            status="done",
            markdown=markdown,
            markdown_clean=markdown_clean,
            blocks_json=json.dumps(blocks, ensure_ascii=False),
            assets_json=json.dumps(assets, ensure_ascii=False),
            markdown_original=markdown,
            markdown_clean_original=markdown_clean,
            blocks_json_original=json.dumps(blocks, ensure_ascii=False),
            pages=len(blocks["pages"]),
            duration_seconds=duration,
            error=None,
        )
    except Exception as exc:
        duration = round(time.perf_counter() - started, 2)
        _update_document(
            document_id,
            status="failed",
            error=str(exc),
            duration_seconds=duration,
        )


def extract_positioned_blocks(
    pdf_path: Path,
    middle_path: Path,
    content_list_path: Path | None = None,
    supplemented: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    doc = fitz.open(pdf_path)
    page_sizes = [
        {"page": idx, "width": float(page.rect.width), "height": float(page.rect.height), "blocks": []}
        for idx, page in enumerate(doc)
    ]

    if not middle_path.exists():
        return {"pages": page_sizes}

    raw = json.loads(middle_path.read_text(encoding="utf-8"))
    pdf_info = raw.get("pdf_info", [])
    visual_paths = _visual_paths_by_page(content_list_path)
    crop_rects = _crop_rects_by_page(supplemented or [])
    for page_idx, page_info in enumerate(pdf_info):
        if page_idx >= len(page_sizes):
            continue
        blocks = page_info.get("preproc_blocks") or page_info.get("para_blocks") or []
        normalized = []
        visual_index = 0
        for block_index, block in enumerate(blocks):
            block_type = str(block.get("type", "text")).lower()
            block_bbox = [float(value) for value in block.get("bbox", [0, 0, 0, 0])]
            if any(_bbox_overlaps_visual_crop(block_bbox, crop_bbox) for crop_bbox in crop_rects.get(page_idx, [])):
                continue
            image_path = None
            if block_type in {"image", "table"}:
                if supplemented:
                    visual_index += 1
                    continue
                paths = visual_paths.get(page_idx, [])
                if visual_index < len(paths):
                    image_path = paths[visual_index]
                visual_index += 1
            text = _block_text(block)
            if text or image_path:
                normalized.append(_normalize_block(block, image_path=image_path, page_idx=page_idx, block_index=block_index))
        page_sizes[page_idx]["blocks"] = normalized

    for crop_index, crop in enumerate(supplemented or []):
        page_idx = crop.get("page")
        if page_idx is None or page_idx >= len(page_sizes):
            continue
        page_sizes[page_idx]["blocks"].append(
            {
                "id": _block_id(int(page_idx), f"visual-{crop.get('type', 'image')}-{crop.get('number', crop_index)}"),
                "type": crop.get("type", "image"),
                "bbox": [float(value) for value in crop.get("bbox", [0, 0, 0, 0])],
                "text": "",
                "image_path": crop.get("path"),
            }
        )

    return {"pages": page_sizes}


def _crop_rects_by_page(crops: list[dict[str, Any]]) -> dict[int, list[list[float]]]:
    by_page: dict[int, list[list[float]]] = {}
    for crop in crops:
        page = crop.get("page")
        bbox = crop.get("bbox")
        if page is None or not bbox:
            continue
        by_page.setdefault(int(page), []).append([float(value) for value in bbox])
    return by_page


def rebuild_markdown_from_middle(middle_path: Path) -> str:
    if not middle_path.exists():
        return ""

    raw = json.loads(middle_path.read_text(encoding="utf-8"))
    pages = raw.get("pdf_info", [])
    parts: list[str] = []
    for page in pages:
        blocks = page.get("preproc_blocks") or page.get("para_blocks") or []
        for block in blocks:
            text = _clean_block_text(block)
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def clean_markdown(markdown: str) -> str:
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            paragraphs.append(_clean_markdown_paragraph(current))
            current = []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if _is_noise_markdown_line(stripped):
            flush()
            paragraphs.append("")
            continue
        if not stripped:
            flush()
            paragraphs.append("")
            continue
        if _is_markdown_structural_line(stripped):
            flush()
            paragraphs.append(stripped)
            continue
        current.append(stripped)
    flush()

    text = "\n".join(paragraphs)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_asset_inventory(markdown: str, content_list_path: Path | None = None, result_root: Path | None = None) -> dict[str, Any]:
    figures = _collect_numbered_labels(markdown, r"\bFigure\s+(\d+)[A-Za-z]?\b")
    tables = _collect_numbered_labels(markdown, r"\bTable\s+(\d+)[A-Za-z]?\b")
    charts = _collect_numbered_labels(markdown, r"\bChart\s+(\d+)[A-Za-z]?\b")
    exported_files = sorted(str(path.relative_to(result_root)) for path in (result_root / "images").glob("*") if path.is_file()) if result_root and (result_root / "images").exists() else []

    content_counts = {"image": 0, "table": 0}
    if content_list_path and content_list_path.exists():
        content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
        content_counts["image"] = sum(1 for item in content_list if item.get("type") == "image")
        content_counts["table"] = sum(1 for item in content_list if item.get("type") == "table")

    return {
        "semantic": {
            "figures": figures,
            "tables": tables,
            "charts": charts,
            "figure_count": len(figures),
            "table_count": len(tables),
            "chart_count": len(charts),
            "visual_total_count": len(figures) + len(tables) + len(charts),
        },
        "mineru_exports": {
            "image_file_count": len(exported_files),
            "image_files": exported_files,
            "content_image_count": content_counts["image"],
            "content_table_count": content_counts["table"],
        },
    }


def supplement_visual_crops(pdf_path: Path, markdown: str, content_list_path: Path, result_root: Path) -> list[dict[str, Any]]:
    assets = extract_asset_inventory(markdown, content_list_path, result_root)
    image_dir = result_root / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    created: list[dict[str, Any]] = []
    for number in assets["semantic"]["figures"]:
        target = image_dir / f"semantic_figure_{number}.png"
        crop = _crop_label_from_pdf(doc, "Figure", number, target)
        if crop:
            created.append({"type": "figure", "number": number, "path": str(target.relative_to(result_root)), **crop})

    for number in assets["semantic"]["tables"]:
        target = image_dir / f"semantic_table_{number}.png"
        crop = _crop_label_from_pdf(doc, "Table", number, target)
        if crop:
            created.append({"type": "table", "number": number, "path": str(target.relative_to(result_root)), **crop})

    for number in assets["semantic"]["charts"]:
        target = image_dir / f"semantic_chart_{number}.png"
        crop = _crop_label_from_pdf(doc, "Chart", number, target)
        if crop:
            created.append({"type": "chart", "number": number, "path": str(target.relative_to(result_root)), **crop})

    if created:
        print(f"[mineru-assets] supplemented {len(created)} visual crops: {created}")
    return created


def remove_supplemented_visual_text(markdown: str, middle_path: Path, crops: list[dict[str, Any]]) -> str:
    if not markdown or not crops or not middle_path.exists():
        return markdown

    raw = json.loads(middle_path.read_text(encoding="utf-8"))
    pages = raw.get("pdf_info", [])
    snippets: list[str] = []
    for crop in crops:
        page_index = crop.get("page")
        if page_index is None or page_index >= len(pages):
            continue
        crop_rect = crop.get("bbox") or [0, 0, 0, 0]
        for block in pages[page_index].get("preproc_blocks", []):
            block_bbox = [float(value) for value in block.get("bbox", [0, 0, 0, 0])]
            if _bbox_overlaps_visual_crop(block_bbox, crop_rect):
                text = _block_text(block)
                if text:
                    snippets.append(text)

    cleaned = markdown
    for snippet in sorted(set(snippets), key=len, reverse=True):
        cleaned = _remove_snippet_from_markdown(cleaned, snippet)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def insert_semantic_image_refs(markdown: str, crops: list[dict[str, Any]]) -> str:
    if not markdown or not crops:
        return markdown

    updated = re.sub(r"(?m)^\s*!\[[^\]]*\]\((?!https?://)(?!/api/)(?![^)]*semantic_)[^)]+\)\s*(?:  )?\s*$\n?", "", markdown)
    for crop in sorted(crops, key=lambda item: (item.get("page", 0), item.get("bbox", [0, 0, 0, 0])[1], item.get("number", 0))):
        path = crop.get("path")
        label = str(crop.get("type", "image")).capitalize()
        number = crop.get("number")
        if not path or number is None or path in updated:
            continue

        image_markdown = f"\n\n![]({path})\n\n"
        heading_pattern = re.compile(rf"(?im)^#*\s*{re.escape(label)}\s+{number}[A-Za-z]?\.")
        match = heading_pattern.search(updated)
        if not match:
            match = re.search(rf"\b{re.escape(label)}\s+{number}[A-Za-z]?\b", updated, flags=re.IGNORECASE)

        if match:
            insert_at = updated.rfind("\n\n", 0, match.start())
            insert_at = 0 if insert_at < 0 else insert_at + 2
            updated = updated[:insert_at] + image_markdown + updated[insert_at:]
        else:
            updated = updated.rstrip() + image_markdown

    return re.sub(r"\n{3,}", "\n\n", updated).strip()


def _bbox_overlaps_visual_crop(block_bbox: list[float], crop_bbox: list[float]) -> bool:
    bx0, by0, bx1, by1 = block_bbox
    cx0, cy0, cx1, cy1 = crop_bbox
    block_area = max((bx1 - bx0) * (by1 - by0), 1.0)
    ix0, iy0 = max(bx0, cx0), max(by0, cy0)
    ix1, iy1 = min(bx1, cx1), min(by1, cy1)
    overlap = max(ix1 - ix0, 0) * max(iy1 - iy0, 0)
    center_inside = cx0 <= (bx0 + bx1) / 2 <= cx1 and cy0 <= (by0 + by1) / 2 <= cy1
    return overlap / block_area > 0.45 or center_inside


def _remove_snippet_from_markdown(markdown: str, snippet: str) -> str:
    snippet = snippet.strip()
    if not snippet:
        return markdown

    heading_match = re.match(r"^(Chart|Figure|Table)\s+(\d+)\.", snippet, flags=re.IGNORECASE)
    if heading_match:
        label, number = heading_match.groups()
        heading_pattern = rf"(?m)^#*\s*{re.escape(label)}\s+{number}\.[^\n]*\n?"
        markdown = re.sub(heading_pattern, "", markdown, flags=re.IGNORECASE)

    pattern = _flexible_snippet_pattern(snippet)
    updated = re.sub(pattern, "", markdown, count=1, flags=re.IGNORECASE | re.DOTALL)
    if updated != markdown:
        return updated

    paragraphs = re.split(r"(\n\s*\n)", markdown)
    needle = _search_key(snippet)
    if len(needle) < 16:
        return markdown
    for index, paragraph in enumerate(paragraphs):
        if index % 2 == 1:
            continue
        haystack = _search_key(paragraph)
        if needle[:60] in haystack and _looks_like_visual_ocr(snippet):
            paragraphs[index] = ""
            return "".join(paragraphs)
    return markdown


def _flexible_snippet_pattern(snippet: str) -> str:
    snippet = re.sub(r"\s+", " ", snippet.strip())
    pieces = []
    for char in snippet:
        if char.isspace():
            pieces.append(r"\s+")
        elif char == "$":
            pieces.append(r"\\?\$")
        elif char == "\\":
            pieces.append(r"\\?")
        else:
            pieces.append(re.escape(char))
    return "".join(pieces)


def _search_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", text).lower()


def _looks_like_visual_ocr(text: str) -> bool:
    number_tokens = len(re.findall(r"\b\d+(?:\.\d+)?\b", text))
    symbol_tokens = len(re.findall(r"[%$θµ∼□三]|[A-Za-z]\d+|\d+[A-Za-z]", text))
    return number_tokens >= 5 or symbol_tokens >= 4


def _existing_semantic_assets(content_list_path: Path, semantic: dict[str, Any]) -> dict[str, set[int]]:
    existing = {"figures": set(), "tables": set(), "charts": set()}
    if not content_list_path.exists():
        return existing

    content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
    unlabeled_tables = 0
    for item in content_list:
        item_type = item.get("type")
        if item_type not in {"image", "table"}:
            continue
        text = " ".join(_caption_texts(item))
        existing["figures"].update(_collect_numbered_labels(text, r"\bFigure\s+(\d+)[A-Za-z]?\b"))
        existing["tables"].update(_collect_numbered_labels(text, r"\bTable\s+(\d+)[A-Za-z]?\b"))
        existing["charts"].update(_collect_numbered_labels(text, r"\bChart\s+(\d+)[A-Za-z]?\b"))
        if item_type == "table" and not existing["tables"]:
            unlabeled_tables += 1

    if unlabeled_tables:
        for number in semantic.get("tables", [])[:unlabeled_tables]:
            existing["tables"].add(number)
    return existing


def _caption_texts(item: dict[str, Any]) -> list[str]:
    values = []
    for key in ("img_caption", "table_caption", "text"):
        value = item.get(key)
        if isinstance(value, list):
            values.extend(str(part) for part in value)
        elif value:
            values.append(str(value))
    return values


def _crop_label_from_pdf(doc: fitz.Document, label: str, number: int, target: Path) -> dict[str, Any] | None:
    located = _find_label_block(doc, label, number)
    if located is None:
        return None

    page_index, block = located
    page = doc[page_index]
    x0, y0, x1, y1 = block[:4]
    if label == "Figure":
        caption_bottom = _caption_bottom_y(page, block)
        clip = _figure_visual_rect(page.rect, x0, y0, x1, caption_bottom)
    elif label == "Table":
        next_y = _table_bottom_y(page, y0)
        col_x0, col_x1 = _column_bounds(page.rect, x0, x1)
        clip = fitz.Rect(col_x0, max(24, y0 - 8), col_x1, min(page.rect.height - 40, next_y))
    else:
        next_y = _next_label_or_body_y(page, y0, label, number)
        clip = fitz.Rect(45, max(24, y0 - 8), page.rect.width - 45, min(page.rect.height - 40, next_y))

    if clip.width < 20 or clip.height < 20:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=clip, alpha=False)
    pix.save(target)
    return {"page": page_index, "bbox": [clip.x0, clip.y0, clip.x1, clip.y1]}


def _find_label_block(doc: fitz.Document, label: str, number: int) -> tuple[int, tuple] | None:
    pattern = re.compile(rf"\b{re.escape(label)}\s+{number}[A-Za-z]?\.", re.IGNORECASE)
    fallback = None
    for page_index, page in enumerate(doc):
        for block in page.get_text("blocks"):
            text = str(block[4]).replace("\n", " ").strip()
            if re.match(rf"^{re.escape(label)}\s+{number}[A-Za-z]?\.", text, flags=re.IGNORECASE):
                return page_index, block
            if fallback is None and pattern.search(text):
                fallback = (page_index, block)
    return fallback


def _figure_visual_rect(page_rect: fitz.Rect, x0: float, y0: float, x1: float, caption_bottom: float) -> fitz.Rect:
    col_x0, col_x1 = _column_bounds(page_rect, x0, x1)
    return fitz.Rect(col_x0, 28, col_x1, min(page_rect.height - 40, caption_bottom + 6))


def _column_bounds(page_rect: fitz.Rect, x0: float, x1: float) -> tuple[float, float]:
    if x0 < page_rect.width / 2 and x1 < page_rect.width * 0.58:
        return 45, page_rect.width / 2 - 10
    if x0 > page_rect.width * 0.42:
        return page_rect.width / 2 + 10, page_rect.width - 45
    return 45, page_rect.width - 45


def _caption_bottom_y(page: fitz.Page, label_block: tuple) -> float:
    x0, y0, x1, y1 = label_block[:4]
    bottom = y1
    caption_text = str(label_block[4]).replace("\n", " ").strip()
    if _caption_text_complete(caption_text):
        return bottom

    for block in sorted(page.get_text("blocks"), key=lambda item: (item[1], item[0])):
        bx0, by0, bx1, by1, text = block[:5]
        if by0 <= y0 + 1 or by0 > bottom + 18 or by0 > y0 + 96:
            continue
        same_column = min(x1, bx1) - max(x0, bx0) > min(x1 - x0, bx1 - bx0) * 0.35
        if same_column:
            caption_text = f"{caption_text} {str(text).replace(chr(10), ' ').strip()}"
            bottom = max(bottom, by1)
            if _caption_text_complete(caption_text):
                break
    return bottom


def _caption_text_complete(text: str) -> bool:
    stripped = text.strip().replace("\n", " ")
    return bool(re.search(r"[.!?。！？]\s*$", stripped))


def _table_bottom_y(page: fitz.Page, y0: float) -> float:
    bottom = y0 + 80
    for block in sorted(page.get_text("blocks"), key=lambda item: (item[1], item[0])):
        bx0, by0, bx1, by1, text = block[:5]
        if by0 <= y0 + 3:
            continue
        if by0 > y0 + 155:
            break
        stripped = str(text).strip().replace("\n", " ")
        if _looks_like_table_text(stripped):
            bottom = max(bottom, by1)
            continue
        if bottom > y0 + 90 and by0 > bottom + 18:
            break
    return bottom + 8


def _looks_like_table_text(text: str) -> bool:
    number_tokens = len(re.findall(r"[+-]?\d+(?:\.\d+)?", text))
    return "charge state" in text.lower() or "amide bond" in text.lower() or number_tokens >= 4


def _next_label_or_body_y(page: fitz.Page, y0: float, label: str, number: int) -> float:
    candidates = []
    label_pattern = re.compile(r"\b(Figure|Table|Chart)\s+\d+", re.IGNORECASE)
    for block in page.get_text("blocks"):
        bx0, by0, bx1, by1, text = block[:5]
        if by0 <= y0 + 3:
            continue
        if label_pattern.search(str(text)) or by0 > y0 + 58:
            candidates.append(by0 - 6)
    return min(candidates) if candidates else y0 + 72


def _collect_numbered_labels(text: str, pattern: str) -> list[int]:
    return sorted({int(match) for match in re.findall(pattern, text, flags=re.IGNORECASE)})


def _visual_paths_by_page(content_list_path: Path | None) -> dict[int, list[str]]:
    by_page: dict[int, list[str]] = {}
    if not content_list_path or not content_list_path.exists():
        return by_page
    content_list = json.loads(content_list_path.read_text(encoding="utf-8"))
    for item in content_list:
        if item.get("type") not in {"image", "table"} or not item.get("img_path"):
            continue
        by_page.setdefault(int(item.get("page_idx", 0)), []).append(str(item["img_path"]))
    return by_page


def _normalize_block(block: dict[str, Any], image_path: str | None = None, page_idx: int = 0, block_index: int = 0) -> dict[str, Any]:
    bbox = [float(value) for value in block.get("bbox", [0, 0, 0, 0])]
    normalized = {
        "id": _block_id(page_idx, block_index),
        "type": block.get("type", "text"),
        "bbox": bbox,
        "text": _block_text(block),
    }
    if image_path:
        normalized["image_path"] = image_path
    return normalized


def _block_id(page_idx: int, block_key: int | str) -> str:
    return f"p{page_idx}-{block_key}"


def _block_text(block: dict[str, Any]) -> str:
    if block.get("text"):
        return str(block["text"]).strip()

    lines = []
    for line in block.get("lines", []):
        spans = line.get("spans", [])
        content = " ".join(str(span.get("content", "")).strip() for span in spans if span.get("content")).strip()
        if content:
            lines.append(content)
    return "\n".join(lines).strip()


def _clean_block_text(block: dict[str, Any]) -> str:
    lines = _extract_lines(block)
    if not lines:
        return ""

    block_type = str(block.get("type", "text")).lower()
    text_lines = [line["text"] for line in lines if line["text"]]
    if not text_lines:
        return ""

    if block_type == "title":
        level = 1 if len(" ".join(text_lines)) > 48 else 2
        return f"{'#' * level} {' '.join(text_lines)}"

    if block_type in {"table", "interline_equation", "image", "image_body", "table_body"}:
        return "\n".join(text_lines)

    paragraphs: list[str] = []
    current = text_lines[0]
    previous = lines[0]
    for line in lines[1:]:
        if _should_start_new_paragraph(previous, line, current, block_type):
            paragraphs.append(current.strip())
            current = line["text"]
        else:
            current = _join_line(current, line["text"])
        previous = line
    paragraphs.append(current.strip())
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def _clean_markdown_paragraph(lines: list[str]) -> str:
    if not lines:
        return ""
    current = lines[0]
    for line in lines[1:]:
        if _looks_like_list_item(line) or _looks_like_caption_or_axis_text(current, line):
            current += "\n" + line
        else:
            current = _join_line(current, line)
    return current


def _is_markdown_structural_line(line: str) -> bool:
    if line.startswith("#"):
        return True
    if line.startswith("![](") or line.startswith("!["):
        return True
    if line.startswith("|") or re.match(r"^[-:| ]{3,}$", line):
        return True
    if line.startswith("```"):
        return True
    if _looks_like_list_item(line):
        return True
    return False


def _is_noise_markdown_line(line: str) -> bool:
    normalized = line.strip().strip("#").strip()
    if not normalized:
        return True
    return normalized.lower() in {
        "read",
        "authors",
        "notes",
        "access",
        "metrics & more",
        "article recommendations",
        "supporting information",
    }


def _looks_like_caption_or_axis_text(left: str, right: str) -> bool:
    combined = f"{left} {right}"
    number_tokens = len(re.findall(r"\b\d+(?:\.\d+)?\b", combined))
    short_symbol_tokens = len(re.findall(r"(?<![A-Za-z])[A-Za-z]\d+|\d+[A-Za-z]|[%$θµ∼□三]+", combined))
    return number_tokens >= 8 or short_symbol_tokens >= 6


def _extract_lines(block: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    for line in block.get("lines", []):
        spans = line.get("spans", [])
        text = " ".join(str(span.get("content", "")).strip() for span in spans if span.get("content")).strip()
        text = _normalize_spacing(text)
        if not text:
            continue
        bbox = [float(value) for value in line.get("bbox", block.get("bbox", [0, 0, 0, 0]))]
        lines.append({"text": text, "bbox": bbox})

    if lines:
        return lines

    text = _normalize_spacing(str(block.get("text", "")).strip())
    if not text:
        return []
    bbox = [float(value) for value in block.get("bbox", [0, 0, 0, 0])]
    return [{"text": text, "bbox": bbox}]


def _should_start_new_paragraph(previous: dict[str, Any], line: dict[str, Any], current: str, block_type: str) -> bool:
    if block_type in {"list", "index"}:
        return True
    if _looks_like_list_item(line["text"]):
        return True
    if _looks_like_list_item(previous["text"]) and not _ends_with_sentence_punctuation(previous["text"]):
        return False

    prev_box = previous["bbox"]
    box = line["bbox"]
    prev_height = max(prev_box[3] - prev_box[1], 1.0)
    vertical_gap = box[1] - prev_box[3]
    left_shift = abs(box[0] - prev_box[0])
    current_width = max(prev_box[2] - prev_box[0], 1.0)

    if vertical_gap > prev_height * 1.15:
        return True
    if left_shift > current_width * 0.16 and _ends_with_sentence_punctuation(current):
        return True
    return False


def _join_line(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-") and _is_ascii_word(left[-2:-1]) and _is_ascii_word(right[:1]):
        return left[:-1] + right
    if _should_join_without_space(left[-1], right[0]):
        return left + right
    return f"{left} {right}"


def _normalize_spacing(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?%)\]}，。；：！？、）】])", r"\1", text)
    text = re.sub(r"([([{（【])\s+", r"\1", text)
    return text.strip()


def _should_join_without_space(left: str, right: str) -> bool:
    if _is_cjk(left) or _is_cjk(right):
        return True
    if right in ",.;:!?%)]]}":
        return True
    if left in "([{":
        return True
    return False


def _ends_with_sentence_punctuation(text: str) -> bool:
    return bool(re.search(r"[.!?。！？；;:：]([\"')\]}）】]*)$", text.strip()))


def _looks_like_list_item(text: str) -> bool:
    return bool(re.match(r"^(\(?\d+[\).、]|[-*•]|[A-Za-z][\).])\s+", text.strip()))


def _is_ascii_word(char: str) -> bool:
    return bool(char and re.match(r"[A-Za-z0-9]", char))


def _is_cjk(char: str) -> bool:
    return bool(char and "\u4e00" <= char <= "\u9fff")


def _find_result_root(output_dir: Path, stem: str, method: str) -> Path:
    expected = output_dir / stem / method
    if expected.exists():
        return expected

    candidates = sorted((output_dir / stem).glob("*")) if (output_dir / stem).exists() else []
    for candidate in candidates:
        if candidate.is_dir() and (candidate / f"{stem}_middle.json").exists():
            return candidate
    raise FileNotFoundError(f"MinerU output was not found under {output_dir / stem}")


def _update_document(document_id: str, **fields: Any) -> None:
    if not fields:
        return
    names = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [document_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE documents SET {names} WHERE id = ?", values)
