"""知识库端点：检索 / 状态 / 已入库文档。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/knowledge/search` | 语义检索说明书与预案（返回带来源的片段） |
| GET | `/api/knowledge/documents` | 已入库文档清单 |
| GET | `/api/knowledge/status` | 向量库状态（块数、模型、目录） |

两件事要有意识地区分开：

- **检索**（本模块）：把问题变成向量、召回相关片段。它**只负责找**，不负责答；
- **回答生成**（后续 Agent）：拿到片段后组织语言，并且**必须引用** `source_file` + `section`。

这样分开的好处是"**检索准不准**"和"**回答写得好不好**"可以分别验证 ——
如果混在一起，回答错了你根本不知道是没找到资料，还是找到了却编错了。

⚠️ Embedding 调用是同步阻塞的 HTTP 请求，所以这些端点用 `def`（FastAPI 自动丢线程池），
**不能**写成 `async def` —— 否则一次检索就会卡住整个后端。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query, status

from app.config.settings import get_settings
from app.rag.loader import DEFAULT_RAW_DIR, iter_documents
from app.rag.retriever import search as rag_search
from app.rag.store import COLLECTION_NAME, chroma_dir, collection_count, list_indexed_documents
from app.schemas.knowledge import (
    IndexedDocumentOut,
    KnowledgeHitOut,
    KnowledgeSearchOut,
    KnowledgeStatusOut,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get(
    "/search",
    response_model=KnowledgeSearchOut,
    summary="语义检索设备说明书 / 安防预案",
)
def search(
    q: str = Query(..., min_length=1, description="自然语言问题，如『客厅空调滤网多久清洗一次』"),
    k: int = Query(4, ge=1, le=20, description="最多返回几个片段"),
    doc_type: str | None = Query(
        None, description="限定文档类型：device_manual（说明书）/ emergency_plan（应急预案）"
    ),
    device_type: str | None = Query(
        None, description="限定设备类型，如 ac / gas_sensor（在召回后按元数据过滤）"
    ),
    max_distance: float | None = Query(
        None, description="距离上限，超过则不返回（宁可不给，也不用不相关的片段凑数）"
    ),
):
    started = time.perf_counter()
    try:
        hits = rag_search(
            q, k=k, doc_type=doc_type, device_type=device_type, max_distance=max_distance
        )
    except Exception as e:  # noqa: BLE001
        # 检索失败通常是 Embedding 服务不可用（网络/配额），要说清楚而不是笼统 500
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"检索失败（可能是向量模型服务不可用或配额不足）：{type(e).__name__}: {e}",
        ) from e

    return KnowledgeSearchOut(
        query=q,
        k=k,
        count=len(hits),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        collection_size=collection_count(),
        hits=[KnowledgeHitOut(**h.as_dict()) for h in hits],
        note="检索只负责召回片段；引用给用户时必须带上 source_file 与 section，不得声称『说明书上写了』却指不出来源",
    )


@router.get("/documents", response_model=list[IndexedDocumentOut], summary="已入库文档清单")
def documents():
    return [
        IndexedDocumentOut(
            source_file=d["source_file"],
            doc_title=d["doc_title"],
            doc_type=d["doc_type"],
            sections=d["sections"],
            chunks=d["chunks"],
        )
        for d in list_indexed_documents()
    ]


@router.get("/status", response_model=KnowledgeStatusOut, summary="知识库状态")
def knowledge_status():
    settings = get_settings()
    indexed = list_indexed_documents()
    raw_files = sum(1 for _ in iter_documents(DEFAULT_RAW_DIR))
    chunks = collection_count()
    note = ""
    if chunks == 0 and raw_files:
        note = (
            "向量库是空的，但 data/raw 下有源文件 —— "
            "请先执行灌库：cd backend && python -m app.rag.ingest"
        )
    return KnowledgeStatusOut(
        collection=COLLECTION_NAME,
        chunks=chunks,
        embedding_model=settings.embedding_model_name,
        chroma_dir=str(chroma_dir()),
        raw_dir=str(DEFAULT_RAW_DIR),
        raw_files=raw_files,
        indexed_documents=len(indexed),
        documents=[
            IndexedDocumentOut(
                source_file=d["source_file"],
                doc_title=d["doc_title"],
                doc_type=d["doc_type"],
                sections=d["sections"],
                chunks=d["chunks"],
            )
            for d in indexed
        ],
        note=note,
    )
