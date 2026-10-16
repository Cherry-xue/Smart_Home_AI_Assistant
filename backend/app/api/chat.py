"""对话端点。

对应规划书 §10.3 契约里的 `/api/chat/stream`，另加一个非流式的 `/api/chat`
（手机 App 首期用非流式更省事，H5/大屏用流式体验更好）。

多 Agent 编排的入口就在这里 —— 一次请求会走完
`路由 → 并行助手 → 汇总 → 编排 → 低风险执行 → 高风险确认 → 回答`。

⚠️ SSE 用 `fetch` + `ReadableStream` 消费，**不要用 `EventSource`**：
`EventSource` 只支持 GET、不能带自定义请求头、也没法优雅中止，
而规划书要求"用户点停止要真的停"（需要 `AbortController`）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import runner
from app.api.deps import get_tool_context
from app.tools import ToolContext

log = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatIn(BaseModel):
    question: str = Field(description="用户这句话")
    thread_id: str | None = Field(default=None, description="会话标识；不传则新建一个")
    home_id: int = Field(default=1)
    member_name: str | None = Field(default=None, description="发言人（首期单用户可留空）")


class ChatOut(BaseModel):
    thread_id: str
    interrupted: bool = Field(description="true 表示停在人工确认节点，等 App 弹窗")
    answer: str = ""
    sections: dict[str, Any] = Field(default_factory=dict, description="分区回答（事实/分析/待确认项…）")
    trace: list[dict[str, Any]] = Field(
        default_factory=list, description="执行过程，可用于「正在做什么」的进度提示"
    )
    intervention: dict[str, Any] | None = Field(default=None, description="待人工确认的载荷")
    route: dict[str, Any] | None = Field(default=None, description="路由结论（便于复盘）")
    plan_rejected: list[dict[str, Any]] = Field(default_factory=list, description="被剔除的动作及原因")
    run_result: dict[str, Any] | None = None


def _new_thread_id() -> str:
    return f"c-{dt.datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8]}"


@router.post("", response_model=ChatOut, summary="问一句（非流式，手机端首期用这个）")
async def chat(body: ChatIn, ctx: ToolContext = Depends(get_tool_context)) -> ChatOut:
    thread_id = body.thread_id or _new_thread_id()
    log.info("收到提问 thread=%s: %s", thread_id, body.question[:80])

    out = await runner.run(
        body.question,
        thread_id=thread_id,
        ctx=ctx,
        home_id=body.home_id,
        member_name=body.member_name,
    )
    return ChatOut(
        thread_id=thread_id,
        interrupted=out["interrupted"],
        answer=out["answer"],
        sections=out["answer_sections"],
        trace=out["trace"],
        intervention=out["intervention"],
        route=out["route"],
        plan_rejected=out["plan_rejected"],
        run_result=out["run_result"],
    )


@router.post("/stream", summary="问一句（SSE 流式）")
async def chat_stream(body: ChatIn, ctx: ToolContext = Depends(get_tool_context)) -> StreamingResponse:
    """流式返回执行过程与最终结果。

    事件类型（前端按 `type` 分支）：
    - `node`      某个节点完成（可用于"正在查客厅温度…"这类进度提示）
    - `interrupt` 停在高风险确认，需要 App 弹窗
    - `final`     最终结果
    - `error`     出错了
    """
    thread_id = body.thread_id or _new_thread_id()

    async def event_source() -> AsyncIterator[str]:
        # 先告诉前端这次用的是哪个会话，后续确认/追问都要带着它
        yield _sse({"type": "start", "thread_id": thread_id})
        try:
            async for event in runner.stream(
                body.question,
                thread_id=thread_id,
                ctx=ctx,
                home_id=body.home_id,
                member_name=body.member_name,
            ):
                yield _sse(event)
        except Exception as e:  # noqa: BLE001
            log.exception("流式对话失败")
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        yield _sse({"type": "done", "thread_id": thread_id})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            # 关掉各级缓冲，否则事件会被攒着一起发，流式就没意义了
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
