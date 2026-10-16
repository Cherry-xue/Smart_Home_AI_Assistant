"""多 Agent 编排层。

结构
----
- `models.py`  数据契约（Finding / RouteDecision / Intervention）
- `state.py`   图状态（含并行合并的 reducer）
- `prompts.py` 提示词
- `router.py`  路由节点
- `agents/`    5 个专业 Agent（各自 create_agent + 专属工具白名单）
- `nodes.py`   汇总 / 编排 / 确认 / 执行节点
- `graph.py`   图组装与对外接口

设计要点：**主图是手写的 StateGraph**，路由走向是图上的边（可审计），
节点内部才用 `langchain.agents.create_agent`。不用 create_agent 一把梭当总控——
那样路由决定会藏在模型的多轮推理里，出问题无法复盘。
"""

from __future__ import annotations

#: 5 个专业 Agent 的标识（与 agent_runs.route_agent 落库值一致）
AGENT_SCENE = "scene"
AGENT_SECURITY = "security"
AGENT_ENERGY = "energy"
AGENT_CARE = "care"
AGENT_ASSISTANT = "assistant"

#: 全部专业 Agent。顺序即扇出顺序，也用于校验路由输出不越界。
ALL_AGENTS: tuple[str, ...] = (
    AGENT_SCENE,
    AGENT_SECURITY,
    AGENT_ENERGY,
    AGENT_CARE,
    AGENT_ASSISTANT,
)

#: 中文名（用于回答与日志）
AGENT_LABELS: dict[str, str] = {
    AGENT_SCENE: "场景编排",
    AGENT_SECURITY: "安防应急",
    AGENT_ENERGY: "能源优化",
    AGENT_CARE: "健康照护",
    AGENT_ASSISTANT: "家务助理",
}
