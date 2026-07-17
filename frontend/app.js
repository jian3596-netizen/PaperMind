const historyList = document.querySelector("#history-list");
const uploadForm = document.querySelector("#upload-form");
const fileInput = document.querySelector("#pdf-file");
const fileName = document.querySelector("#file-name");
const refreshButton = document.querySelector("#refresh-button");
const docTitle = document.querySelector("#doc-title");
const docMeta = document.querySelector("#doc-meta");
const emptyState = document.querySelector("#empty-state");
const compareView = document.querySelector("#compare-view");
const markdownView = document.querySelector("#markdown-view");
const markdownContent = document.querySelector("#markdown-content");
const tabButtons = document.querySelectorAll(".view-tabs button");
const editToolbar = document.querySelector("#edit-toolbar");
const selectionCount = document.querySelector("#selection-count");
const deleteSelectedButton = document.querySelector("#delete-selected");
const undoEditButton = document.querySelector("#undo-edit");
const resetEditButton = document.querySelector("#reset-edit");

let selectedId = null;
let selectedDocument = null;
let pollTimer = null;
let currentView = "compare";
let selectedBlockIds = new Set();

fileInput.addEventListener("change", () => {
  fileName.textContent = fileInput.files[0]?.name || "未选择文件";
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  const button = uploadForm.querySelector("button");
  button.disabled = true;
  button.textContent = "识别中...";

  const formData = new FormData();
  formData.append("file", file);
  const method = new FormData(uploadForm).get("method") || "txt";

  try {
    const response = await fetch(`/api/documents?method=${encodeURIComponent(method)}`, {
      method: "POST",
      body: formData,
    });
    if (!response.ok) throw new Error(await response.text());
    const item = await response.json();
    selectedId = item.id;
    await loadHistory();
    await loadDocument(item.id);
    startPolling();
  } catch (error) {
    alert(`上传失败：${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "上传并识别";
    uploadForm.reset();
    fileName.textContent = "未选择文件";
  }
});

refreshButton.addEventListener("click", async () => {
  await loadHistory();
  if (selectedId) await loadDocument(selectedId);
});

deleteSelectedButton.addEventListener("click", deleteSelectedBlocks);
undoEditButton.addEventListener("click", undoEdit);
resetEditButton.addEventListener("click", resetEdits);

tabButtons.forEach((button) => {
  button.addEventListener("click", () => {
    currentView = button.dataset.view;
    tabButtons.forEach((item) => item.classList.toggle("active", item === button));
    renderSelectedDocument();
  });
});

async function loadHistory() {
  const response = await fetch("/api/documents");
  const items = await response.json();
  historyList.innerHTML = "";

  for (const item of items) {
    const row = document.createElement("div");
    row.className = `history-item ${item.id === selectedId ? "active" : ""}`;
    row.innerHTML = `
      <button class="history-open" type="button">
        <span class="history-name">${escapeHtml(item.original_name)}</span>
        <span class="status ${item.status}">${statusText(item.status)}</span>
        <span class="history-meta">
          <span>${item.pages || 0} 页</span>
          <span>${item.method}</span>
          <span>${item.duration_seconds ? `${item.duration_seconds}s` : ""}</span>
        </span>
      </button>
      <button class="delete-button" type="button" title="删除历史">删除</button>
    `;
    row.querySelector(".history-open").addEventListener("click", () => loadDocument(item.id));
    row.querySelector(".delete-button").addEventListener("click", () => deleteDocument(item));
    historyList.appendChild(row);
  }
}

async function deleteDocument(item) {
  if (!confirm(`删除 ${item.original_name}？`)) return;
  const response = await fetch(`/api/documents/${item.id}`, { method: "DELETE" });
  if (!response.ok) {
    alert("删除失败");
    return;
  }
  if (selectedId === item.id) {
    selectedId = null;
    selectedDocument = null;
    compareView.classList.add("hidden");
    markdownView.classList.add("hidden");
    emptyState.classList.remove("hidden");
    docTitle.textContent = "选择或上传一个 PDF";
    docMeta.textContent = "识别完成后会显示原 PDF 和带位置的文字层。";
  }
  await loadHistory();
}

async function loadDocument(id) {
  selectedId = id;
  const response = await fetch(`/api/documents/${id}`);
  selectedDocument = await response.json();
  selectedBlockIds.clear();
  await loadHistory();
  renderSelectedDocument();
  if (["queued", "processing"].includes(selectedDocument.status)) startPolling();
}

function renderSelectedDocument() {
  if (!selectedDocument) return;

  emptyState.classList.add("hidden");
  docTitle.textContent = selectedDocument.original_name;
  docMeta.textContent = `${statusText(selectedDocument.status)} · ${selectedDocument.pages || 0} 页 · ${selectedDocument.method}${selectedDocument.duration_seconds ? ` · ${selectedDocument.duration_seconds}s` : ""}`;
  renderMarkdown();

  compareView.classList.toggle("hidden", currentView !== "compare");
  markdownView.classList.toggle("hidden", currentView !== "markdown");
  updateEditToolbarVisibility();

  if (selectedDocument.status === "failed") {
    compareView.innerHTML = `<section class="empty-state"><h3>识别失败</h3><p>${escapeHtml(selectedDocument.error || "未知错误")}</p></section>`;
    return;
  }

  if (selectedDocument.status !== "done") {
    compareView.innerHTML = `<section class="empty-state"><h3>${statusText(selectedDocument.status)}</h3><p>MinerU 正在处理，完成后会自动刷新。</p></section>`;
    return;
  }

  renderCompare(selectedDocument);
}

function renderMarkdown() {
  if (!selectedDocument) return;
  const markdown = selectedDocument.markdown_clean || selectedDocument.markdown || "";
  markdownContent.innerHTML = renderMarkdownDocument(markdown, selectedDocument.id);
}

function renderCompare(doc) {
  const pages = doc.blocks?.pages || [];
  compareView.innerHTML = "";
  for (const page of pages) {
    const pair = document.createElement("article");
    pair.className = "page-pair";

    const pdfColumn = document.createElement("div");
    pdfColumn.className = "page-column";
    pdfColumn.innerHTML = `<h3>原 PDF · 第 ${page.page + 1} 页</h3>`;
    const pdfPage = document.createElement("div");
    pdfPage.className = "pdf-page";
    pdfPage.style.aspectRatio = `${page.width} / ${page.height}`;
    pdfPage.innerHTML = `<img src="/api/documents/${doc.id}/pages/${page.page}.png" alt="PDF 第 ${page.page + 1} 页" loading="lazy" />`;
    pdfColumn.appendChild(pdfPage);

    const textColumn = document.createElement("div");
    textColumn.className = "page-column";
    textColumn.innerHTML = `<h3>识别文字 · 第 ${page.page + 1} 页</h3>`;
    const textPage = document.createElement("div");
    textPage.className = "text-page";
    textPage.style.aspectRatio = `${page.width} / ${page.height}`;
    setupMarqueeSelection(textPage);

    for (const block of page.blocks || []) {
      const [x0, y0, x1, y1] = block.bbox;
      const blockEl = document.createElement("div");
      blockEl.className = "text-block";
      blockEl.dataset.blockId = block.id;
      blockEl.dataset.pageWidth = String(page.width);
      blockEl.dataset.pageHeight = String(page.height);
      blockEl.dataset.x0 = String(x0);
      blockEl.dataset.y0 = String(y0);
      blockEl.dataset.x1 = String(x1);
      blockEl.dataset.y1 = String(y1);
      blockEl.style.left = `${(x0 / page.width) * 100}%`;
      blockEl.style.top = `${(y0 / page.height) * 100}%`;
      blockEl.style.width = `${Math.max(((x1 - x0) / page.width) * 100, 1)}%`;
      blockEl.style.height = `${Math.max(((y1 - y0) / page.height) * 100, 1)}%`;
      blockEl.classList.toggle("selected", selectedBlockIds.has(block.id));
      if (block.image_path) {
        blockEl.classList.add("visual-block");
        blockEl.innerHTML = `
          <img src="/api/documents/${doc.id}/assets/${encodeAssetPath(block.image_path)}" alt="${escapeHtml(block.type || "截图")}" loading="lazy" />
          <span class="crop-hint">在左侧拖拽框线调整</span>
        `;

        const regionEl = document.createElement("div");
        regionEl.className = "visual-region-control";
        regionEl.dataset.blockId = block.id;
        regionEl.dataset.pageWidth = String(page.width);
        regionEl.dataset.pageHeight = String(page.height);
        regionEl.dataset.x0 = String(x0);
        regionEl.dataset.y0 = String(y0);
        regionEl.dataset.x1 = String(x1);
        regionEl.dataset.y1 = String(y1);
        regionEl.classList.toggle("selected", selectedBlockIds.has(block.id));
        regionEl.innerHTML = `
          <span class="region-label">当前图片</span>
          <span class="region-resize-edge top" data-resize-handle="t" title="拖动上边界"></span>
          <span class="region-resize-edge right" data-resize-handle="r" title="拖动右边界"></span>
          <span class="region-resize-edge bottom" data-resize-handle="b" title="拖动下边界"></span>
          <span class="region-resize-edge left" data-resize-handle="l" title="拖动左边界"></span>
          <span class="region-resize-corner top-left" data-resize-handle="tl" title="拖动调整左上角"></span>
          <span class="region-resize-corner top-right" data-resize-handle="tr" title="拖动调整右上角"></span>
          <span class="region-resize-corner bottom-left" data-resize-handle="bl" title="拖动调整左下角"></span>
          <span class="region-resize-corner bottom-right" data-resize-handle="br" title="拖动调整右下角"></span>
        `;
        positionVisualRegion(regionEl, { x0, y0, x1, y1 }, page.width, page.height);
        pdfPage.appendChild(regionEl);
        setupVisualRegionResize(regionEl);
      } else {
        blockEl.textContent = block.text;
      }
      blockEl.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!event.shiftKey && !event.metaKey && !event.ctrlKey) {
          selectedBlockIds.clear();
        }
        toggleBlockSelection(block.id);
        updateSelectionStyles();
      });
      textPage.appendChild(blockEl);
    }

    textColumn.appendChild(textPage);
    pair.append(pdfColumn, textColumn);
    compareView.appendChild(pair);
  }
}

function toggleBlockSelection(blockId) {
  if (!blockId) return;
  if (selectedBlockIds.has(blockId)) {
    selectedBlockIds.delete(blockId);
  } else {
    selectedBlockIds.add(blockId);
  }
}

function updateSelectionStyles() {
  document.querySelectorAll(".text-block[data-block-id], .visual-region-control[data-block-id]").forEach((block) => {
    block.classList.toggle("selected", selectedBlockIds.has(block.dataset.blockId));
  });
  const count = document.querySelector("#selection-count");
  const deleteButton = document.querySelector("#delete-selected");
  if (count) count.textContent = `${selectedBlockIds.size} 个已选`;
  if (deleteButton) deleteButton.disabled = selectedBlockIds.size === 0;
}

function updateEditToolbarVisibility() {
  const visible = currentView === "compare" && selectedDocument?.status === "done";
  editToolbar.classList.toggle("hidden", !visible);
  updateSelectionStyles();
}

function setupVisualRegionResize(regionEl) {
  const handles = regionEl.querySelectorAll("[data-resize-handle]");
  if (!handles.length) return;

  handles.forEach((handle) => handle.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    event.stopPropagation();
    event.preventDefault();
    const pageEl = regionEl.closest(".pdf-page");
    if (!pageEl || !selectedDocument) return;

    selectedBlockIds.clear();
    selectedBlockIds.add(regionEl.dataset.blockId);
    updateSelectionStyles();

    const pageRect = pageEl.getBoundingClientRect();
    const pageWidth = Number(regionEl.dataset.pageWidth);
    const pageHeight = Number(regionEl.dataset.pageHeight);
    const startX0 = Number(regionEl.dataset.x0);
    const startY0 = Number(regionEl.dataset.y0);
    const startX1 = Number(regionEl.dataset.x1);
    const startY1 = Number(regionEl.dataset.y1);
    const startClientX = event.clientX;
    const startClientY = event.clientY;
    const direction = handle.dataset.resizeHandle || "br";
    let draftBox = { x0: startX0, y0: startY0, x1: startX1, y1: startY1 };
    let hasMoved = false;
    regionEl.classList.add("resizing-region");

    const onMove = (moveEvent) => {
      hasMoved = true;
      const dx = ((moveEvent.clientX - startClientX) / pageRect.width) * pageWidth;
      const dy = ((moveEvent.clientY - startClientY) / pageRect.height) * pageHeight;
      let x0 = startX0;
      let y0 = startY0;
      let x1 = startX1;
      let y1 = startY1;
      if (direction.includes("l")) x0 = clamp(startX0 + dx, 0, startX1 - 8);
      if (direction.includes("r")) x1 = clamp(startX1 + dx, startX0 + 8, pageWidth);
      if (direction.includes("t")) y0 = clamp(startY0 + dy, 0, startY1 - 8);
      if (direction.includes("b")) y1 = clamp(startY1 + dy, startY0 + 8, pageHeight);
      draftBox = { x0, y0, x1, y1 };
      positionVisualRegion(regionEl, draftBox, pageWidth, pageHeight);
    };

    const onUp = async () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      regionEl.classList.remove("resizing-region");
      if (!hasMoved) return;
      regionEl.dataset.x0 = String(draftBox.x0);
      regionEl.dataset.y0 = String(draftBox.y0);
      regionEl.dataset.x1 = String(draftBox.x1);
      regionEl.dataset.y1 = String(draftBox.y1);
      await saveVisualRegionResize(regionEl, draftBox);
    };

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }));
}

function positionVisualRegion(regionEl, box, pageWidth, pageHeight) {
  regionEl.style.left = `${(box.x0 / pageWidth) * 100}%`;
  regionEl.style.top = `${(box.y0 / pageHeight) * 100}%`;
  regionEl.style.width = `${((box.x1 - box.x0) / pageWidth) * 100}%`;
  regionEl.style.height = `${((box.y1 - box.y0) / pageHeight) * 100}%`;
}

async function saveVisualRegionResize(regionEl, box = null) {
  if (!selectedDocument) return;
  const blockId = regionEl.dataset.blockId;
  document.querySelectorAll(`[data-block-id="${CSS.escape(blockId)}"]`).forEach((element) => {
    element.classList.add("crop-saving");
  });
  const bbox = [
    Number(box?.x0 ?? regionEl.dataset.x0),
    Number(box?.y0 ?? regionEl.dataset.y0),
    Number(box?.x1 ?? regionEl.dataset.x1),
    Number(box?.y1 ?? regionEl.dataset.y1),
  ];
  const response = await fetch(`/api/documents/${selectedDocument.id}/edit/resize-visual`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ block_id: blockId, bbox }),
  });
  if (!response.ok) {
    document.querySelectorAll(`[data-block-id="${CSS.escape(blockId)}"]`).forEach((element) => {
      element.classList.remove("crop-saving");
    });
    alert("调整截图区域失败");
    await loadDocument(selectedDocument.id);
    return;
  }
  const result = await response.json();
  selectedDocument = result.document;
  selectedBlockIds.clear();
  selectedBlockIds.add(blockId);
  renderSelectedDocument();
}

function setupMarqueeSelection(textPage) {
  let start = null;
  let marquee = null;

  textPage.addEventListener("mousedown", (event) => {
    if (event.button !== 0 || event.target.closest("[data-block-id]")) return;
    const rect = textPage.getBoundingClientRect();
    start = { x: event.clientX - rect.left, y: event.clientY - rect.top, additive: event.shiftKey || event.metaKey || event.ctrlKey };
    marquee = document.createElement("div");
    marquee.className = "selection-marquee";
    textPage.appendChild(marquee);
    event.preventDefault();
  });

  textPage.addEventListener("mousemove", (event) => {
    if (!start || !marquee) return;
    const rect = textPage.getBoundingClientRect();
    const current = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    const left = Math.min(start.x, current.x);
    const top = Math.min(start.y, current.y);
    const width = Math.abs(start.x - current.x);
    const height = Math.abs(start.y - current.y);
    Object.assign(marquee.style, {
      left: `${left}px`,
      top: `${top}px`,
      width: `${width}px`,
      height: `${height}px`,
    });
  });

  textPage.addEventListener("mouseup", () => {
    if (!start || !marquee) return;
    const selectionRect = marquee.getBoundingClientRect();
    if (!start.additive) selectedBlockIds.clear();
    textPage.querySelectorAll("[data-block-id]").forEach((block) => {
      if (rectsIntersect(selectionRect, block.getBoundingClientRect())) {
        selectedBlockIds.add(block.dataset.blockId);
      }
    });
    marquee.remove();
    marquee = null;
    start = null;
    updateSelectionStyles();
  });

  textPage.addEventListener("mouseleave", () => {
    if (marquee) marquee.remove();
    marquee = null;
    start = null;
  });
}

function rectsIntersect(a, b) {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

async function deleteSelectedBlocks() {
  if (!selectedDocument || selectedBlockIds.size === 0) return;
  const response = await fetch(`/api/documents/${selectedDocument.id}/edit/delete-blocks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ block_ids: Array.from(selectedBlockIds) }),
  });
  if (!response.ok) {
    alert("删除失败");
    return;
  }
  const result = await response.json();
  selectedDocument = result.document;
  selectedBlockIds.clear();
  renderSelectedDocument();
  await loadHistory();
}

