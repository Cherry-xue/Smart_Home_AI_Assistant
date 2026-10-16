"""图组装：主线的 `StateGraph`。

拓扑
----
```
START → route ──(按需 Send 扇出)──┬→ scene_node     ─┐
                                  ├→ security_node  ─┤
                                  ├→ energy_node    ─┼→ aggregate → plan
                                  ├→ care_node      ─┤              ↓
                                  └→ assistant_node ─┘         execute_low（低风险直接跑）
                                （无助手时直接进 aggregate）          ↓
                                                              confirm（高风险才停）
                                                                     ↓
                                                          execute_high（执行已批准的）
                                                                     ↓
                                                                 respond → END
```

为什么"扇出"要用条件边 + `Send`
----------------------------
路由结论里的 `agents` 是一份数据，条件边只是**照着它扇出**。这样：
- "这次跑了哪几个助手"在图上是可见的（可审计）；
- 单测可以直接断言"这句话应该扇出到哪几个节点"；
- 加第 6 个助手只需动 `ALL_AGENTS` 与条件边，主流程不变。

⚠️ 编译后的图必须缓存
------------------
`create_agent` 每个要 ~0.9s，5 个就是 4.5s。放到每次请求里会直接毁掉体验。
按 `(catalog 身份, home_id)` 缓存 —— catalog 只在启动时加载一次，
所以进程内通常只编译一遍。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agent import ALL_AGENTS
from app.agent.models import Decision
from app.agent.nodes import (
    aggregate_node,
    build_confirm_gate,
    build_execute_node,
    build_plan_node,
    respond_node,
)
from app.agent.router import route_node
from app.agent.specialists import build_specialist_node
from app.agent.state import AgentState
from app.tools import ToolContext

log = logging.getLogger("app.agent.graph")

#: 进程内图缓存：key -> 编译好的图
_GRAPH_CACHE: dict[tuple[int, int], Any] = {}


def _fan_out(state: AgentState):
    """按路由结论扇出。

    - 没有需要激活的助手 → 直接去汇总（纯查询/纯知识问答走这条路）；
    - 有助手 → 为每个助手生成一个 `Send`，它们会**并行**执行。
    """
    route = state.get("route")
    agents = [a for a in (route.agents if route else []) if a in ALL_AGENTS]
    if not agents:
        return "aggregate"
    return [Send(f"{a}_node", state) for a in agents]


def build_graph(ctx: ToolContext, *, checkpointer: Any = None) -> Any:
    """构建并编译主图。

    不缓存（每次都重新编译）—— 需要缓存请用 `get_graph()`。
    """
    g = StateGraph(AgentState)

    g.add_node("route", route_node)
    g.add_node("aggregate", aggregate_node)

    # 5 个专业 Agent：每个都是独立的 create_agent + 专属工具白名单
    for agent in ALL_AGENTS:
        g.add_node(f"{agent}_node", build_specialist_node(agent, ctx))
        g.add_edge(f"{agent}_node", "aggregate")

    # 确定性主干
    g.add_node("plan", build_plan_node(ctx))
    g.add_node("execute_low", build_execute_node(ctx, only_high_risk=False))
    g.add_node("confirm", build_confirm_gate(ctx))
    g.add_node("execute_high", build_execute_node(ctx, only_high_risk=True))
    g.add_node("respond", respond_node)

    g.add_edge(START, "route")
    g.add_conditional_edges(
        "route",
        _fan_out,
        [f"{a}_node" for a in ALL_AGENTS] + ["aggregate"],
    )
    g.add_edge("aggregate", "plan")
    g.add_edge("plan", "execute_low")
    g.add_edge("execute_low", "confirm")
    g.add_edge("confirm", "execute_high")
    g.add_edge("execute_high", "respond")
    g.add_edge("respond", END)

    if checkpointer is None:
        from app.agent.checkpoint import build_checkpointer
        from app.config.settings import get_settings

        # ⚠️ 内存版只用于开发/测试：进程一重启，中断现场就没了。
        # 要跨重启保留就设 `CHECKPOINT_BACKEND=mysql`（见 app/agent/checkpoint.py）。
        checkpointer = build_checkpointer(get_settings().checkpoint_backend)

    return g.compile(checkpointer=checkpointer)


def get_graph(ctx: ToolContext, *, checkpointer: Any = None, use_cache: bool = True) -> Any:
    """获取编译好的图（默认按 `(catalog 身份, home_id)` 缓存）。"""
    if not use_cache or checkpointer is not None:
        return build_graph(ctx, checkpointer=checkpointer)

    key = (id(ctx.catalog), int(getattr(ctx, "home_id", 0) or 0))
    graph = _GRAPH_CACHE.get(key)
    if graph is None:
        graph = build_graph(ctx)
        _GRAPH_CACHE[key] = graph
        log.info("已编译并缓存 Agent 图（key=%s）", key)
    return graph


def reset_graph_cache() -> None:
    """清空图缓存（设备目录重新加载后需要调用）。"""
    _GRAPH_CACHE.clear()


def graph_info() -> dict[str, Any]:
    """图结构自检信息（供 /api/health 或验收脚本使用）。"""
    return {
        "cached_graphs": len(_GRAPH_CACHE),
        "nodes": [
            "route",
            *[f"{a}_node" for a in ALL_AGENTS],
            "aggregate",
            "plan",
            "execute_low",
            "confirm",
            "execute_high",
            "respond",
        ],
        "agents": list(ALL_AGENTS),
    }
