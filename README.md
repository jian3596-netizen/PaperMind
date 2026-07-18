# PaperMind

PaperMind is a local PDF recognition workspace built on top of MinerU (`magic-pdf`). It provides a FastAPI backend, SQLite storage, and a browser UI for uploading academic PDFs, reviewing OCR/layout output, editing recognized blocks, and rendering cleaned Markdown with extracted figures.

## Features

- Upload PDF files and run MinerU recognition locally.
- Store document history and recognition results in SQLite.
- Show a side-by-side comparison of original PDF pages and positioned recognized content.
- Render cleaned Markdown with images inserted in place.
- Supplement missing figure/table/chart crops using PDF text coordinates.
- Add missing screenshots manually by dragging a region on the source PDF page.
- Remove visual-region OCR leftovers from Markdown.
- Select one or more recognition blocks in the comparison view and delete them.
- Undo the previous edit or reset a document to the post-recognition state.
- Translate cleaned English Markdown into Chinese with DeepSeek through its OpenAI-compatible protocol.
- View the original English Markdown and the persisted Chinese translation side by side.
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

Copy the model configuration and fill in your API key:

```bash
cp .env.example .env
```

The default translation provider is DeepSeek V4 Flash:

```dotenv
TRANSLATION_API_BASE_URL=https://api.deepseek.com
TRANSLATION_MODEL=deepseek-v4-flash
TRANSLATION_API_KEY=your-api-key
TRANSLATION_THINKING=disabled
TRANSLATION_API_TIMEOUT_SECONDS=180
TRANSLATION_MAX_OUTPUT_TOKENS=65536
```

The application calls DeepSeek's OpenAI-compatible Chat Completions endpoint; it does not use an OpenAI model. `TRANSLATION_THINKING` may be `disabled`, `enabled`, or empty when the endpoint does not support this DeepSeek field. Model credentials stay in the untracked `.env` file.

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
GET    /api/documents/{id}/translation
POST   /api/documents/{id}/translation
```

## Notes

- `txt` mode is fastest for text-based PDFs.
- `ocr` mode is useful for scanned PDFs.
- The comparison view preserves original layout and may show raw line breaks.
- The Markdown view renders the cleaned version intended for reading/export.
- Translation analyzes the document first, applies a glossary while translating natural paragraphs with the previous three bilingual paragraphs as context, and performs a final consistency review.
- Translation planning notes are in [`translation_requirements.md`](translation_requirements.md).