async function undoEdit() {
  if (!selectedDocument) return;
  const response = await fetch(`/api/documents/${selectedDocument.id}/edit/undo`, { method: "POST" });
  if (!response.ok) {
    alert("撤销失败");
    return;
  }
  const result = await response.json();
  selectedDocument = result.document;
  selectedBlockIds.clear();
  renderSelectedDocument();
}

async function resetEdits() {
  if (!selectedDocument || !confirm("重置到识别完成后的初始状态？")) return;
  const response = await fetch(`/api/documents/${selectedDocument.id}/edit/reset`, { method: "POST" });
  if (!response.ok) {
    alert("重置失败");
    return;
  }
  const result = await response.json();
  selectedDocument = result.document;
  selectedBlockIds.clear();
  renderSelectedDocument();
}

function encodeAssetPath(path) {
  return String(path).split("/").map(encodeURIComponent).join("/");
}

function renderMarkdownDocument(markdown, documentId) {
  const lines = String(markdown || "").split(/\r?\n/);
  const html = [];
  let paragraph = [];
  let listItems = [];
  let tableRows = [];
  let codeLines = [];
  let inCode = false;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    html.push(`<ul>${listItems.map((item) => `<li>${renderInline(item)}</li>`).join("")}</ul>`);
    listItems = [];
  };
  const flushTable = () => {
    if (!tableRows.length) return;
    const rows = tableRows
      .filter((row) => !/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(row))
      .map((row, index) => {
        const cells = row.replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
        const tag = index === 0 ? "th" : "td";
        return `<tr>${cells.map((cell) => `<${tag}>${renderInline(cell)}</${tag}>`).join("")}</tr>`;
      })
      .join("");
    html.push(`<table>${rows}</table>`);
    tableRows = [];
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushTable();
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const trimmed = line.trim();

    if (trimmed.startsWith("```")) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        flushAll();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }

    if (!trimmed) {
      flushAll();
      continue;
    }

    const imageMatch = trimmed.match(/^!\[([^\]]*)\]\(([^)]+)\)\s*$/);
    if (imageMatch) {
      flushAll();
      html.push(`<figure><img src="${assetUrl(documentId, imageMatch[2])}" alt="${escapeHtml(imageMatch[1])}" loading="lazy" /></figure>`);
      continue;
    }

    const headingMatch = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      flushAll();
      const level = headingMatch[1].length;
      html.push(`<h${level}>${renderInline(headingMatch[2])}</h${level}>`);
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph();
      flushTable();
      listItems.push(trimmed.replace(/^[-*]\s+/, ""));
      continue;
    }

    if (trimmed.includes("|") && /^\|?(.+\|)+.+\|?$/.test(trimmed)) {
      flushParagraph();
      flushList();
      tableRows.push(trimmed);
      continue;
    }

    flushList();
    flushTable();
    paragraph.push(trimmed);
  }

  if (inCode) {
    html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  }
  flushAll();
  return html.join("");
}

function renderInline(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function assetUrl(documentId, path) {
  const cleanPath = String(path).replace(/^\.?\//, "");
  if (/^https?:\/\//i.test(cleanPath) || cleanPath.startsWith("/api/")) {
    return escapeHtml(cleanPath);
  }
  return `/api/documents/${documentId}/assets/${encodeAssetPath(cleanPath)}`;
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!selectedId) return;
    await loadDocument(selectedId);
    if (!["queued", "processing"].includes(selectedDocument.status)) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 3000);
}

function statusText(status) {
  return {
    queued: "排队中",
    processing: "识别中",
    done: "已完成",
    failed: "失败",
  }[status] || status;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Delete" || event.key === "Backspace") {
    if (currentView === "compare" && selectedBlockIds.size > 0 && !event.target.matches("input, textarea")) {
      event.preventDefault();
      deleteSelectedBlocks();
    }
  }
});

loadHistory();
