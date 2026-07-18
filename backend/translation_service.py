from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from .database import BASE_DIR, get_connection


load_dotenv(BASE_DIR / ".env")


TRANSLATION_SYSTEM_PROMPT = """你是一名严谨的英文学术论文翻译与校对专家。
翻译必须完整、准确、自然，优先采用术语表中的固定译法。
不得删减信息，不得改动公式、单位、编号、引用标号、图片路径和 Markdown 结构。
Figure、Table、Chart 的标题和说明需要翻译，但图片本体不翻译。"""


@dataclass(frozen=True)
class TranslationConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float
    max_output_tokens: int
    thinking: str = "disabled"

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


@dataclass(frozen=True)
class MarkdownSegment:
    index: int
    source_text: str
    content: str
    suffix: str
    section_title: str
    translatable: bool


class TranslationCancelled(Exception):
    pass


def get_translation_config() -> TranslationConfig:
    base_url = os.getenv("TRANSLATION_API_BASE_URL", "https://api.deepseek.com").strip()
    model = os.getenv("TRANSLATION_MODEL", "deepseek-v4-flash").strip()
    api_key = os.getenv("TRANSLATION_API_KEY", "").strip()
    thinking = os.getenv("TRANSLATION_THINKING", "disabled").strip().lower()
    try:
        timeout_seconds = float(os.getenv("TRANSLATION_API_TIMEOUT_SECONDS", "180"))
        max_output_tokens = int(os.getenv("TRANSLATION_MAX_OUTPUT_TOKENS", "65536"))
    except ValueError as error:
        raise RuntimeError("翻译模型超时或输出长度配置不是有效数字") from error
    if not base_url:
        raise RuntimeError("缺少 TRANSLATION_API_BASE_URL")
    if not model:
        raise RuntimeError("缺少 TRANSLATION_MODEL")
    if not api_key:
        raise RuntimeError("缺少 TRANSLATION_API_KEY，请在项目根目录 .env 中配置")
    if thinking not in {"", "enabled", "disabled"}:
        raise RuntimeError("TRANSLATION_THINKING 只能是 enabled、disabled 或留空")
    return TranslationConfig(base_url, model, api_key, timeout_seconds, max_output_tokens, thinking)


def get_translation(document_id: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM translations
            WHERE document_id = ? AND target_language = 'zh-CN'
            LIMIT 1
            """,
            (document_id,),
        ).fetchone()
    if row is None:
        return {
            "status": "not_started",
            "target_language": "zh-CN",
            "translated_markdown": "",
            "progress_current": 0,
            "progress_total": 0,
            "error": None,
        }
    data = dict(row)
    return {
        "id": data["id"],
        "document_id": data["document_id"],
        "status": data["status"],
        "target_language": data["target_language"],
        "translated_markdown": data["translated_markdown"] or "",
        "progress_current": data["progress_current"] or 0,
        "progress_total": data["progress_total"] or 0,
        "error": data["error"],
        "review_notes": data["review_notes"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


def prepare_translation(document_id: str) -> dict:
    config = get_translation_config()
    with get_connection() as conn:
        document = conn.execute(
            "SELECT status, markdown, markdown_clean FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise LookupError("Document not found")
        if document["status"] != "done":
            raise ValueError("文档识别完成后才能翻译")
        if not (document["markdown_clean"] or document["markdown"] or "").strip():
            raise ValueError("文档没有可翻译的 Markdown 内容")

        existing = conn.execute(
            "SELECT id, status FROM translations WHERE document_id = ? AND target_language = 'zh-CN'",
            (document_id,),
        ).fetchone()
        if existing and existing["status"] in {"queued", "analyzing", "translating", "reviewing"}:
            return {"translation_id": existing["id"], "already_running": True, "config": config}

        if existing:
            conn.execute("DELETE FROM translations WHERE id = ?", (existing["id"],))
        translation_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO translations (id, document_id, status, target_language)
            VALUES (?, ?, 'queued', 'zh-CN')
            """,
            (translation_id, document_id),
        )
    return {"translation_id": translation_id, "already_running": False, "config": config}


