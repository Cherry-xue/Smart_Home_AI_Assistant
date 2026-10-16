"""路由节点：一次结构化分类，决定激活哪些专业 Agent。

为什么路由要单独成节点（而不是让一个"总 Agent"自己决定调谁）
----------------------------------------------------------
如果路由决定藏在模型的多轮推理里，事后你只能看到"它调了安防助手"，
看不到"它**为什么**这么判断"，也没法复现。

做成图上的节点之后：
- 决定本身是一份结构化数据，可以落库（`agent_runs.route_agent` / `intent`）；
- 走向是**图上的边**，条件边只负责按决定扇出，不再做二次判断；
- 单测可以直接喂一句话断言"它该选哪几个助手"。
"""

from __future__ import annotations

import logging
import time

from app.agent import ALL_AGENTS, AGENT_SCENE
from app.agent.models import RouteDecision
from app.agent.prompts import ROUTER_PROMPT
from app.agent.state import AgentState, TraceEntry

log = logging.getLogger("app.agent.router")


async def route_node(state: AgentState) -> dict:
    """判断用户这句话该交给哪些专业助手。

    返回的 `route` 会被条件边读取并扇出。模型不可用时**降级到只激活场景助手**
    （覆盖面最广的那个），而不是让整个请求失败 —— 一次分类失败不该导致
    "什么都做不了"。
    """
    started = time.perf_counter()
    question = (state.get("question") or "").strip()

    if not question:
        decision = RouteDecision(
            intents=[],
            agents=[],
            reason="空问题",
            needs_clarification="请问需要我做什么？",
        )
        return {
            "route": decision,
            "trace": [_trace(started, "空问题，直接反问他需要什么")],
        }

    prompt = ROUTER_PROMPT.replace("{question}", question)
    decision = await _classify(prompt)

    elapsed = int((time.perf_counter() - started) * 1000)
    detail = (
        f"激活 {decision.agents or '（无，直接回答）'}"
        f"｜涉及房间 {decision.rooms or '未指定'}"
        f"｜{decision.reason}"
    )
    log.info("路由完成 %dms: %s", elapsed, detail)
    return {
        "route": decision,
        "trace": [
            TraceEntry(
                node="router",
                label="理解你的意图",
                detail=detail,
                elapsed_ms=elapsed,
            )
        ],
    }


async def _classify(prompt: str) -> RouteDecision:
    """调模型做结构化分类，并对结果做**边界校验**。"""
    from app.models.llm import get_llm

    try:
        llm = get_llm()
        structured = llm.with_structured_output(RouteDecision)
        raw = await structured.ainvoke(prompt)
        decision = raw if isinstance(raw, RouteDecision) else RouteDecision(**dict(raw))
    except Exception as e:  # noqa: BLE001
        log.warning("路由分类失败，降级为只激活场景助手：%s: %s", type(e).__name__, e)
        return RouteDecision(
            intents=["降级"],
            agents=[AGENT_SCENE],
            reason=f"路由模型调用失败（{type(e).__name__}），降级走场景助手",
        )

    return apply_route_fallbacks(decision)


def apply_route_fallbacks(decision: RouteDecision) -> RouteDecision:
    """对模型给出的路由结论做**边界校验与兜底**。

    ⚠️ 抽成纯函数（不调模型）是为了能直接单测 —— 这几条规则是"系统不会因为
    模型一次误判就哑掉"的最后防线，必须有测试钉住。

    三条规则：
    1. 模型可能编出不存在的 Agent 名 → 丢弃；
    2. 需要反问用户时不该同时激活助手（否则一边提问一边动手）；
    3. **一个助手都没选中、又不是要反问 → 兜底交给场景助手**。
       这条是实测出来的漏洞：提示词曾允许"纯查状态不激活助手"，而图上并没有
       "不激活助手也能回答"的通道 —— 只有助手持有工具，路由为空就等于没人去
       查数据，用户只能拿到"已处理完成"。
    """
    # ---- 规则 1：只保留登记过的 Agent ----
    valid = [a for a in decision.agents if a in ALL_AGENTS]
    dropped = [a for a in decision.agents if a not in ALL_AGENTS]
    if dropped:
        log.warning("路由返回了未登记的 Agent，已丢弃：%s", dropped)
    decision.agents = valid

    # ---- 规则 2：要反问就不动手 ----
    if decision.needs_clarification and decision.agents:
        log.info("路由同时给了反问与 Agent，按'先问清楚'处理，清空 agents")
        decision.agents = []

    # ---- 规则 3：空路由兜底 ----
    if not decision.agents and not decision.needs_clarification:
        log.info("路由未命中任何助手且无需反问 → 兜底交给场景助手")
        decision.agents = [AGENT_SCENE]
        suffix = "未命中具体助手，按通用助手兜底"
        decision.reason = (
            f"{decision.reason}（{suffix}）" if decision.reason else suffix
        )

    return decision


def _trace(started: float, detail: str) -> TraceEntry:
    return TraceEntry(
        node="router",
        label="理解你的意图",
        detail=detail,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )
