from __future__ import annotations

import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
RESULT_DIR = STORAGE_DIR / "results"
DB_PATH = STORAGE_DIR / "mineru.sqlite3"


def ensure_storage() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    ensure_storage()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                pdf_path TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                status TEXT NOT NULL,
                method TEXT NOT NULL,
                pages INTEGER,
                markdown TEXT,
                markdown_clean TEXT,
                blocks_json TEXT,
                assets_json TEXT,
                markdown_original TEXT,
                markdown_clean_original TEXT,
                blocks_json_original TEXT,
                error TEXT,
                duration_seconds REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _add_column_if_missing(conn, "documents", "markdown_clean", "TEXT")
        _add_column_if_missing(conn, "documents", "assets_json", "TEXT")
        _add_column_if_missing(conn, "documents", "markdown_original", "TEXT")
        _add_column_if_missing(conn, "documents", "markdown_clean_original", "TEXT")
        _add_column_if_missing(conn, "documents", "blocks_json_original", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_edit_history (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                before_blocks_json TEXT NOT NULL,
                before_markdown TEXT NOT NULL,
                before_markdown_clean TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS translations (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                status TEXT NOT NULL,
                target_language TEXT NOT NULL DEFAULT 'zh-CN',
                document_summary TEXT,
                section_summaries_json TEXT,
                glossary_json TEXT,
                translated_markdown TEXT,
                review_notes TEXT,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, target_language)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS translation_segments (
                id TEXT PRIMARY KEY,
                translation_id TEXT NOT NULL,
                segment_index INTEGER NOT NULL,
                section_title TEXT,
                segment_type TEXT NOT NULL,
                source_text TEXT NOT NULL,
                translated_text TEXT,
                context_json TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(translation_id) REFERENCES translations(id) ON DELETE CASCADE,
                UNIQUE(translation_id, segment_index)
            )
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS documents_updated_at
            AFTER UPDATE ON documents
            FOR EACH ROW
            BEGIN
                UPDATE documents SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS translations_updated_at
            AFTER UPDATE ON translations
            FOR EACH ROW
            BEGIN
                UPDATE translations SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END
            """
        )
        conn.execute(
            """
            UPDATE translations
            SET status = 'failed', error = '服务重启导致翻译任务中断，请重试'
            WHERE status IN ('queued', 'analyzing', 'translating', 'reviewing')
            """
        )


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
