"""灌库：把 `data/raw/` 下的说明书 / 预案写进向量库。

跑法（在 `backend/` 目录下）：

    python -m app.rag.ingest                 # 增量：内容没变的文档自动跳过
    python -m app.rag.ingest --rebuild       # 清空重建（改了切分参数后用）
    python -m app.rag.ingest --dry-run       # 只看会切出多少块，**不调用 Embedding（不花钱）**

⚠️ 灌库会真实调用 Embedding API（花钱、有配额），所以：
1. 结果持久化在 `chroma_db/` —— 进程重启、容器重建都**不要重跑**；
2. 增量模式按文档内容哈希判断，源文件没改就不重复灌；
3. 改语料后建议先 `--dry-run` 看一眼块数，确认切分合理再真灌。
"""

from __future__ import annotations

import argparse
import sys

from app.rag.loader import DEFAULT_RAW_DIR, iter_documents, to_chunks
from app.rag.store import (
    COLLECTION_NAME,
    chroma_dir,
    collection_count,
    existing_hashes,
    get_vectorstore,
    reset_collection,
)


def ingest(
    raw_dir=DEFAULT_RAW_DIR,
    *,
    rebuild: bool = False,
    dry_run: bool = False,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
    verbose: bool = True,
) -> dict:
    def log(msg: str = "") -> None:
        if verbose:
            print(msg)

    docs = list(iter_documents(raw_dir))
    if not docs:
        log(f"⚠️ 在 {raw_dir} 下没找到可入库的文档（支持 .md / .txt / .pdf）")
        return {"documents": 0, "chunks": 0, "ingested": 0, "skipped": 0}

    log("=" * 78)
    log("知识库灌库")
    log("=" * 78)
    log(f"  语料目录    : {raw_dir}")
    log(f"  向量库目录  : {chroma_dir()}")
    log(f"  集合        : {COLLECTION_NAME}")
    log(f"  切分参数    : chunk_size={chunk_size}, overlap={chunk_overlap}")
    log(f"  文档数      : {len(docs)}")
    if dry_run:
        log("  模式        : DRY-RUN（不调用 Embedding）")
    log()

    log(f"{'文档':<44} {'章节':>4} {'块数':>5}  状态")
    log("-" * 78)

    total_chunks = 0
    plan: list[tuple] = []
    for doc in docs:
        chunks = to_chunks(doc, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        total_chunks += len(chunks)
        plan.append((doc, chunks))

    if dry_run:
        for doc, chunks in plan:
            log(f"{doc.rel_path:<44} {len(doc.sections):>4} {len(chunks):>5}  (dry-run)")
        log("-" * 78)
        log(f"合计 {len(docs)} 份文档 / {total_chunks} 个块")
        log("⚠️ dry-run 结束，没有写入向量库，也没有调用 Embedding。")
        return {"documents": len(docs), "chunks": total_chunks, "ingested": 0, "skipped": 0}

    if rebuild:
        log("  → 清空原集合（--rebuild）")
        reset_collection()
        seen: set[str] = set()
    else:
        seen = existing_hashes()
        log(f"  已有内容哈希 {len(seen)} 条，内容未变的文档将被跳过")
        log()

    store = get_vectorstore()
    ingested = skipped = 0
    for doc, chunks in plan:
        if doc.content_hash in seen:
            skipped += 1
            log(f"{doc.rel_path:<44} {len(doc.sections):>4} {len(chunks):>5}  跳过（内容未变）")
            continue

        ids = [f"{doc.content_hash}-{i:04d}" for i in range(len(chunks))]
        store.add_documents(chunks, ids=ids)
        ingested += 1
        log(f"{doc.rel_path:<44} {len(doc.sections):>4} {len(chunks):>5}  ✅ 已入库")

    log("-" * 78)
    log(f"入库 {ingested} 份 / 跳过 {skipped} 份；集合现有 {collection_count()} 个块")
    if ingested:
        log("⚠️ 以上入库会真实消耗 Embedding 配额（已持久化，重建进程/容器不用重跑）")
    return {
        "documents": len(docs),
        "chunks": total_chunks,
        "ingested": ingested,
        "skipped": skipped,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="把设备说明书灌进向量库")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="语料目录")
    ap.add_argument("--rebuild", action="store_true", help="清空集合后重建")
    ap.add_argument("--dry-run", action="store_true", help="只统计切分结果，不调用 Embedding")
    ap.add_argument("--chunk-size", type=int, default=800, help="块大小（默认 800）")
    ap.add_argument("--chunk-overlap", type=int, default=100, help="块重叠（默认 100）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stats = ingest(
        args.raw_dir,
        rebuild=args.rebuild,
        dry_run=args.dry_run,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )
    return 0 if stats["documents"] else 1


if __name__ == "__main__":
    sys.exit(main())
