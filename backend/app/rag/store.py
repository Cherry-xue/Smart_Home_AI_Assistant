"""向量库：嵌入式 Chroma（不起独立服务）。

选型决定（见技术选型方案 §3.5）
----------------------------
- **嵌入式 `PersistentClient`**，落盘项目根下的 `chroma_db/`；
- 语料只有 6 份文档、几百个 chunk，为一个向量库单独起容器纯属浪费；
- **只能单进程写** —— 所以灌库由独立脚本执行（`python -m app.rag.ingest`），
  服务进程只做**只读检索**。这也是为什么服务启动时不做自动灌库。

**可逆性**：真要切成 server 模式，只改 `get_client()` 这一处
（`PersistentClient` → `HttpClient`），检索代码一行不动。

⚠️ `chroma_db/` 是数据不是代码，已在 `.gitignore` 里。
⚠️ 关掉匿名遥测（`anonymized_telemetry=False`）：本项目不需要它，也不想让它外发请求。
"""

from __future__ import annotations

import functools
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_chroma import Chroma

from app.config.settings import PROJECT_ROOT
from app.models.llm import get_embeddings

#: 集合名（一个项目一个集合；将来若做多租户，按 home_id 分集合）
COLLECTION_NAME = "home_knowledge"

#: 持久化目录。放项目根，便于整体打包/迁移（换机器时把这个目录一起带走即可）
DEFAULT_CHROMA_DIR = PROJECT_ROOT / "chroma_db"


def chroma_dir() -> Path:
    return DEFAULT_CHROMA_DIR


@functools.lru_cache(maxsize=1)
def get_client() -> chromadb.ClientAPI:
    """Chroma 客户端（进程内单例）。

    必须单例：chromadb 对同一个路径同时只允许一个 client，
    重复创建会告警并可能拿到不一致的句柄。
    """
    path = chroma_dir()
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(path),
        settings=ChromaSettings(anonymized_telemetry=False),
    )


@functools.lru_cache(maxsize=1)
def get_vectorstore() -> Chroma:
    """LangChain 包装的向量库（检索与灌库共用）。"""
    return Chroma(
        client=get_client(),
        collection_name=COLLECTION_NAME,
        embedding_function=get_embeddings(),
    )


def collection_count() -> int:
    """当前集合里的块数（不触发 embedding，开销很小）。"""
    try:
        return get_client().get_or_create_collection(COLLECTION_NAME).count()
    except Exception:  # noqa: BLE001
        return 0


def existing_hashes() -> set[str]:
    """已入库文档的内容哈希集合（用于增量灌库时跳过没变的文档）。"""
    try:
        res = get_client().get_or_create_collection(COLLECTION_NAME).get(include=["metadatas"])
    except Exception:  # noqa: BLE001
        return set()
    metas = res.get("metadatas") or []
    return {m.get("content_hash") for m in metas if m and m.get("content_hash")}


def reset_collection() -> None:
    """清空集合（重建前调用）。

    用"删集合再重建"而不是逐条删 —— 后者在块多的时候慢得多，
    而且删到一半失败会留下半截数据。
    """
    client = get_client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:  # noqa: BLE001
        # 集合本来就不存在时会抛异常，属于正常情况
        pass
    client.get_or_create_collection(COLLECTION_NAME)
    # 清掉缓存的 vectorstore，避免它继续持有旧集合的句柄
    get_vectorstore.cache_clear()


def list_indexed_documents() -> list[dict]:
    """已入库文档清单（从块的元数据聚合出来）。

    知识库管理页需要知道"库里到底有哪些资料"，而 Chroma 没有"按文档"的概念 ——
    它的最小单位是块，所以这里按 `source_file` 反向聚合。
    """
    try:
        res = get_client().get_or_create_collection(COLLECTION_NAME).get(include=["metadatas"])
    except Exception:  # noqa: BLE001
        return []

    agg: dict[str, dict] = {}
    for m in res.get("metadatas") or []:
        if not m:
            continue
        key = m.get("source_file", "")
        item = agg.setdefault(
            key,
            {
                "source_file": key,
                "doc_title": m.get("doc_title", ""),
                "doc_type": m.get("doc_type", ""),
                "sections": [],
                "chunks": 0,
            },
        )
        item["chunks"] += 1
        sec = m.get("section")
        if sec and sec not in item["sections"]:
            item["sections"].append(sec)
    return sorted(agg.values(), key=lambda x: x["source_file"])
