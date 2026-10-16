"""知识类工具：说明书检索 + 场景意图约束。

对应规划书 §13.3 里 `list_capability` 的孪生兄弟 —— 一个查"机器能做什么"，
一个查"人写的说明里怎么说"。

引用必须可追溯
------------
检索结果一律带上 `source_file` + `section`。回答里引用说明书时**必须**把它们写出来，
不允许只说"说明书上写了"却指不出来源。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from app.rag.retriever import search as rag_search


def build_knowledge_tools(ctx: Any) -> list[Any]:
    @tool
    def search_knowledge(query: str, k: int = 4, doc_type: str | None = None) -> str:
        """在企业知识库（设备说明书 / 家庭安防应急预案）里检索相关段落。

        什么时候用：用户问"这个设备怎么用""多久维护一次""为什么这么设计"
        "说明书怎么说"，或你需要为某条建议找依据时。

        参数：
        - query    检索问题（自然语言）
        - k        返回几段，默认 4
        - doc_type 限定文档类型：`device_manual`（设备说明书）/ `emergency_plan`（应急预案）

        ⚠️ 返回的每段都带 `[文件 / 章节]` 出处。**在回答里引用时要把出处写出来**；
        如果检索结果与问题无关，就如实说"说明书里没有相关内容"，不要用别的知识凑。
        """
        try:
            hits = rag_search(query, k=max(1, min(int(k or 4), 8)), doc_type=doc_type)
        except Exception as e:  # noqa: BLE001
            return f"知识库检索失败：{type(e).__name__}: {e}"

        if not hits:
            return "知识库里没有检索到相关内容。请如实告知用户说明书未覆盖这个问题。"

        lines: list[str] = []
        for i, h in enumerate(hits, 1):
            dist = f"{h.distance:.3f}" if h.distance is not None else "?"
            titles = getattr(h, "device_types", None) or []
            lines.append(f"[{i}] {h.doc_title} / {h.section}（相关度距离 {dist}，越小越相关）")
            if titles:
                lines.append(f"     涉及设备类型：{', '.join(titles)}")
            lines.append(f"     原文：{h.content.strip()}")
            lines.append(f"     出处：{h.source_file} → {h.section}")
            lines.append("")
        return "\n".join(lines).strip()

    @tool
    def list_scene_intents() -> str:
        """列出系统已登记的**场景意图及其约束区间**（如"观影模式要求照明≤40%"）。

        什么时候用：在为用户编排场景动作**之前**调用，这样你生成的动作天然落在
        合理区间内，不容易被后续的冲突检测剔除。

        注意：这个约束是系统硬性判据，不是你生成的 —— 你应当顺着它来，
        而不是为了满足用户"把灯全开"的要求去绕过它。真的超出区间时，
        照常生成，系统会剔掉并说明原因。
        """
        from app.services.scenes.intents import describe_intents

        return describe_intents()

    return [search_knowledge, list_scene_intents]
