from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

import fitz
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .database import BASE_DIR, RESULT_DIR, UPLOAD_DIR, get_connection, init_db
from .mineru_service import clean_markdown, run_recognition
from .translation_service import get_translation, prepare_translation, run_translation


app = FastAPI(title="MinerU PDF Recognition API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DeleteBlocksRequest(BaseModel):
    block_ids: list[str]


class ResizeVisualRequest(BaseModel):
    block_id: str
    bbox: list[float]


class AddVisualRequest(BaseModel):
    page_index: int
    bbox: list[float]


@app.on_event("startup")
def startup() -> None:
    init_db()
    _backfill_original_snapshots()


@app.post("/api/documents")
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    method: str = "txt",
) -> dict:
    if method not in {"txt", "ocr", "auto"}:
        raise HTTPException(status_code=400, detail="method must be txt, ocr, or auto")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    document_id = uuid.uuid4().hex
    stored_name = f"{document_id}.pdf"
    pdf_path = UPLOAD_DIR / stored_name
    output_dir = RESULT_DIR / document_id
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    with pdf_path.open("wb") as target:
        shutil.copyfileobj(file.file, target)

    page_count = _page_count(pdf_path)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO documents (
                id, original_name, stored_name, pdf_path, output_dir, status, method, pages
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                file.filename,
                stored_name,
                str(pdf_path),
                str(output_dir),
                "queued",
                method,
                page_count,
            ),
        )

    background_tasks.add_task(run_recognition, document_id)
    return get_document_summary(document_id)


