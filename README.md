# PaperMind

PaperMind is a local PDF recognition workspace built on top of MinerU (`magic-pdf`). It provides a FastAPI backend, SQLite storage, and a browser UI for uploading academic PDFs, reviewing OCR/layout output, editing recognized blocks, and rendering cleaned Markdown with extracted figures.

## Features

- Upload PDF files and run MinerU recognition locally.
- Store document history and recognition results in SQLite.
- Show a side-by-side comparison of original PDF pages and positioned recognized content.
- Render cleaned Markdown with images inserted in place.
- Supplement missing figure/table/chart crops using PDF text coordinates.
- Remove visual-region OCR leftovers from Markdown.
- Select one or more recognition blocks in the comparison view and delete them.
- Undo the previous edit or reset a document to the post-recognition state.
- Delete history records and their uploaded/result files.

## Requirements

- Python 3.12+
- MinerU model files configured for `magic-pdf`
- macOS/Linux recommended

The local MinerU config is expected at `~/magic-pdf.json`. This project has been tested with `magic-pdf==1.3.12`.

## Install

```bash
uv sync
```

If you already have the virtual environment:

```bash
source .venv/bin/activate
```

## Run

```bash
./.venv/bin/python main.py
```

Then open:

```text
http://127.0.0.1:8000
```

## Storage

Runtime files are stored under `storage/`:

- `storage/mineru.sqlite3`
- `storage/uploads/`
- `storage/results/`

These files are ignored by Git.

## API Overview

```text
GET    /api/documents
POST   /api/documents?method=txt
GET    /api/documents/{id}
DELETE /api/documents/{id}
GET    /api/documents/{id}/pdf
GET    /api/documents/{id}/pages/{page}.png
GET    /api/documents/{id}/assets/{asset_path}

POST   /api/documents/{id}/edit/delete-blocks
POST   /api/documents/{id}/edit/undo
POST   /api/documents/{id}/edit/reset
```

## Notes

- `txt` mode is fastest for text-based PDFs.
- `ocr` mode is useful for scanned PDFs.
- The comparison view preserves original layout and may show raw line breaks.
- The Markdown view renders the cleaned version intended for reading/export.
- Translation planning notes are in [`translation_requirements.md`](translation_requirements.md).
