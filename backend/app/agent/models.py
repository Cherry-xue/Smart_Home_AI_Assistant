"""Agent 的数据契约。

这里定义的是整个 M4 的**接口**：

- 路由节点必须产出 `RouteDecision`；
- 每个专业 Agent 必须产出统一的 `Finding`，**不是自由文本**；
- 汇总节点读 `Finding` 列表，用**纯代码**拼装回答分区，不再调用模型。

为什么强制结构化
--------------
如果让子 Agent 输出自然语言，汇总节点只能让模型"再总结一遍" ——
那是信息二次损耗，而且多了一次幻觉机会。

结构化之后，"汇总"退化成一次确定性的字段拼接：可测、可复现，
而且**事实与建议天然分开**（`facts` 与 `analyses` / `proposed_actions` 是不同字段），
正好满足规划书 §10.2"回答固定分区"的要求。

⚠️ 字段的 `description` 会进入模型看到的 JSON schema，所以要写成"给模型看的说明"。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

#: 各 Agent 提议动作时的置信度语义（不参与执行决策，只用于前端展示）
ActionConfidence = Literal["high", "medium", "low"]


# ============================================================================
# 一、事实与分析（"是什么" vs "所以呢" 严格分开）
# ============================================================================


class Fact(BaseModel):
    """一条**观测事实**：来自设备数据 / 库表 / 事件，不是模型推测。

    允许 `value=None`：查不到数据时也要如实说出来（宁可不给，不要编）。
    """

    label: str = Field(description="指标的中文名，如「客厅温度」")
    value: Any | None = Field(default=None, description="数值或布尔或文本；无数据时为 null")
    unit: str | None = Field(default=None, description="单位，如 ℃ / % / kWh")
    device_code: str | None = Field(default=None, description="来源设备编码（可追溯）")
    observed_at: str | None = Field(default=None, description="观测时间（ISO 8601）")
    quality: str | None = Field(default=None, description="数据质量标记：good/suspect/bad")
    is_stale: bool = Field(default=False, description="是否已过期。为 true 时**不得**当作实时值陈述")

    def render(self) -> str:
        """渲染成回答里的一行。"""
        if self.value is None:
            body = "无数据"
        else:
            body = f"{self.value}{self.unit or ''}"
        tail = []
        if self.observed_at:
            tail.append(f"更新于 {self.observed_at}")
        if self.is_stale:
            tail.append("⚠️ 数据已过期")
        if self.quality and self.quality != "good":
            tail.append(f"质量标记 {self.quality}")
        suffix = f"（{'；'.join(tail)}）" if tail else ""
        return f"{self.label}：{body}{suffix}"


class Citation(BaseModel):
    """说明书 / 预案的引用出处。**必须能指到具体文件与章节**。"""

    source_file: str = Field(description="文件名，如 02_空气调节与热水设备说明书.md")
    section: str = Field(description="章节标题，如「3.4 热水器使用建议」")
    snippet: str = Field(default="", description="原文摘录（简短，不超过 120 字）")


class MemoryRef(BaseModel):
    """引用的长期记忆。带来源与置信度 —— 用户要能追问"你凭什么这么推荐"。"""

    memory_id: int | None = Field(default=None, description="memories 表主键")
    content: str = Field(description="记忆内容")
    source: str = Field(default="", description="来源：manual / chat / feedback")
    confidence: float = Field(default=0.0, description="置信度 0~1，低置信度不应影响结论")


# ============================================================================
# 二、提议动作（Agent 只"提议"，绝不执行）
# ============================================================================


class ProposedAction(BaseModel):
    """一条**待编排**的设备动作。

    ⚠️ 这只是"想做"，不是"已做"。它必须再经过确定性编排（能力校验 / 冲突检测）
    才会进入执行环节。高风险设备的动作**永远**只能生成待确认项。
    """

    device_code: str = Field(description="设备编码，必须来自工具返回的真实设备")
    capability: str = Field(description="能力编码，如 power_on / brightness / target_temp")
    value: Any = Field(description="目标值，必须符合能力档案声明的类型与范围")
    reason: str = Field(default="", description="为什么要这么做（一句话，会展示给用户）")
    confidence: ActionConfidence = Field(default="medium", description="对该动作把握程度")


# ============================================================================
# 三、专业 Agent 的统一产出
# ============================================================================


class Finding(BaseModel):
    """一个专业 Agent 的完整产出。**5 个 Agent 共用这一个结构**。

    这是并行扇出后要合并的东西，所以尽量保持"自解释"：
    汇总节点不做二次理解，只做字段拼接。
    """

    agent: str = Field(description="产出的 Agent 标识：scene/security/energy/care/assistant")
    summary: str = Field(description="一句话结论，会直接展示给用户")

    facts: list[Fact] = Field(default_factory=list, description="观测事实（有据可查）")
    analyses: list[str] = Field(default_factory=list, description="分析结果。数值必须由确定性代码算出，不心算")

    proposed_actions: list[ProposedAction] = Field(
        default_factory=list, description="提议的设备动作；没有就留空数组"
    )

    memory_refs: list[MemoryRef] = Field(default_factory=list, description="引用了哪些用户偏好")
    citations: list[Citation] = Field(default_factory=list, description="引用了说明书/预案的哪些章节")

    uncertainties: list[str] = Field(
        default_factory=list,
        description="数据不足、口径不明、无法判断的地方。**不确定就写在这里，不要编一个答案**",
    )

    def render_lines(self) -> list[str]:
        """给汇总用的纯文本渲染（不调模型）。"""
        out = [f"【{self.agent}】{self.summary}"]
        for f in self.facts:
            out.append(f"  · {f.render()}")
        for a in self.analyses:
            out.append(f"  · 分析：{a}")
        for u in self.uncertainties:
            out.append(f"  · 不确定：{u}")
        return out


# ============================================================================
# 四、路由结论
# ============================================================================


class RouteDecision(BaseModel):
    """路由节点的一次性分类结果。

    ⚠️ 这里**只做分类**，不做执行决策。产出后由 `graph` 用条件边扇出，
    所以"调了哪几个 Agent"是图上可见的事实，不是模型的隐式行为。
    """

    intents: list[str] = Field(
        default_factory=list,
        description="用户意图标签，如 查询状态 / 场景控制 / 安防咨询 / 能耗分析 / 健康照护 / 设备用法",
    )
    agents: list[str] = Field(
        default_factory=list,
        description="需要激活的 Agent（可多选）：scene/security/energy/care/assistant。"
        "纯闲聊或纯知识问答可以留空数组",
    )
    rooms: list[str] = Field(default_factory=list, description="涉及的房间中文名")
    reason: str = Field(default="", description="为什么这么路由（一句话，会落库便于复盘）")
    needs_clarification: str | None = Field(
        default=None,
        description="信息不足需要反问用户时，把要问的话写在这里；否则为 null。"
        "此时 agents 应为空数组",
    )


# ============================================================================
# 五、人工介入（高风险确认 + 目标冲突裁决，共用一条通道）
# ============================================================================


class InterventionItem(BaseModel):
    """一条待人工决定的事项。

    ⚠️ 高风险确认与"省电还是舒适"的冲突裁决**共用这个结构** ——
    两者本质都是"暂停，等人做决定，再继续"，所以走同一条通道、同一个前端组件。
    """

    kind: Literal["high_risk", "conflict"] = Field(description="high_risk=高风险动作确认；conflict=目标冲突裁决")
    device_code: str | None = Field(default=None, description="涉及设备（冲突裁决时可为空）")
    device_name: str = Field(default="", description="设备中文名")
    capability: str | None = Field(default=None, description="动作能力")
    value: Any | None = Field(default=None, description="目标值")
    prompt: str = Field(default="", description="要展示给人看的问题")
    detail: str = Field(default="", description="为什么要问（风险说明 / 冲突原因）")
    options: list[str] = Field(
        default_factory=list,
        description="可选项。高风险确认通常是 批准/驳回；冲突裁决是若干方案标签",
    )
    command_id: int | None = Field(default=None, description="device_commands 主键（高风险确认关联）")
    expires_at: str | None = Field(default=None, description="超时时间，过期自动失效")


class Decision(BaseModel):
    """人对一条 `InterventionItem` 的决定（`Command(resume=...)` 里回传）。"""

    intervention_index: int = Field(default=0, description="对应第几条介入事项")
    choice: str = Field(description="选择项，如 approve / reject / 某个方案标签")
    note: str = Field(default="", description="备注（可选）")
