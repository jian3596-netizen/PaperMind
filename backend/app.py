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


app = FastAPI(title="MinerU PDF Recognition API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DeleteBlocksRequest(BaseModel):
    block_ids: list[str]


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

    return {"ok": True, "removed": len(removed_blocks), "document": get_document(document_id)}


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
