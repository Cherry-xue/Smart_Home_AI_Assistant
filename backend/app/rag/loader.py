"""文档加载与分块：把 `data/raw/` 下的说明书/预案变成可检索的块。

支持格式
--------
| 格式 | 处理方式 |
|---|---|
| `.md` / `.txt` | 直接读取（UTF-8） |
| `.pdf` | `pypdf` 抽取文本 |

**不用 `langchain-community` 的 loader** —— 选型已定不依赖它；自己封装比拉一整棵依赖树可控。
将来拿到真实的厂商说明书 PDF，直接丢进 `data/raw/` 即可，代码不用动。

为什么按章节切分，而不是整篇直接切
--------------------------------
1. **元数据可追溯**：每块都带"来自哪份文档的哪一节"，回答时才能引用到具体章节
   （规划书要求"记忆依据"必须可追溯）；
2. **检索质量**：「空调温度与耗电的关系」这一节不会被切碎后混进别的主题；
3. 每个章节内部再用 `RecursiveCharacterTextSplitter` 做二级切分，避免超长章节撑爆上下文。

文档头部的 YAML front matter（`---` 包裹）会被解析成元数据并**从正文中剥离**——
它不该出现在检索结果里。
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from pathlib import Path
from typing import Any, Iterator

import yaml
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

#: 默认语料目录：backend/data/raw
DEFAULT_RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"

SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt", ".pdf")

FRONT_MATTER_RE = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n", re.S)
#: 一、二、三级标题都算章节边界
SECTION_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)


@dataclasses.dataclass
class DocSection:
    title: str
    text: str
    order: int
    level: int


@dataclasses.dataclass
class LoadedDoc:
    path: Path
    rel_path: str
    title: str
    metadata: dict[str, Any]
    body: str
    sections: list[DocSection]
    content_hash: str

    @property
    def device_types(self) -> list[str]:
        v = self.metadata.get("device_types") or []
        return [str(x) for x in v] if isinstance(v, list) else [str(v)]

    @property
    def room_scope(self) -> list[str]:
        v = self.metadata.get("room_scope")
        if v is None:
            return []
        return [str(x) for x in v] if isinstance(v, list) else [str(v)]


# ---------------------------------------------------------------- 读取
def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"## 第 {i} 页\n\n{text.strip()}")
    return "\n\n".join(pages)


def _read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)
    return path.read_text(encoding="utf-8")


def _split_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    m = FRONT_MATTER_RE.match(raw)
    if not m:
        return {}, raw
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, raw[m.end():]


def _split_sections(body: str, fallback_title: str) -> list[DocSection]:
    """按标题切章节。

    第一段（首个标题之前的内容）如果非空，作为 `概述` 保留下来 ——
    很多文档的"适用范围/免责声明"就在标题之前。
    """
    matches = list(SECTION_RE.finditer(body))
    if not matches:
        text = body.strip()
        return [DocSection(fallback_title, text, 0, 1)] if text else []

    sections: list[DocSection] = []
    order = 0

    head = body[: matches[0].start()].strip()
    if head:
        sections.append(DocSection("概述", head, order, 0))
        order += 1

    for idx, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        if not text:
            continue
        sections.append(DocSection(title, text, order, level))
        order += 1
    return sections


def load_document(path: Path, *, raw_dir: Path | None = None) -> LoadedDoc:
    raw_dir = raw_dir or path.parent
    raw = _read_text(path)
    metadata, body = _split_front_matter(raw)

    try:
        rel_path = str(path.relative_to(raw_dir)).replace("\\", "/")
    except ValueError:
        rel_path = path.name

    title = str(metadata.get("title") or path.stem)
    sections = _split_sections(body, title)
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    return LoadedDoc(
        path=path,
        rel_path=rel_path,
        title=title,
        metadata=metadata,
        body=body,
        sections=sections,
        content_hash=content_hash,
    )


def iter_documents(raw_dir: Path | str = DEFAULT_RAW_DIR) -> Iterator[LoadedDoc]:
    base = Path(raw_dir)
    if not base.exists():
        return
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES:
            yield load_document(p, raw_dir=base)


# ---------------------------------------------------------------- 分块
def to_chunks(
    doc: LoadedDoc,
    *,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> list[Document]:
    """把一份文档切成带元数据的块。

    参数与规划书 §13.2 保持一致（`chunk_size=800, chunk_overlap=100`）。
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        add_start_index=True,
        separators=["\n\n", "\n", "。", "；", " ", ""],
    )

    docs: list[Document] = []
    for sec in doc.sections:
        pieces = splitter.split_text(sec.text) or [sec.text]
        for i, piece in enumerate(pieces):
            docs.append(
                Document(
                    page_content=piece,
                    metadata={
                        "source_file": doc.rel_path,
                        "doc_title": doc.title,
                        "doc_type": str(doc.metadata.get("doc_type") or "unknown"),
                        "section": sec.title,
                        "section_order": sec.order,
                        "chunk_index": i,
                        "content_hash": doc.content_hash,
                        "device_types": ",".join(doc.device_types),
                        "room_scope": ",".join(doc.room_scope),
                    },
                )
            )
    return docs