def run_translation(document_id: str, translation_id: str, config: TranslationConfig | None = None) -> None:
    try:
        config = config or get_translation_config()
        source_markdown = _source_markdown(document_id)
        _ensure_translation_active(translation_id)
        _update_translation(translation_id, status="analyzing", error=None)
        analysis = _analyze_document(config, source_markdown)
        _ensure_translation_active(translation_id)
        segments = split_markdown_segments(source_markdown)
        _store_segments(translation_id, segments)
        translatable_total = sum(segment.translatable for segment in segments)
        _update_translation(
            translation_id,
            status="translating",
            document_summary=analysis.get("document_summary", ""),
            section_summaries_json=json.dumps(analysis.get("section_summaries", {}), ensure_ascii=False),
            glossary_json=json.dumps(analysis.get("glossary", []), ensure_ascii=False),
            progress_current=0,
            progress_total=translatable_total,
        )

        recent_pairs: list[tuple[str, str]] = []
        completed = 0
        translated_parts: list[str] = []
        for segment in segments:
            _ensure_translation_active(translation_id)
            if segment.translatable:
                context = {
                    "previous_english": [pair[0] for pair in recent_pairs[-3:]],
                    "previous_chinese": [pair[1] for pair in recent_pairs[-3:]],
                }
                translated = _translate_segment(config, segment, analysis, context)
                recent_pairs.append((segment.content.strip(), translated.strip()))
                completed += 1
            else:
                context = {}
                translated = segment.content
            translated_text = translated + segment.suffix
            translated_parts.append(translated_text)
            _complete_segment(translation_id, segment.index, translated_text, context)
            if segment.translatable:
                _update_translation(translation_id, progress_current=completed)

        draft = "".join(translated_parts)
        _ensure_translation_active(translation_id)
        _update_translation(translation_id, status="reviewing", translated_markdown=draft)
        reviewed, review_notes = _review_translation(config, source_markdown, draft, analysis)
        _ensure_translation_active(translation_id)
        if analysis.get("analysis_note"):
            review_notes = f"{analysis['analysis_note']}；{review_notes}"
        _update_translation(
            translation_id,
            status="done",
            translated_markdown=reviewed,
            review_notes=review_notes,
            error=None,
        )
    except TranslationCancelled:
        return
    except Exception as error:
        _update_translation(translation_id, status="failed", error=str(error)[:2000])


def split_markdown_segments(markdown: str) -> list[MarkdownSegment]:
    chunks = _split_markdown_blocks(markdown)

    segments: list[MarkdownSegment] = []
    current_section = ""
    for index, source_text in enumerate(chunks):
        content = source_text.rstrip("\n")
        suffix = source_text[len(content):]
        heading = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", content)
        if heading:
            current_section = heading.group(1).strip()
        segments.append(
            MarkdownSegment(
                index=index,
                source_text=source_text,
                content=content,
                suffix=suffix,
                section_title=current_section,
                translatable=not _should_preserve_segment(content),
            )
        )
    return segments


