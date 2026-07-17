# 论文翻译功能需求

## 目标

在现有 PDF 识别结果基础上，支持将英文论文翻译为中文 Markdown。翻译应尽量保证术语一致、上下文连贯、图表标题准确、公式和单位不被误改。

## 核心思路

不建议一次性翻译全文。推荐采用：

1. 先分析全文结构和主题
2. 提取术语表
3. 按自然段逐段翻译
4. 每段翻译时带最近 3 段上下文
5. 同时带全文摘要或章节摘要
6. 翻译后做全文一致性校对

## 翻译上下文设计

每一段翻译时，模型输入应包含：

- 当前英文段落
- 最近 3 段英文原文
- 最近 3 段中文译文
- 全文摘要
- 当前章节摘要
- 术语表
- 翻译风格规则

这样比只带前文中文译文更稳，因为可以避免前文误译继续污染后续翻译。

## 推荐流程

### 1. 文档结构化

从 MinerU 识别结果中提取：

- 标题
- 作者和机构
- 摘要
- 章节标题
- 正文自然段
- 图表标题
- 公式
- 参考文献

翻译时应优先使用清理后的 Markdown，而不是对照页里的原始 bbox 文本。

### 2. 生成摘要

先让模型基于全文生成：

- 全文摘要
- 各章节摘要
- 论文主题关键词

摘要用于每段翻译的全局上下文，避免模型只看局部段落导致术语或语气漂移。

### 3. 提取术语表

翻译前生成术语表，至少包含：

- 专业名词
- 缩写
- 仪器名
- 方法名
- 化学物质名
- 指标和单位
- 图表中反复出现的标签

术语表格式建议：

| English | 中文译法 | 备注 |
| --- | --- | --- |
| beam-type CID | 束型碰撞诱导解离 | 固定译法 |
| ion trap CID | 离子阱碰撞诱导解离 | 固定译法 |
| charge state | 电荷态 | 固定译法 |

### 4. 分段翻译

以自然段为单位翻译，而不是按页或固定字数切分。

每段请求模型时使用如下上下文：

```text
全文摘要：
{document_summary}

当前章节摘要：
{section_summary}

术语表：
{glossary}

最近 3 段英文原文：
{previous_english_paragraphs}

最近 3 段中文译文：
{previous_chinese_translations}

当前英文段落：
{current_paragraph}

要求：
1. 翻译为准确、自然的中文学术表达
2. 保持术语表译法一致
3. 不改写公式、单位、编号、引用标号
4. 不删除原文信息
5. 图表编号保持一致
```

### 5. 图表和公式处理

图表截图不翻译图片本体，但应翻译图表标题和说明文字。

处理规则：

- `![](images/xxx.png)` 保留原位
- Figure/Table/Chart 标题翻译为中文
- 图中坐标轴、曲线标签等暂不强制翻译，除非后续支持图片标注翻译
- 公式和编号保持原样

示例：

```markdown
![](images/semantic_figure_1.png)

图 1. [中文图注]
```

### 6. 全文校对

逐段翻译完成后，再做一轮全文校对。

校对内容：

- 术语是否一致
- 是否漏译
- 是否误译专有名词
- 是否改动公式、单位、引用编号
- 图表编号是否和原文一致
- 中文是否自然、连贯
- 章节标题是否统一

## 数据存储建议

SQLite 可新增翻译相关字段或表。

建议新增表：

```sql
CREATE TABLE translations (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    status TEXT NOT NULL,
    target_language TEXT NOT NULL DEFAULT 'zh-CN',
    document_summary TEXT,
    section_summaries_json TEXT,
    glossary_json TEXT,
    translated_markdown TEXT,
    review_notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
);
```

如需保存逐段翻译过程，可新增：

```sql
CREATE TABLE translation_segments (
    id TEXT PRIMARY KEY,
    translation_id TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    section_title TEXT,
    source_text TEXT NOT NULL,
    translated_text TEXT,
    context_json TEXT,
    status TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY(translation_id) REFERENCES translations(id) ON DELETE CASCADE
);
```

## 前端功能建议

后续可增加一个“翻译”页签：

- 开始翻译按钮
- 翻译进度
- 术语表查看和编辑
- 原文 / 译文对照
- 最终中文 Markdown 渲染
- 导出中文 Markdown

## 注意事项

- 不要无限累积上下文，只带最近 3 段即可
- 必须同时带英文原文和中文译文上下文
- 术语表优先级应高于模型自由发挥
- 摘要用于全局语义稳定，不替代原文
- 翻译完成后必须做全文一致性校对