@app.get("/api/documents")
def list_documents() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, original_name, status, method, pages, duration_seconds, error, created_at, updated_at
            FROM documents
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/documents/{document_id}")
def get_document(document_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    data = dict(row)
    blocks = _ensure_block_ids(_loads_json(data["blocks_json"]))
    return {
        "id": data["id"],
        "original_name": data["original_name"],
        "status": data["status"],
        "method": data["method"],
        "pages": data["pages"],
        "markdown": data["markdown"] or "",
        "markdown_clean": data.get("markdown_clean") or data["markdown"] or "",
        "blocks": blocks,
        "assets": _loads_json(data.get("assets_json")),
        "error": data["error"],
        "duration_seconds": data["duration_seconds"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


@app.get("/api/documents/{document_id}/translation")
def get_document_translation(document_id: str) -> dict:
    _get_row(document_id)
    return get_translation(document_id)


@app.post("/api/documents/{document_id}/translation")
def start_document_translation(document_id: str, background_tasks: BackgroundTasks) -> dict:
    try:
        prepared = prepare_translation(document_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    if not prepared["already_running"]:
        background_tasks.add_task(
            run_translation,
            document_id,
            prepared["translation_id"],
            prepared["config"],
        )
    return get_translation(document_id)


@app.post("/api/documents/{document_id}/edit/delete-blocks")
def delete_blocks(document_id: str, payload: DeleteBlocksRequest) -> dict:
    if not payload.block_ids:
        raise HTTPException(status_code=400, detail="No block ids provided")

    row = _get_row(document_id)
    blocks = _ensure_block_ids(_loads_json(row["blocks_json"]))
    markdown = row["markdown"] or ""
    removed_blocks = []
    block_ids = set(payload.block_ids)

    for page in blocks.get("pages", []):
        kept = []
        for block in page.get("blocks", []):
            if block.get("id") in block_ids:
                removed_blocks.append(block)
            else:
                kept.append(block)
        page["blocks"] = kept

    if not removed_blocks:
        raise HTTPException(status_code=404, detail="No matching blocks found")

    _push_edit_history(row, "delete-blocks")
    for block in removed_blocks:
        markdown = _remove_block_from_markdown(markdown, block)
    markdown_clean = clean_markdown(markdown)

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE documents
            SET blocks_json = ?, markdown = ?, markdown_clean = ?
            WHERE id = ?
            """,
            (json.dumps(blocks, ensure_ascii=False), markdown, markdown_clean, document_id),
        )
    _invalidate_translation(document_id)

    return {"ok": True, "removed": len(removed_blocks), "document": get_document(document_id)}


@app.post("/api/documents/{document_id}/edit/resize-visual")
def resize_visual_block(document_id: str, payload: ResizeVisualRequest) -> dict:
    if len(payload.bbox) != 4:
        raise HTTPException(status_code=400, detail="bbox must have 4 numbers")

    row = _get_row(document_id)
    blocks = _ensure_block_ids(_loads_json(row["blocks_json"]))
    target_page = None
    target_block = None
    for page in blocks.get("pages", []):
        for block in page.get("blocks", []):
            if block.get("id") == payload.block_id:
                target_page = page
                target_block = block
                break
        if target_block:
            break

    if target_page is None or target_block is None:
        raise HTTPException(status_code=404, detail="Block not found")
    if not target_block.get("image_path"):
        raise HTTPException(status_code=400, detail="Only visual blocks can be resized")

    bbox = _clamp_bbox(payload.bbox, float(target_page["width"]), float(target_page["height"]))
    if bbox[2] - bbox[0] < 8 or bbox[3] - bbox[1] < 8:
        raise HTTPException(status_code=400, detail="bbox is too small")

    _push_edit_history(row, "resize-visual")
    image_path = _crop_pdf_region(row, int(target_page["page"]), bbox, str(target_block["id"]))
    markdown = _replace_image_path(row["markdown"] or "", str(target_block.get("image_path") or ""), image_path)
    target_block["bbox"] = bbox
    target_block["image_path"] = image_path

    kept_blocks = []
    removed_text_blocks = []
    for block in target_page.get("blocks", []):
        if block is target_block:
            kept_blocks.append(block)
            continue
        if not block.get("image_path") and _bbox_overlaps_region(block.get("bbox", []), bbox):
            removed_text_blocks.append(block)
            continue
        kept_blocks.append(block)
    target_page["blocks"] = kept_blocks

    for block in removed_text_blocks:
        markdown = _remove_block_from_markdown(markdown, block)
    markdown_clean = clean_markdown(markdown)

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE documents
            SET blocks_json = ?, markdown = ?, markdown_clean = ?
            WHERE id = ?
            """,
            (json.dumps(blocks, ensure_ascii=False), markdown, markdown_clean, document_id),
        )
    _invalidate_translation(document_id)

    return {"ok": True, "document": get_document(document_id)}


@app.post("/api/documents/{document_id}/edit/add-visual")
def add_visual_block(document_id: str, payload: AddVisualRequest) -> dict:
    if len(payload.bbox) != 4:
        raise HTTPException(status_code=400, detail="bbox must have 4 numbers")

    row = _get_row(document_id)
    blocks = _ensure_block_ids(_loads_json(row["blocks_json"]))
    target_page = next(
        (page for page in blocks.get("pages", []) if int(page.get("page", -1)) == payload.page_index),
        None,
    )
    if target_page is None:
        raise HTTPException(status_code=404, detail="Page not found")

    bbox = _clamp_bbox(payload.bbox, float(target_page["width"]), float(target_page["height"]))
    if bbox[2] - bbox[0] < 8 or bbox[3] - bbox[1] < 8:
        raise HTTPException(status_code=400, detail="bbox is too small")

    for block in target_page.get("blocks", []):
        if block.get("image_path") and _visual_regions_duplicate(block.get("bbox", []), bbox):
            raise HTTPException(status_code=409, detail="该区域已经存在截图")

    _push_edit_history(row, "add-visual")
    block_id = f"p{payload.page_index}-manual-visual-{uuid.uuid4().hex[:10]}"
    image_path = _crop_pdf_region(row, payload.page_index, bbox, block_id)
    markdown = row["markdown"] or ""
    page_blocks = list(target_page.get("blocks", []))
    markdown = _insert_image_near_region(markdown, image_path, page_blocks, bbox)

    kept_blocks = []
    removed_text_blocks = []
    for block in page_blocks:
        if not block.get("image_path") and _bbox_overlaps_region(block.get("bbox", []), bbox):
            removed_text_blocks.append(block)
            continue
        kept_blocks.append(block)
    for block in removed_text_blocks:
        markdown = _remove_block_from_markdown(markdown, block)

    new_block = {
        "id": block_id,
        "type": "image",
        "bbox": bbox,
        "text": "",
        "image_path": image_path,
    }
    kept_blocks.append(new_block)
    kept_blocks.sort(key=_block_position)
    target_page["blocks"] = kept_blocks
    markdown_clean = clean_markdown(markdown)

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE documents
            SET blocks_json = ?, markdown = ?, markdown_clean = ?
            WHERE id = ?
            """,
            (json.dumps(blocks, ensure_ascii=False), markdown, markdown_clean, document_id),
        )
    _invalidate_translation(document_id)

    return {
        "ok": True,
        "block_id": block_id,
        "removed_text_blocks": len(removed_text_blocks),
        "document": get_document(document_id),
    }


@app.post("/api/documents/{document_id}/edit/undo")
def undo_edit(document_id: str) -> dict:
    row = _get_row(document_id)
    with get_connection() as conn:
        history = conn.execute(
            """
            SELECT * FROM document_edit_history
            WHERE document_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
        if history is None:
            return {"ok": False, "reason": "nothing_to_undo", "document": get_document(document_id)}
        conn.execute(
            """
            UPDATE documents
            SET blocks_json = ?, markdown = ?, markdown_clean = ?
            WHERE id = ?
            """,
            (history["before_blocks_json"], history["before_markdown"], history["before_markdown_clean"], row["id"]),
        )
        conn.execute("DELETE FROM document_edit_history WHERE id = ?", (history["id"],))
    _invalidate_translation(document_id)
    return {"ok": True, "document": get_document(document_id)}


@app.post("/api/documents/{document_id}/edit/reset")
def reset_edits(document_id: str) -> dict:
    row = _get_row(document_id)
    blocks_original = row["blocks_json_original"] or row["blocks_json"] or json.dumps({"pages": []})
    markdown_original = row["markdown_original"] or row["markdown"] or ""
    markdown_clean_original = row["markdown_clean_original"] or row["markdown_clean"] or clean_markdown(markdown_original)
    _push_edit_history(row, "reset")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE documents
            SET blocks_json = ?, markdown = ?, markdown_clean = ?
            WHERE id = ?
            """,
            (blocks_original, markdown_original, markdown_clean_original, document_id),
        )
    _invalidate_translation(document_id)
    return {"ok": True, "document": get_document(document_id)}


@app.get("/api/documents/{document_id}/pdf")
def get_pdf(document_id: str) -> FileResponse:
    row = _get_row(document_id)
    return FileResponse(row["pdf_path"], media_type="application/pdf", filename=row["original_name"])


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str) -> dict:
    row = _get_row(document_id)
    pdf_path = Path(row["pdf_path"])
    output_dir = Path(row["output_dir"])
    with get_connection() as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    if pdf_path.exists():
        pdf_path.unlink()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    return {"ok": True}


@app.get("/api/documents/{document_id}/assets/{asset_path:path}")
def get_asset(document_id: str, asset_path: str) -> FileResponse:
    row = _get_row(document_id)
    output_dir = Path(row["output_dir"]).resolve()
    requested = (output_dir / Path(asset_path)).resolve()
    if not requested.is_file():
        matches = list(output_dir.glob(f"*/*/{asset_path}"))
        requested = matches[0].resolve() if matches else requested
    if not requested.is_file() or output_dir not in requested.parents:
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(requested)


@app.get("/api/documents/{document_id}/pages/{page_index}.png")
def get_page_image(document_id: str, page_index: int) -> FileResponse:
    row = _get_row(document_id)
    if page_index < 0:
        raise HTTPException(status_code=404, detail="Page not found")

    image_dir = RESULT_DIR / document_id / "page_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / f"{page_index}.png"
    if not image_path.exists():
        doc = fitz.open(row["pdf_path"])
        if page_index >= len(doc):
            raise HTTPException(status_code=404, detail="Page not found")
        page = doc[page_index]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        pix.save(image_path)
    return FileResponse(image_path, media_type="image/png")


def get_document_summary(document_id: str) -> dict:
    row = _get_row(document_id)
    keys = ["id", "original_name", "status", "method", "pages", "duration_seconds", "error", "created_at", "updated_at"]
    return {key: row[key] for key in keys}


def _get_row(document_id: str):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return row


def _page_count(pdf_path: Path) -> int:
    try:
        return len(fitz.open(pdf_path))
    except Exception:
        return 0


def _loads_json(value: str | None) -> dict:
    if not value:
        return {"pages": []}
    import json

    return json.loads(value)


def _backfill_original_snapshots() -> None:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, blocks_json, markdown, markdown_clean, blocks_json_original, markdown_original, markdown_clean_original
            FROM documents
            """
        ).fetchall()
        for row in rows:
            blocks = _ensure_block_ids(_loads_json(row["blocks_json"]))
            blocks_json = json.dumps(blocks, ensure_ascii=False)
            blocks_original = row["blocks_json_original"] or blocks_json
            markdown_original = row["markdown_original"] or row["markdown"] or ""
            markdown_clean_original = row["markdown_clean_original"] or row["markdown_clean"] or clean_markdown(markdown_original)
            conn.execute(
                """
                UPDATE documents
                SET blocks_json = ?, blocks_json_original = ?, markdown_original = ?, markdown_clean_original = ?
                WHERE id = ?
                """,
                (blocks_json, blocks_original, markdown_original, markdown_clean_original, row["id"]),
            )


def _invalidate_translation(document_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE translations
            SET status = 'stale', error = '原文已修改，请重新翻译'
            WHERE document_id = ?
            """,
            (document_id,),
        )


def _ensure_block_ids(blocks: dict) -> dict:
    for page in blocks.get("pages", []):
        page_idx = int(page.get("page", 0))
        for index, block in enumerate(page.get("blocks", [])):
            if not block.get("id"):
                block["id"] = f"p{page_idx}-{index}"
    return blocks


def _push_edit_history(row, action_type: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO document_edit_history (
                id, document_id, action_type, before_blocks_json, before_markdown, before_markdown_clean
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                row["id"],
                action_type,
                row["blocks_json"] or json.dumps({"pages": []}),
                row["markdown"] or "",
                row["markdown_clean"] or clean_markdown(row["markdown"] or ""),
            ),
        )


def _clamp_bbox(bbox: list[float], page_width: float, page_height: float) -> list[float]:
    x0, y0, x1, y1 = [float(value) for value in bbox]
    x0 = min(max(x0, 0), page_width)
    y0 = min(max(y0, 0), page_height)
    x1 = min(max(x1, 0), page_width)
    y1 = min(max(y1, 0), page_height)
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _crop_pdf_region(row, page_index: int, bbox: list[float], block_id: str) -> str:
    doc = fitz.open(row["pdf_path"])
    if page_index < 0 or page_index >= len(doc):
        raise HTTPException(status_code=404, detail="Page not found")

    output_dir = Path(row["output_dir"])
    crop_dir = output_dir / "adjusted_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    safe_block_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", block_id)
    target = crop_dir / f"{safe_block_id}_{uuid.uuid4().hex[:8]}.png"
    page = doc[page_index]
    clip = fitz.Rect(*bbox)
    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=clip, alpha=False)
    pix.save(target)
    return str(target.relative_to(output_dir))


def _bbox_overlaps_region(block_bbox: list[float], region_bbox: list[float]) -> bool:
    if len(block_bbox) != 4 or len(region_bbox) != 4:
        return False
    bx0, by0, bx1, by1 = [float(value) for value in block_bbox]
    rx0, ry0, rx1, ry1 = [float(value) for value in region_bbox]
    block_area = max((bx1 - bx0) * (by1 - by0), 1.0)
    ix0, iy0 = max(bx0, rx0), max(by0, ry0)
    ix1, iy1 = min(bx1, rx1), min(by1, ry1)
    overlap = max(ix1 - ix0, 0) * max(iy1 - iy0, 0)
    center_inside = rx0 <= (bx0 + bx1) / 2 <= rx1 and ry0 <= (by0 + by1) / 2 <= ry1
    return overlap / block_area > 0.35 or center_inside


def _visual_regions_duplicate(block_bbox: list[float], region_bbox: list[float]) -> bool:
    if len(block_bbox) != 4 or len(region_bbox) != 4:
        return False
    bx0, by0, bx1, by1 = [float(value) for value in block_bbox]
    rx0, ry0, rx1, ry1 = [float(value) for value in region_bbox]
    ix0, iy0 = max(bx0, rx0), max(by0, ry0)
    ix1, iy1 = min(bx1, rx1), min(by1, ry1)
    overlap = max(ix1 - ix0, 0) * max(iy1 - iy0, 0)
    smaller_area = max(min((bx1 - bx0) * (by1 - by0), (rx1 - rx0) * (ry1 - ry0)), 1.0)
    return overlap / smaller_area > 0.75


def _block_position(block: dict) -> tuple[float, float]:
    bbox = block.get("bbox", [])
    if len(bbox) != 4:
        return (0.0, 0.0)
    return (float(bbox[1]), float(bbox[0]))


def _insert_image_near_region(markdown: str, image_path: str, page_blocks: list[dict], bbox: list[float]) -> str:
    image_markdown = f"![]({image_path})"
    candidates = []
    for block in page_blocks:
        text = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
        block_bbox = block.get("bbox", [])
        if block.get("image_path") or not text or len(block_bbox) != 4:
            continue
        if _bbox_overlaps_region(block_bbox, bbox):
            continue
        x0, y0, _, _ = [float(value) for value in block_bbox]
        if y0 >= bbox[3] - 2:
            candidates.append((y0, x0, text))

    for _, _, text in sorted(candidates):
        match = re.search(_flexible_text_pattern(text), markdown, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        paragraph_start = markdown.rfind("\n\n", 0, match.start())
        insert_at = 0 if paragraph_start < 0 else paragraph_start + 2
        return markdown[:insert_at] + image_markdown + "\n\n" + markdown[insert_at:]

    if not markdown.strip():
        return image_markdown
    return markdown.rstrip() + "\n\n" + image_markdown


def _replace_image_path(markdown: str, old_path: str, new_path: str) -> str:
    if not old_path:
        return markdown
    return re.sub(
        rf"(!\[[^\]]*\]\(){re.escape(old_path)}(\))",
        rf"\g<1>{new_path}\2",
        markdown,
    )


def _remove_block_from_markdown(markdown: str, block: dict) -> str:
    image_path = block.get("image_path")
    if image_path:
        markdown = re.sub(
            rf"(?m)^\s*!\[[^\]]*\]\({re.escape(str(image_path))}\)\s*(?:  )?\s*$\n?",
            "",
            markdown,
        )

    text = str(block.get("text") or "").strip()
    if text:
        markdown = _remove_text_snippet(markdown, text)

    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


def _remove_text_snippet(markdown: str, text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < 8:
        short_pattern = rf"(?im)^\s*#*\s*{re.escape(normalized)}\s*$\n?"
        updated = re.sub(short_pattern, "", markdown)
        return updated
    pattern = _flexible_text_pattern(normalized)
    updated = re.sub(pattern, "", markdown, count=1, flags=re.IGNORECASE | re.DOTALL)
    if updated != markdown:
        return updated

    paragraphs = re.split(r"(\n\s*\n)", markdown)
    needle = _search_key(normalized)
    if len(needle) < 16:
        return markdown
    for index, paragraph in enumerate(paragraphs):
        if index % 2 == 1:
            continue
        haystack = _search_key(paragraph)
        if needle[:80] in haystack:
            paragraphs[index] = ""
            return "".join(paragraphs)
    return markdown


def _flexible_text_pattern(text: str) -> str:
    return r"\s+".join(re.escape(part) for part in re.split(r"\s+", text) if part)


def _search_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", text).lower()


frontend_dir = BASE_DIR / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
