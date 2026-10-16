"""M2 · RAG 验收脚本（设备说明书入库与检索）。

跑法：
    cd backend
    python tests/rag_check.py                # 全量（会调用 Embedding API 做检索）
    python tests/rag_check.py --skip-search  # 只查语料/切分/入库状态，不调 API

检查四层：

1. **语料与切分** —— 文档能不能读出来、章节与元数据是否完整；
2. **入库状态** —— 向量库块数与文档清单；
3. **检索质量** —— 一组"问题 → 应命中的文档与章节"，**Top-1 必须命中**。
   这是把"检索准不准"变成客观检查的关键：改切分参数或换 embedding 模型后，
   重跑这个脚本就知道有没有变差；
4. **元数据可追溯** —— 每个命中都必须带 `source_file` 与 `section`。
   规划书要求回答里的"记忆依据"可追溯，**模型不能声称"说明书上写了"却指不出来源**。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.rag.loader import (  # noqa: E402
    DEFAULT_RAW_DIR,
    _split_sections,
    iter_documents,
    to_chunks,
)
from app.rag.retriever import search  # noqa: E402
from app.rag.store import COLLECTION_NAME, chroma_dir, collection_count, list_indexed_documents  # noqa: E402


#: 检索用例：(问题, Top-1 应命中的文档关键词, Top-1 应命中的章节关键词)
RETRIEVAL_CASES: list[tuple[str, str, str]] = [
    ("客厅空调滤网多久清洗一次", "空气调节", "维护周期"),
    ("闻到燃气味应该先做什么", "应急预案", "燃气泄漏"),
    ("热水器什么时候开最省钱", "空气调节", "热水器建议"),
    ("为什么照护场景不用摄像头", "安防与门禁", "毫米波"),
    ("门锁能不能自动打开", "安防与门禁", "智能门锁"),
    ("热水器目标水温可以设到多少度", "空气调节", "智能热水器"),
    ("烟感和水浸的告警等级是什么", "厨房与用水", "报警阈值"),
    ("电视音量建议开多大", "影音与环境", "使用建议"),
]


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  ✅ {name}" + (f"  [{detail}]" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  ❌ {name}" + (f"  [{detail}]" if detail else ""))

    def report(self, title: str) -> None:
        print()
        print("-" * 76)
        if self.failed:
            print(f"{title}: {self.passed} PASS / {len(self.failed)} FAIL")
            for f in self.failed:
                print(f"    ❌ {f}")
        else:
            print(f"{title}: {self.passed} PASS / 0 FAIL")
        print("-" * 76)


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 验收")
    ap.add_argument("--skip-search", action="store_true", help="跳过检索测试（不调用 Embedding）")
    ap.add_argument("--verbose", action="store_true", help="打印每题的召回详情")
    args = ap.parse_args()

    ck = Checker()
    print()
    print("#" * 76)
    print("# M2 · RAG 验收（设备说明书入库与检索）")
    print("#" * 76)
    print(f"  语料目录 : {DEFAULT_RAW_DIR}")
    print(f"  向量库   : {chroma_dir()} / 集合 {COLLECTION_NAME}")
    print()

    # ---------------------------------------------------------- 1. 语料与切分
    print("=" * 76)
    print("1 · 语料加载与切分")
    print("=" * 76)
    docs = list(iter_documents(DEFAULT_RAW_DIR))
    ck.check("发现源文档", len(docs) > 0, f"{len(docs)} 份")
    ck.check("文档都解析出了标题", all(d.title for d in docs))
    ck.check("文档都切出了章节", all(len(d.sections) > 0 for d in docs),
             ", ".join(f"{d.rel_path.split('_')[-1][:6]}:{len(d.sections)}节" for d in docs))
    ck.check("front matter 已从正文剥离（正文不以 --- 开头）",
             all(not d.body.lstrip().startswith("---") for d in docs))
    ck.check("元数据含 doc_type",
             all(d.metadata.get("doc_type") for d in docs),
             ", ".join(sorted({str(d.metadata.get("doc_type")) for d in docs})))
    ck.check("文档 H1 标题被识别为第一节",
             all(d.sections[0].title == d.title for d in docs),
             f"示例：{docs[0].sections[0].title}" if docs else "")

    # 单独验证"标题之前有内容"时不会丢 —— 用构造样例，不依赖实际文档的排版
    probe = _split_sections("这是标题前的一段说明。\n\n## 第一节\n正文一\n", "探针")
    ck.check("标题前的内容会被保留为『概述』",
             bool(probe) and probe[0].title == "概述" and "标题前" in probe[0].text,
             f"{[s.title for s in probe]}")

    all_chunks = [c for d in docs for c in to_chunks(d)]
    ck.check("切分出块", len(all_chunks) > 0, f"{len(all_chunks)} 块")
    ck.check("每块都带来源元数据",
             all(c.metadata.get("source_file") and c.metadata.get("section") for c in all_chunks))
    ck.check("每块都不为空", all(c.page_content.strip() for c in all_chunks))
    longest = max(len(c.page_content) for c in all_chunks)
    ck.check("没有超长块（远小于 chunk_size 上限）", longest <= 1200, f"最长 {longest} 字符")

    # ---------------------------------------------------------- 2. 入库状态
    print()
    print("=" * 76)
    print("2 · 向量库入库状态")
    print("=" * 76)
    chunks_in_store = collection_count()
    ck.check("向量库非空", chunks_in_store > 0, f"{chunks_in_store} 块")
    if chunks_in_store == 0:
        print("     → 请先灌库：python -m app.rag.ingest")
    ck.check("入库块数与切分结果一致",
             chunks_in_store == len(all_chunks),
             f"库 {chunks_in_store} / 切分 {len(all_chunks)}"
             + ("（增量灌库后源文件被改过？用 --rebuild 重灌）" if chunks_in_store != len(all_chunks) else ""))
    indexed = list_indexed_documents()
    ck.check("文档清单完整", len(indexed) == len(docs), f"{len(indexed)} 份")
    ck.check("清单按文档聚合出章节名", all(d["sections"] for d in indexed),
             f"示例：{indexed[0]['source_file']} → {indexed[0]['sections'][:2]}…" if indexed else "")

    # ---------------------------------------------------------- 3. 检索质量
    print()
    print("=" * 76)
    print("3 · 检索质量（Top-1 必须命中预期章节）")
    print("=" * 76)
    if args.skip_search:
        print("  ⏭️  已跳过（--skip-search）")
    elif chunks_in_store == 0:
        print("  ⏭️  向量库为空，跳过")
    else:
        for query, want_doc, want_section in RETRIEVAL_CASES:
            hits = search(query, k=3)
            if not hits:
                ck.check(f"『{query}』能召回", False, "0 条")
                continue
            top = hits[0]
            ok = want_doc in top.doc_title and want_section in top.section
            detail = f"→ {top.doc_title} / {top.section}（距离 {top.distance:.3f}）"
            ck.check(f"『{query}』", ok, detail)
            if args.verbose or not ok:
                for h in hits[1:]:
                    print(f"       · {h.doc_title} / {h.section}（{h.distance:.3f}）")

        # 过滤能力
        only_plan = search("燃气泄漏怎么处理", k=3, doc_type="emergency_plan")
        ck.check("doc_type 过滤生效（只返回应急预案）",
                 bool(only_plan) and all(h.doc_type == "emergency_plan" for h in only_plan),
                 f"{len(only_plan)} 条")

        only_ac = search("滤网清洗周期", k=3, device_type="ac")
        ck.check("device_type 过滤生效（只返回与空调有关的块）",
                 bool(only_ac) and all("ac" in h.device_types for h in only_ac),
                 f"{len(only_ac)} 条")

        # 元数据可追溯（规划书硬要求）
        hits = search("燃气浓度报警阈值是多少", k=4)
        ck.check("每个命中都带 source_file（引用可指到文件）",
                 bool(hits) and all(h.source_file for h in hits))
        ck.check("每个命中都带 section（引用可指到节）",
                 bool(hits) and all(h.section for h in hits))
        ck.check("命中带距离值（可判断相关度）",
                 bool(hits) and all(h.distance is not None for h in hits))

        # 阈值过滤：不相关的宁可不要
        strict = search("如何种西红柿", k=5, max_distance=0.5)
        loose = search("如何种西红柿", k=5)
        ck.check("max_distance 能滤掉不相关结果",
                 len(strict) <= len(loose), f"严格 {len(strict)} / 宽松 {len(loose)}")

        ck.check("空查询返回空结果", search("   ", k=3) == [])

    ck.report("RAG 验收")
    print()
    print("=" * 76)
    if ck.failed:
        print(f"结果：{ck.passed} PASS / {len(ck.failed)} FAIL")
    else:
        print(f"结果：{ck.passed} PASS / 0 FAIL  —— RAG 验收通过 ✅")
    print("=" * 76)
    return 1 if ck.failed else 0


if __name__ == "__main__":
    sys.exit(main())