def _split_markdown_blocks(markdown: str) -> list[str]:
    lines = markdown.splitlines(keepends=True)
    chunks: list[str] = []
    buffer: list[str] = []
    fence_marker = ""
    in_math_block = False
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        buffer.append(line)

        fence_match = re.match(r"^\s*(```+|~~~+)", line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if not fence_marker:
                fence_marker = marker
            elif fence_marker == marker:
                fence_marker = ""
        elif not fence_marker and stripped == "$$":
            in_math_block = not in_math_block

        if not fence_marker and not in_math_block and not stripped:
            index += 1
            while index < len(lines) and not lines[index].strip():
                buffer.append(lines[index])
                index += 1
            chunks.append("".join(buffer))
            buffer = []
            continue
        index += 1

    if buffer:
        chunks.append("".join(buffer))
    if not lines and markdown:
        chunks.append(markdown)
    return chunks


def _should_preserve_segment(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return True
    if re.fullmatch(r"(?:!\[[^\]]*\]\([^)]+\)\s*)+", stripped):
        return True
    if (stripped.startswith("```") and stripped.endswith("```")) or (
        stripped.startswith("~~~") and stripped.endswith("~~~")
    ):
        return True
    if (stripped.startswith("$$") and stripped.endswith("$$")) or (
        stripped.startswith(r"\[") and stripped.endswith(r"\]")
    ):
        return True
    if re.fullmatch(r"[-*_]{3,}", stripped):
        return True
    return False


def _source_markdown(document_id: str) -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT markdown, markdown_clean FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("文档不存在")
    markdown = row["markdown_clean"] or row["markdown"] or ""
    if not markdown.strip():
        raise RuntimeError("文档没有可翻译内容")
    return markdown


def _analyze_document(config: TranslationConfig, markdown: str) -> dict:
    prompt = f"""请先分析下面的英文学术论文 Markdown，并只返回一个 JSON 对象，不要添加解释。

JSON 结构：
{{
  "document_summary": "用于后续翻译的中文全文摘要",
  "section_summaries": {{"英文小节标题": "中文小节摘要"}},
  "keywords": ["关键词"],
  "glossary": [{{"english": "术语", "chinese": "固定中文译法", "note": "必要备注"}}]
}}

要求：覆盖专业名词、缩写、仪器名、方法名、化学物质、指标与单位；术语译法适合中文学术论文。
全文摘要不超过 300 个汉字，每个章节摘要不超过 100 个汉字，术语表最多 100 项。

论文 Markdown：
{markdown}"""
    content = _call_chat(
        config,
        [
            {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    analysis_note = ""
    try:
        data = _parse_json_object(content)
    except RuntimeError:
        try:
            data = _repair_analysis_json(config, content)
            analysis_note = "全文分析 JSON 已自动修复"
        except Exception:
            data = {
                "document_summary": "",
                "section_summaries": {},
                "keywords": [],
                "glossary": [],
            }
            analysis_note = "全文分析 JSON 无法解析，已使用空术语表继续翻译"
    if not isinstance(data.get("section_summaries", {}), dict):
        data["section_summaries"] = {}
    if not isinstance(data.get("glossary", []), list):
        data["glossary"] = []
    if analysis_note:
        data["analysis_note"] = analysis_note
    return data


def _repair_analysis_json(config: TranslationConfig, malformed: str) -> dict:
    prompt = f"""请把下面内容修复为一个严格、可解析的 JSON 对象，只输出 JSON，不要添加解释。

必须使用以下结构，缺失内容用空字符串、空对象或空数组补齐：
{{
  "document_summary": "中文全文摘要",
  "section_summaries": {{"英文小节标题": "中文小节摘要"}},
  "keywords": ["关键词"],
  "glossary": [{{"english": "术语", "chinese": "固定中文译法", "note": "必要备注"}}]
}}

待修复内容：
{malformed}"""
    repaired = _call_chat(
        config,
        [
            {"role": "system", "content": "你是 JSON 修复工具。必须只返回严格 JSON。"},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
    )
    return _parse_json_object(repaired)


def _translate_segment(
    config: TranslationConfig,
    segment: MarkdownSegment,
    analysis: dict,
    context: dict,
) -> str:
    section_summary = _find_section_summary(analysis.get("section_summaries", {}), segment.section_title)
    prompt = f"""全文摘要：
{analysis.get('document_summary', '')}

当前章节：
{segment.section_title or '未识别章节'}

当前章节摘要：
{section_summary}

术语表：
{json.dumps(analysis.get('glossary', []), ensure_ascii=False)}

最近 3 段英文原文：
{json.dumps(context.get('previous_english', []), ensure_ascii=False)}

最近 3 段中文译文：
{json.dumps(context.get('previous_chinese', []), ensure_ascii=False)}

当前英文 Markdown 段落：
{segment.content}

请翻译当前段落。保持 Markdown 标记、图片引用、公式、单位、编号和引用标号原样；不要添加解释。
只输出当前段落的中文 Markdown。"""
    translated = _strip_markdown_fence(
        _call_chat(
            config,
            [
                {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    ).strip("\r\n")
    if not translated:
        raise RuntimeError(f"第 {segment.index + 1} 段翻译结果为空")
    translated = _restore_edge_whitespace(segment.content, translated)
    structure_errors = _translation_structure_errors(segment.content, translated)
    if structure_errors:
        raise RuntimeError(f"第 {segment.index + 1} 段翻译破坏了 Markdown 结构：{'；'.join(structure_errors)}")
    return translated


def _review_translation(config: TranslationConfig, source: str, draft: str, analysis: dict) -> tuple[str, str]:
    prompt = f"""请对下面的中文论文 Markdown 译稿做最后一轮全文一致性校对。

重点检查：术语一致、无漏译或误译、专有名词准确、公式/单位/引用编号不变、图表编号一致、中文自然连贯、章节标题统一。
禁止删减内容，禁止修改任何图片路径。只输出校对后的完整中文 Markdown，不要解释。

术语表：
{json.dumps(analysis.get('glossary', []), ensure_ascii=False)}

英文原文：
{source}

中文译稿：
{draft}"""
    try:
        reviewed = _strip_markdown_fence(
            _call_chat(
                config,
                [
                    {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
        ).strip("\r\n")
        if len(reviewed) < max(100, int(len(draft.strip()) * 0.6)):
            return draft, "校对结果疑似不完整，已保留逐段译稿"
        structure_errors = _translation_structure_errors(source, reviewed)
        if structure_errors:
            return draft, f"校对结果破坏了 Markdown 结构，已保留逐段译稿：{'；'.join(structure_errors)}"
        return reviewed + ("\n" if draft.endswith("\n") else ""), "全文一致性校对完成"
    except Exception as error:
        return draft, f"全文校对失败，已保留逐段译稿：{str(error)[:500]}"


def _call_chat(
    config: TranslationConfig,
    messages: list[dict],
    response_format: dict | None = None,
) -> str:
    payload: dict = {
        "model": config.model,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": config.max_output_tokens,
    }
    if response_format:
        payload["response_format"] = response_format
    if config.thinking:
        payload["thinking"] = {"type": config.thinking}
    request = Request(
        config.chat_completions_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("模型响应中没有 choices[0].message.content")
            return content
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"模型接口返回 HTTP {error.code}：{body[:800]}")
            if error.code < 500 and error.code != 429:
                break
        except (URLError, socket.timeout, TimeoutError, json.JSONDecodeError, RuntimeError) as error:
            last_error = error
        if attempt < 2:
            time.sleep(2**attempt)
    raise RuntimeError(f"调用翻译模型失败：{last_error}")


def _parse_json_object(content: str) -> dict:
    cleaned = _strip_markdown_fence(content).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("模型未返回有效的论文分析 JSON")
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as error:
            raise RuntimeError("模型返回的论文分析 JSON 无法解析") from error
    if not isinstance(data, dict):
        raise RuntimeError("模型返回的论文分析结果不是 JSON 对象")
    return data


def _strip_markdown_fence(content: str) -> str:
    stripped = content.strip("\r\n")
    match = re.fullmatch(r"```(?:markdown|md|json)?[^\S\r\n]*\r?\n(.*?)\r?\n```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else content


def _find_section_summary(section_summaries: dict, section_title: str) -> str:
    if not section_title:
        return ""
    if section_title in section_summaries:
        return str(section_summaries[section_title])
    normalized = re.sub(r"\W+", "", section_title).lower()
    for title, summary in section_summaries.items():
        if re.sub(r"\W+", "", str(title)).lower() == normalized:
            return str(summary)
    return ""


def _image_paths(markdown: str) -> list[str]:
    return re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)


def _same_image_paths(source: str, translated: str) -> bool:
    return _image_paths(source) == _image_paths(translated)


def _restore_edge_whitespace(source: str, translated: str) -> str:
    leading = re.match(r"^[ \t]*", source).group(0)
    trailing = re.search(r"[ \t]+$", source)
    if leading and not translated.startswith(leading):
        translated = leading + translated.lstrip(" \t")
    if trailing:
        translated = translated.rstrip(" \t") + trailing.group(0)
    return translated


def _translation_structure_errors(source: str, translated: str) -> list[str]:
    checks = (
        ("图片路径或顺序发生变化", _image_paths(source), _image_paths(translated)),
        ("标题层级发生变化", _heading_levels(source), _heading_levels(translated)),
        ("代码块发生变化", _fenced_blocks(source), _fenced_blocks(translated)),
        ("公式发生变化", _math_expressions(source), _math_expressions(translated)),
        ("链接地址发生变化", _link_targets(source), _link_targets(translated)),
        ("数字引用标号发生变化", _numeric_citations(source), _numeric_citations(translated)),
    )
    return [message for message, expected, actual in checks if expected != actual]


def _heading_levels(markdown: str) -> list[int]:
    return [len(marker) for marker in re.findall(r"(?m)^\s*(#{1,6})\s+", markdown)]


def _fenced_blocks(markdown: str) -> list[str]:
    pattern = re.compile(r"(?ms)^\s*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^\s*(?P=fence)\s*$")
    return [match.group(0) for match in pattern.finditer(markdown)]


def _math_expressions(markdown: str) -> list[str]:
    display = re.findall(r"\$\$.*?\$\$|\\\[.*?\\\]", markdown, flags=re.DOTALL)
    inline = re.findall(r"(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)\$(?!\$)", markdown, flags=re.DOTALL)
    return display + inline


def _link_targets(markdown: str) -> list[str]:
    return re.findall(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", markdown)


def _numeric_citations(markdown: str) -> list[str]:
    return re.findall(r"\[(?:\d+(?:\s*[-–,]\s*\d+)*)\]", markdown)


def _store_segments(translation_id: str, segments: list[MarkdownSegment]) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM translation_segments WHERE translation_id = ?", (translation_id,))
        conn.executemany(
            """
            INSERT INTO translation_segments (
                id, translation_id, segment_index, section_title, segment_type,
                source_text, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    uuid.uuid4().hex,
                    translation_id,
                    segment.index,
                    segment.section_title,
                    "text" if segment.translatable else "preserve",
                    segment.source_text,
                    "pending" if segment.translatable else "preserved",
                )
                for segment in segments
            ],
        )


def _complete_segment(translation_id: str, segment_index: int, translated_text: str, context: dict) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE translation_segments
            SET translated_text = ?, context_json = ?, status = 'done', error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE translation_id = ? AND segment_index = ?
            """,
            (translated_text, json.dumps(context, ensure_ascii=False), translation_id, segment_index),
        )


def _update_translation(translation_id: str, **fields) -> None:
    if not fields:
        return
    allowed = {
        "status",
        "document_summary",
        "section_summaries_json",
        "glossary_json",
        "translated_markdown",
        "review_notes",
        "progress_current",
        "progress_total",
        "error",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unsupported translation fields: {sorted(unknown)}")
    assignments = ", ".join(f"{field} = ?" for field in fields)
    values = list(fields.values()) + [translation_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE translations SET {assignments} WHERE id = ?", values)


def _ensure_translation_active(translation_id: str) -> None:
    with get_connection() as conn:
        row = conn.execute("SELECT status FROM translations WHERE id = ?", (translation_id,)).fetchone()
    if row is None or row["status"] not in {"queued", "analyzing", "translating", "reviewing"}:
        raise TranslationCancelled()
