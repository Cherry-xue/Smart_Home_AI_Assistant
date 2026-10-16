"""5 个专业 Agent。

每个 Agent 的构造方式完全一致（`create_agent` + 各自的白名单工具 + 各自提示词
+ 统一的 `response_format=Finding`），只有**工具集合**与**提示词**不同。

工具白名单写死在这里（不是提示词里）
----------------------------------
`AGENT_TOOLS` 是"这个 Agent 能碰到什么"的唯一定义。给它加一个工具，
就是给它开一个能力 —— 所以这张表要当成权限表来审，而不是当成配置来随手改。

⚠️ 表里**没有**任何"执行动作"的工具。Agent 只能通过 `Finding.proposed_actions`
**提议**动作，能不能做、做了会怎样，全部由确定性主干决定。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Coroutine

from app.agent import (
    AGENT_ASSISTANT,
    AGENT_CARE,
    AGENT_ENERGY,
    AGENT_LABELS,
    AGENT_SCENE,
    AGENT_SECURITY,
)
from app.agent.models import Finding
from app.agent.prompts import build_agent_prompt
from app.agent.state import AgentState, TraceEntry
from app.tools import ToolContext, build_tools

log = logging.getLogger("app.agent.specialists")


#: 每个专业 Agent 的工具白名单（**权限表**，不是配置项）
AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    AGENT_SCENE: (
        "read_device_state",
        "list_capability",
        "list_scene_intents",
        "search_knowledge",
    ),
    AGENT_SECURITY: (
        "read_device_state",
        "list_capability",
        "read_recent_events",
        "search_knowledge",
    ),
    AGENT_ENERGY: (
        "read_device_state",
        "read_history",
        "calc_energy_cost",
        "list_tariff",
        "search_knowledge",
    ),
    AGENT_CARE: (
        "read_device_state",
        "read_history",
        "read_recent_events",
        "search_memory",
    ),
    AGENT_ASSISTANT: (
        "read_device_state",
        "search_knowledge",
        "search_memory",
    ),
}

#: 交给模型的输入模板。把路由已经识别出的信息一并带上，
#: 省得每个 Agent 再自己解析一遍问题。
_INPUT_TEMPLATE = """用户说：{question}

（系统识别的相关信息：涉及房间 {rooms}；当前发言人 {member}；当前时间 {now}）

请按你的职责处理这个请求。需要查数据就调用工具，最后把结论填进结构化字段。"""


def _render_input(state: AgentState, agent: str) -> str:
    import datetime as dt

    route = state.get("route")
    rooms = "、".join(route.rooms) if route and route.rooms else "未指定"
    member = state.get("member_name") or "户主"
    return _INPUT_TEMPLATE.format(
        question=(state.get("question") or "").strip(),
        rooms=rooms,
        member=member,
        now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def build_specialist_node(
    agent: str, ctx: ToolContext
) -> Callable[[AgentState], Coroutine[Any, Any, dict]]:
    """构造一个专业 Agent 的图节点。

    用闭包捕获 `ctx`（设备目录 / 缓存 / 家庭成员），这样节点签名保持
    LangGraph 要求的 `(state) -> dict`，不需要额外的运行时依赖注入。

    ⚠️ `create_agent` 在**闭包外层**执行，只在建图时做一次。
    放到节点函数里会导致每次执行都重新编译子图（实测 ~0.9s × 5 个 Agent），
    那是不可接受的 —— 这也是图必须缓存的原因之一。
    """
    if agent not in AGENT_TOOLS:
        raise KeyError(f"未知的 Agent：{agent!r}；可选：{sorted(AGENT_TOOLS)}")

    label = AGENT_LABELS.get(agent, agent)
    tools = build_tools(ctx, AGENT_TOOLS[agent])
    system_prompt = build_agent_prompt(agent)

    # 延迟导入：create_agent 导入较慢（~1.9s），放模块顶层会拖慢整个 API 的启动
    from langchain.agents import create_agent

    from app.models.llm import get_llm

    sub_agent = create_agent(
        model=get_llm(),
        tools=tools,
        system_prompt=system_prompt,
        response_format=Finding,
        name=f"{agent}_agent",
    )

    async def node(state: AgentState) -> dict:
        started = time.perf_counter()
        try:
            result = await sub_agent.ainvoke(
                {"messages": [{"role": "user", "content": _render_input(state, agent)}]}
            )
        except Exception as e:  # noqa: BLE001
            log.exception("专业 Agent %s 执行失败", agent)
            finding = Finding(
                agent=agent,
                summary=f"{label}处理失败（{type(e).__name__}）",
                uncertainties=[f"该助手本次未能完成处理：{type(e).__name__}: {e}"],
            )
            return {
                "findings": [finding],
                "trace": [_trace(agent, label, started, "执行失败，已降级为空结论")],
            }

        finding = _extract_finding(result, agent, label)
        detail = (
            f"给出 {len(finding.facts)} 条事实、{len(finding.analyses)} 条分析、"
            f"{len(finding.proposed_actions)} 个动作建议"
        )
        return {
            "findings": [finding],
            "trace": [_trace(agent, label, started, detail)],
        }

    node.__name__ = f"{agent}_node"
    return node


def _extract_finding(result: Any, agent: str, label: str) -> Finding:
    """从 `create_agent` 的返回里取出结构化结论。

    不同版本把结构化输出放在 `structured_response` 或 `messages` 末尾，
    这里做兼容处理；取不到就返回一个诚实的空结论，**不猜**。
    """
    raw = None
    if isinstance(result, dict):
        raw = result.get("structured_response") or result.get("response")
    if raw is None:
        raw = getattr(result, "structured_response", None)

    if isinstance(raw, Finding):
        raw.agent = agent  # 以防模型写错自己的标识
        return raw
    if isinstance(raw, dict):
        try:
            finding = Finding(**raw)
            finding.agent = agent
            return finding
        except Exception as e:  # noqa: BLE001
            log.warning("结构化结论字段不合法：%s", e)

    # 兜底：把最后一条消息的文本当作总结（至少不丢信息）
    text = ""
    if isinstance(result, dict):
        msgs = result.get("messages") or []
        if msgs:
            last = msgs[-1]
            text = getattr(last, "content", "") or str(last)
    return Finding(
        agent=agent,
        summary=(text[:200] if text else f"{label}未返回结构化结论"),
        uncertainties=["本次未拿到结构化输出，以上为模型原始回复摘录"],
    )


def _trace(agent: str, label: str, started: float, detail: str) -> TraceEntry:
    return TraceEntry(
        node=agent,
        label=f"{label}",
        detail=detail,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
