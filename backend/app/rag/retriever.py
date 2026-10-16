"""检索：向量召回 + 来源元数据。

关于 rerank
-----------
**首期不启用**。理由：
1. 语料只有 6 份文档、几百个块，向量召回的质量已经够用；
2. rerank 走不了 OpenAI 兼容协议（要用 DashScope 原生 SDK），
   多一层依赖换来的收益在这个数据量下不划算。

等语料真的变大、或者出现"检索结果串味"（召回了主题不相关的块）再启用 ——
调用点已经在 `search()` 里预留好了位置。

⚠️ 检索结果**一律带来源元数据**（哪份文档、哪一节、距离多少）。
规划书要求回答里的"记忆依据"必须可追溯：模型不能凭空说"说明书上写了……"，
它必须能指出**具体是哪个文档的哪一节**。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app.rag.store import get_vectorstore


@dataclasses.dataclass
class Retrieved:
    content: str
    distance: float | None
    source_file: str
    doc_title: str
    doc_type: str
    section: str
    device_types: list[str]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _split_csv(value: str | None) -> list[str]:
    return [x for x in (value or "").split(",") if x]


def search(
    query: str,
    *,
    k: int = 4,
    doc_type: str | None = None,
    device_type: str | None = None,
    max_distance: float | None = None,
) -> list[Retrieved]:
    """按问题召回相关块。

    参数
    ----
    k            : 最多返回几块
    doc_type     : 只要某类文档（`device_manual` / `emergency_plan`），由 Chroma 原生过滤
    device_type  : 只要与某个设备类型有关的块。⚠️ 这个字段在库里存的是逗号分隔字符串，
                   Chroma 无法直接做"包含"匹配，所以在**召回之后用 Python 过滤** ——
                   因此这种情况下会多召回一些候选（k*3），避免过滤后条数不足。
    max_distance : 距离上限（越小越相似）。超过阈值的结果宁可不返回，
                   也不要拿明显不相关的块去凑数。
    """
    query = (query or "").strip()
    if not query:
        return []

    fetch_k = k * 3 if device_type else k

    where: dict[str, Any] | None = None
    if doc_type:
        where = {"doc_type": doc_type}

    store = get_vectorstore()
    pairs = store.similarity_search_with_score(query, k=fetch_k, filter=where)

    results: list[Retrieved] = []
    for doc, distance in pairs:
        meta = doc.metadata or {}
        types = _split_csv(meta.get("device_types"))
        if device_type and device_type not in types:
            continue
        if max_distance is not None and distance is not None and distance > max_distance:
            continue
        results.append(
            Retrieved(
                content=doc.page_content,
                distance=float(distance) if distance is not None else None,
                source_file=meta.get("source_file", ""),
                doc_title=meta.get("doc_title", ""),
                doc_type=meta.get("doc_type", ""),
                section=meta.get("section", ""),
                device_types=types,
            )
        )
        if len(results) >= k:
            break

    return results
