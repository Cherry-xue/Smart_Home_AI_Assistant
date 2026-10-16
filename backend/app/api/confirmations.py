"""人工确认端点。

对应规划书 §10.3 的 `/api/confirmations`。手机 App 的"高风险弹窗"就打这两个接口：

```
GET  /api/confirmations/pending        列出当前所有待确认事项
POST /api/confirmations/{thread_id}    对某条事项给出决定
```

⚠️ 关于"批准之后会发生什么"（很重要，别期待错）
------------------------------------------
本项目有一条不可让步的红线：**门锁、燃气阀、电气总闸、布防撤防永不自动执行**
（`HIGH_RISK_AUTO_EXEC=false`）。

所以"批准"的语义是：**人工已经确认，系统留下确认人与时间的痕迹**，
而不是"系统现在去把门打开"。真正的动作仍需要人到现场或由具备权限的通道完成。

这条在实现上由两处独立保证：`executor.create_command` 里的强制改写，
以及数据库 `device_commands.status` 的默认值。任何一个被绕过都还有另一个兜底。
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.agent.runner import clear_pending, get_pending, list_pending_threads, resume
from app.api.deps import get_tool_context
from app.schemas.confirmation import DecisionIn, DecisionOut, InterventionOut, PendingListOut
from app.tools import ToolContext

log = logging.getLogger("app.api.confirmations")

router = APIRouter(prefix="/confirmations", tags=["confirmations"])


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return dt.datetime.fromisoformat(expires_at) < dt.datetime.now()
    except ValueError:
        return False


@router.get(
    "/pending",
    response_model=PendingListOut,
    summary="待确认事项列表（手机端轮询这个）",
)
def list_pending(include_expired: bool = False) -> PendingListOut:
    """列出所有停在人工确认节点上的事项。

    `include_expired=True` 时连已过期的也返回（便于排查"为什么没弹出来"）。
    """
    items: list[InterventionOut] = []
    for record in list_pending_threads():
        payload = record.get("payload") or {}
        for item in payload.get("interventions") or []:
            expires = item.get("expires_at")
            if _expired(expires) and not include_expired:
                continue
            items.append(
                InterventionOut(
                    thread_id=record["thread_id"],
                    kind=item.get("kind") or "high_risk",
                    device_code=item.get("device_code"),
                    device_name=item.get("device_name") or "",
                    capability=item.get("capability"),
                    value=item.get("value"),
                    prompt=item.get("prompt") or "",
                    detail=item.get("detail") or "",
                    options=list(item.get("options") or ["approve", "reject"]),
                    expires_at=expires,
                    created_at=record.get("created_at"),
                    question=record.get("question"),
                )
            )

    # 过期时间近的排前面，让人先处理最急的
    items.sort(key=lambda i: (i.expires_at or "9999"))
    return PendingListOut(items=items, total=len(items))


@router.post(
    "/{thread_id}",
    response_model=DecisionOut,
    summary="对某条待确认事项给出决定（批准 / 驳回）",
)
async def decide(
    thread_id: str,
    body: DecisionIn,
    ctx: ToolContext = Depends(get_tool_context),
) -> DecisionOut:
    """给出决定并恢复被中断的对话。

    返回值里 `accepted=False` 表示这个决定**没有被采纳**（例如已过期），
    此时对话仍然停在原地，用户不会看到"点一下什么也没发生"的困惑。
    """
    record = get_pending(thread_id)
    if record is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="该会话没有待确认事项（可能已处理，或它本来就没有高风险动作）",
        )

    payload = record.get("payload") or {}
    expires_at = payload.get("expires_at")
    if _expired(expires_at):
        clear_pending(thread_id)
        log.info("确认已过期，自动作废 thread=%s", thread_id)
        return DecisionOut(
            thread_id=thread_id,
            accepted=False,
            choice=body.choice,
            message="该确认已超过有效期，已自动作废。请重新发起请求。",
        )

    items = payload.get("interventions") or []
    if body.intervention_index >= len(items):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"intervention_index 越界：本轮共有 {len(items)} 条待确认",
        )

    target = items[body.intervention_index]
    log.info(
        "收到人工决定 thread=%s index=%s choice=%s 目标=%s",
        thread_id, body.intervention_index, body.choice, target.get("device_name"),
    )

    try:
        out = await resume(thread_id, [body.model_dump()], ctx=ctx)
    except Exception as e:  # noqa: BLE001
        log.exception("恢复对话失败 thread=%s", thread_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"恢复对话失败：{type(e).__name__}: {e}",
        ) from e

    approved = body.choice.strip().lower() in {"approve", "approved", "yes", "ok", "同意", "确认", "批准"}
    if approved:
        msg = (
            f"已确认「{target.get('device_name')} {target.get('capability')}」。"
            "⚠️ 高风险设备不会自动执行 —— 本次确认已留痕，实际动作需人工或授权通道完成。"
        )
    else:
        msg = f"已驳回「{target.get('device_name')} {target.get('capability')}」，该动作不会执行。"

    remaining = len(list_pending_threads())
    return DecisionOut(
        thread_id=thread_id,
        accepted=True,
        choice=body.choice,
        message=msg,
        answer=out.get("answer"),
        run_result=out.get("run_result"),
        remaining=remaining if out.get("interrupted") else 0,
    )
