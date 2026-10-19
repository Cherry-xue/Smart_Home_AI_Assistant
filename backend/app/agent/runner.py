"""图的外部接口：跑一次 / 流式跑 / 恢复中断。

调用方（API 层、测试脚本）只跟这里打交道，不直接碰 `graph` 与 checkpointer。

关于"中断"的两种形态
------------------
LangGraph 在遇到 `interrupt()` 时会**从 `ainvoke` 正常返回**，
结果里多一个 `__interrupt__` 字段。所以调用方要判断的是"结果里有没有中断"，
而不是"有没有抛异常"。

恢复时用**同一个 `thread_id`** 再调一次，输入换成 `Command(resume=...)` ——
checkpointer 会从上次的现场接着往下走。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from app.agent.graph import get_graph
from app.agent.models import Decision
from app.agent.state import AgentState, initial_state
from app.tools import ToolContext

log = logging.getLogger("app.agent.runner")

#: 当前停在人工确认上的会话：`thread_id -> {payload, created_at, question}`
#:
#: 为什么用内存注册表而不是查库：中断载荷本身就在 checkpointer 里（图状态），
#: 但**要遍历所有 thread 才能找到它**。这里只是记一份"哪些 thread 当前挂着"的索引，
#: 让手机端的"待确认列表"能 O(1) 查到。
#:
#: ⚠️ 已知限制：进程重启后这个索引会丢（图状态仍在 checkpointer 里）。
#: 首期单机开发可以接受；接 MySQL checkpointer 后可以改成启动时扫 `conversations` 重建。
_PENDING: dict[str, dict[str, Any]] = {}


def register_pending(thread_id: str, payload: dict[str, Any], *, extra: dict[str, Any] | None = None) -> None:
    import datetime as dt

    _PENDING[thread_id] = {
        "thread_id": thread_id,
        "payload": payload,
        "created_at": dt.datetime.now(),
        **(extra or {}),
    }


def clear_pending(thread_id: str) -> None:
    _PENDING.pop(thread_id, None)


def list_pending_threads() -> list[dict[str, Any]]:
    """列出所有停在确认节点的会话（含中断载荷）。"""
    return list(_PENDING.values())


def get_pending(thread_id: str) -> dict[str, Any] | None:
    return _PENDING.get(thread_id)


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _extract_interrupt(result: Any) -> dict[str, Any] | None:
    """从图的结果里取出中断载荷（没有中断则返回 None）。

    `__interrupt__` 在不同版本里可能是 tuple / list / 单个对象，
    这里统一成"取第一个 `.value`"。
    """
    if not isinstance(result, dict):
        return None
    raw = result.get("__interrupt__")
    if not raw:
        return None
    item = raw[0] if isinstance(raw, (list, tuple)) else raw
    value = getattr(item, "value", item)
    return value if isinstance(value, dict) else {"raw": value}


def _normalize(result: Any) -> dict[str, Any]:
    """把图的结果整理成 API 层好用的形状。"""
    payload = _extract_interrupt(result)
    state = result if isinstance(result, dict) else {}
    return {
        "interrupted": payload is not None,
        "intervention": payload,
        "answer": state.get("answer") or "",
        "answer_sections": state.get("answer_sections") or {},
        "trace": list(state.get("trace") or []),
        "findings": [f.model_dump() for f in (state.get("findings") or [])],
        "route": _dump_route(state.get("route")),
        "plan": state.get("plan"),
        "plan_rejected": list(state.get("plan_rejected") or []),
        "run_result": state.get("run_result"),
    }


def _dump_route(route: Any) -> dict[str, Any] | None:
    if route is None:
        return None
    if hasattr(route, "model_dump"):
        return route.model_dump()
    return {"raw": str(route)}


# ============================================================================
# 跑一次
# ============================================================================


async def run(
    question: str,
    *,
    thread_id: str,
    ctx: ToolContext,
    graph: Any = None,
    home_id: int = 1,
    member_name: str | None = None,
) -> dict[str, Any]:
    """完整跑一次对话。返回值里 `interrupted=True` 表示停在人工确认节点。"""
    g = graph or get_graph(ctx)
    state = initial_state(
        question, home_id=home_id, member_name=member_name, thread_id=thread_id
    )
    result = await g.ainvoke(state, _config(thread_id))
    out = _normalize(result)

    # 维护"哪些会话正等着人确认"的索引（手机端轮询用）
    if out["interrupted"]:
        register_pending(
            thread_id, out["intervention"] or {},
            extra={"question": question, "home_id": home_id},
        )
    else:
        clear_pending(thread_id)

    log.info(
        "对话完成 thread=%s 中断=%s 结论=%s",
        thread_id, out["interrupted"], (out["answer"] or "")[:60],
    )
    return out


async def resume(
    thread_id: str,
    decisions: list[Decision] | list[dict[str, Any]],
    *,
    ctx: ToolContext,
    graph: Any = None,
) -> dict[str, Any]:
    """恢复一次被中断的对话。

    `decisions` 里每项说明"第几条介入事项、选了什么"。
    ⚠️ 没有出现在 `decisions` 里的高风险动作**保持待确认**（默认不放行）。
    """
    from langgraph.types import Command

    g = graph or get_graph(ctx)
    payload = [
        d.model_dump() if isinstance(d, Decision) else dict(d) for d in decisions
    ]
    result = await g.ainvoke(Command(resume={"decisions": payload}), _config(thread_id))
    out = _normalize(result)
    if not out["interrupted"]:
        clear_pending(thread_id)
    log.info(
        "恢复完成 thread=%s 中断=%s 执行=%s",
        thread_id, out["interrupted"],
        (out.get("run_result") or {}).get("status"),
    )
    return out


# ============================================================================
# 流式跑（给 SSE 用）
# ============================================================================


async def stream(
    question: str,
    *,
    thread_id: str,
    ctx: ToolContext,
    graph: Any = None,
    home_id: int = 1,
    member_name: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """逐节点产出事件，最后给一份完整结果。

    事件形状（前端可按 `type` 分支）：
    - `{"type": "node", "node": ..., "label": ..., "detail": ..., "elapsed_ms": ...}`
    - `{"type": "interrupt", "payload": {...}}`
    - `{"type": "final", "result": {...}}`
    - `{"type": "error", "message": ...}`
    """
    g = graph or get_graph(ctx)
    state = initial_state(
        question, home_id=home_id, member_name=member_name, thread_id=thread_id
    )

    # ⚠️ `__interrupt__` 只出现在流事件里，**不在 snapshot.values 里**。
    # 所以必须在这里抓住它，才能在流结束后把这个会话登记进"待确认"索引；
    # 只推给前端不登记的话，前端卡片画得出来，点「批准」却会 404（该会话没有待确认事项）。
    interrupt_payload: dict[str, Any] | None = None

    try:
        async for chunk in g.astream(state, _config(thread_id), stream_mode="updates"):
            for node, payload in (chunk or {}).items():
                if node == "__interrupt__":
                    item = payload[0] if isinstance(payload, (list, tuple)) else payload
                    value = getattr(item, "value", item)
                    interrupt_payload = value if isinstance(value, dict) else {"raw": value}
                    yield {
                        "type": "interrupt",
                        "payload": interrupt_payload,
                    }
                    continue
                for entry in _trace_of(payload):
                    yield {"type": "node", **entry}
    except Exception as e:  # noqa: BLE001
        log.exception("流式执行失败")
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
        return

    snapshot = await g.aget_state(_config(thread_id))
    values = getattr(snapshot, "values", None) or {}
    out = _normalize(values)

    # 用流里抓到的那份兜底：`_normalize(values)` 拿不到中断载荷
    if interrupt_payload is not None:
        out["interrupted"] = True
        out["intervention"] = interrupt_payload
        register_pending(
            thread_id, interrupt_payload, extra={"question": question, "home_id": home_id}
        )
    elif out["interrupted"]:
        register_pending(
            thread_id, out["intervention"] or {},
            extra={"question": question, "home_id": home_id},
        )
    else:
        clear_pending(thread_id)

    yield {"type": "final", "result": out}


def _trace_of(payload: Any) -> list[dict[str, Any]]:
    """从一个节点的返回里取出 trace 条目（没有就返回空）。"""
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for entry in payload.get("trace") or []:
        if isinstance(entry, dict):
            out.append(
                {
                    "node": entry.get("node"),
                    "label": entry.get("label"),
                    "detail": entry.get("detail"),
                    "elapsed_ms": entry.get("elapsed_ms"),
                }
            )
    return out


# ============================================================================
# 会话状态查询（给"待确认列表"用）
# ============================================================================


async def pending_of(thread_id: str, *, ctx: ToolContext, graph: Any = None) -> dict[str, Any] | None:
    """查询某个会话当前是否停在人工确认节点上。"""
    g = graph or get_graph(ctx)
    snapshot = await g.aget_state(_config(thread_id))
    if not snapshot:
        return None
    values = getattr(snapshot, "values", None) or {}
    if not getattr(snapshot, "next", None):
        return None
    payload = _extract_interrupt(values)
    return payload or {"thread_id": thread_id, "raw": "stopped"}
