"""人工确认端点。

对应规划书 §10.3 的 `/api/confirmations`。手机 App 的"高风险弹窗"就打这两个接口：

```
GET  /api/confirmations/pending        列出当前所有待确认事项
POST /api/confirmations/{thread_id}    对某条事项给出决定
```

关于"批准之后会发生什么"
------------------------
本项目红线：**门锁、燃气阀、电气总闸、布防撤防绝不自动执行**
（`HIGH_RISK_AUTO_EXEC=false`）。

但"不自动执行" ≠ "批了也不执行"。本接口就是那个**人工授权通道**：
批准 → `executor.dispatch_confirmed()` 真的下发一次；驳回 → 作废。
自动路径（场景执行、Agent 编排）永远走不到 `dispatch_confirmed`
—— `execute_plan` 遇到高风险动作直接 `continue`，`create_command` 还会把
状态强制改回 `pending_confirm`。所以红线准确地说是：
**高风险动作只能人工授权后执行，绝不自动执行。**

状态留痕：`pending_confirm → confirmed → executed`（驳回为 `rejected`），
中间那次 `confirmed` 是为了让"人批了但没发出去"能在库里看出来。
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from starlette.concurrency import run_in_threadpool

from app.agent.runner import clear_pending, get_pending, list_pending_threads, resume
from app.api.deps import get_catalog, get_tool_context
from app.schemas.confirmation import DecisionIn, DecisionOut, InterventionOut, PendingListOut
from app.services import chat_history
from app.tools import ToolContext

log = logging.getLogger("app.api.confirmations")

router = APIRouter(prefix="/confirmations", tags=["confirmations"])


#: 视为"同意"的字面量（前端传 approve，手机端可能传中文）
_APPROVE_TOKENS = {"approve", "approved", "yes", "ok", "同意", "确认", "批准"}


def _decide_manual(thread_id: str, body: DecisionIn, catalog) -> DecisionOut:
    """处理**手动控制**留下的高风险待确认项：批准 → 真的下发，驳回 → 作废。

    ⚠️ 这是全链路仅有的两处"允许高风险设备动起来"的入口之一
    （另一处是 Agent 的 `_settle_high_risk`）。区别很重要：
    **自动路径永远走不到这里** —— 只有人在待确认列表里点了批准才会执行。
    红线因此从"永不执行"精确成"**绝不自动执行，人工授权后才执行**"。
    """
    from app.db.session import SessionLocal
    from app.models.orm import DeviceCommand
    from app.services.device_control import MANUAL_PENDING_PREFIX
    from app.services.scenes.executor import dispatch_confirmed

    try:
        command_id = int(thread_id[len(MANUAL_PENDING_PREFIX):])
    except (TypeError, ValueError):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="待确认编号不合法"
        ) from None

    db = SessionLocal()
    try:
        row = db.get(DeviceCommand, command_id)
        if row is None:
            clear_pending(thread_id)
            return DecisionOut(
                thread_id=thread_id, accepted=False, choice=body.choice,
                message="这条指令记录已经不存在了，待确认项已作废。",
            )

        # 已经处理过的别再执行一次（重复点批准不能变成"开两次门"）
        if row.status not in ("pending_confirm", "confirmed"):
            clear_pending(thread_id)
            return DecisionOut(
                thread_id=thread_id, accepted=False, choice=body.choice,
                message=f"这条指令已经处理过了（当前状态：{row.status}），不需要重复确认。",
            )

        device = next((d for d in catalog.all() if d.id == row.device_id), None)
        if device is None:
            clear_pending(thread_id)
            return DecisionOut(
                thread_id=thread_id, accepted=False, choice=body.choice,
                message="设备已不在设备目录里，待确认项已作废。",
            )

        cap = device.cap(row.command)
        label = cap.display_name if cap else row.command
        name = f"{device.name} {label}"
        approved = body.choice.strip().lower() in _APPROVE_TOKENS

        if not approved:
            row.status = "rejected"
            row.confirmed_at = dt.datetime.now()
            row.result_message = "人工驳回，未下发"
            db.commit()
            clear_pending(thread_id)
            log.info("手动高风险指令被驳回 command_id=%s", command_id)
            return DecisionOut(
                thread_id=thread_id, accepted=True, choice=body.choice,
                message=f"已驳回「{name}」，该动作不会执行。",
            )

        value = (row.params_json or {}).get("value")
        ok, err = dispatch_confirmed(
            db, device, command_id=row.id, capability=row.command, value=value
        )
        clear_pending(thread_id)
        if ok:
            log.info("手动高风险指令已批准并下发 command_id=%s", command_id)
            return DecisionOut(
                thread_id=thread_id, accepted=True, choice=body.choice,
                message=(
                    f"已批准「{name}」，指令已下发；**设备状态要等回报后才算变**。"
                    "若 12 秒内没有回执，检查模拟器 / MQTT 是否在运行。"
                ),
            )
        return DecisionOut(
            thread_id=thread_id, accepted=True, choice=body.choice,
            message=f"已批准「{name}」，但下发失败：{err}（已记为 failed，可重试）",
        )
    finally:
        db.close()


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        return dt.datetime.fromisoformat(expires_at) < dt.datetime.now()
    except ValueError:
        return False


def _manual_pending(catalog) -> list[InterventionOut]:
    """从数据库里捞"手动控制留下、还没人处理"的高风险指令。

    ⚠️ 为什么需要这一步：`_PENDING` 是**进程内存**。后端一重启，
    手动下发的待确认项就再也弹不出来了，而 `device_commands` 里那行还挂着
    `pending_confirm` —— 等于一条永远处理不了的待办。库里才是持久事实，
    内存只当"额外补充"。
    """
    from app.db.session import SessionLocal
    from app.models.orm import DeviceCommand
    from app.services.device_control import MANUAL_PENDING_PREFIX

    out: list[InterventionOut] = []
    db = SessionLocal()
    try:
        # 只捞最近 24 小时：更早的待办没有现实意义，翻出来只会让人以为系统在抽风
        cutoff = dt.datetime.now() - dt.timedelta(hours=24)
        rows = (
            db.query(DeviceCommand)
            .filter(
                DeviceCommand.status == "pending_confirm",
                DeviceCommand.created_at >= cutoff,
            )
            .order_by(DeviceCommand.id.desc())
            .limit(50)
            .all()
        )
        by_id = {d.id: d for d in catalog.all()}
        seen_keys: set[tuple[int, str]] = set()
        for row in rows:
            params = row.params_json or {}
            # 只认手动控制产生的（Agent / 场景另有自己的待确认通道，重复展示会成两条待办）
            if params.get("source") != "dashboard":
                continue
            # 同一设备同一能力只留最新一条（历史数据里可能攒了多次点击）
            key = (row.device_id, row.command)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            device = by_id.get(row.device_id)
            if device is None:
                continue
            cap = device.cap(row.command)
            label = cap.display_name if cap else row.command
            out.append(
                InterventionOut(
                    thread_id=f"{MANUAL_PENDING_PREFIX}{row.id}",
                    kind="high_risk",
                    device_code=device.code,
                    device_name=device.name,
                    capability=row.command,
                    value=params.get("value"),
                    # 设备名已带房间前缀，不要再拼 room_name（否则"厨房厨房燃气阀"）
                    prompt=f"是否允许 {device.name} 执行「{label} = {params.get('value')}」？",
                    detail="该设备属于高风险设备，必须人工批准后才会下发。",
                    options=["approve", "reject"],
                    expires_at=None,
                    created_at=row.created_at,
                    question=params.get("note") or "手动控制",
                    home_id=device.home_id,
                )
            )
    except Exception:  # noqa: BLE001
        log.exception("从库里补捞手动待确认项失败")
    finally:
        db.close()
    return out


@router.get(
    "/pending",
    response_model=PendingListOut,
    summary="待确认事项列表（手机端轮询这个）",
)
def list_pending(
    include_expired: bool = False,
    catalog=Depends(get_catalog),
) -> PendingListOut:
    """列出所有停在人工确认节点上的事项。

    `include_expired=True` 时连已过期的也返回（便于排查"为什么没弹出来"）。
    """
    items: list[InterventionOut] = []
    seen: set[str] = set()
    for record in list_pending_threads():
        payload = record.get("payload") or {}
        for item in payload.get("interventions") or []:
            expires = item.get("expires_at")
            if _expired(expires) and not include_expired:
                continue
            seen.add(record["thread_id"])
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
                    home_id=record.get("home_id"),
                )
            )

    # 内存里没有的，从库里补（后端重启过就只能靠这一步）
    items.extend(i for i in _manual_pending(catalog) if i.thread_id not in seen)

    # 过期时间近的排前面，让人先处理最急的；不过期的（手动待办）排前面
    items.sort(key=lambda i: (i.expires_at or "0000"))
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
    # 手动控制产生的待确认项：不恢复任何会话，直接走"人工授权"下发通道
    from app.services.device_control import MANUAL_PENDING_PREFIX

    if thread_id.startswith(MANUAL_PENDING_PREFIX):
        return await run_in_threadpool(
            _decide_manual, thread_id, body, ctx.catalog
        )

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

    approved = body.choice.strip().lower() in _APPROVE_TOKENS
    if approved:
        rr = out.get("run_result") or {}
        hr_done = [
            s for s in rr.get("executed", [])
            if s.get("device_code") == target.get("device_code")
        ]
        msg = (
            f"已批准并下发「{target.get('device_name')} {target.get('capability')}」，"
            "设备状态以回执为准（回报之后界面才会变）。"
            if hr_done
            else f"已批准「{target.get('device_name')} {target.get('capability')}」，"
                 "但下发没有成功，详见执行记录。"
        )
    else:
        msg = f"已驳回「{target.get('device_name')} {target.get('capability')}」，该动作不会执行。"

    # 历史会话里也要留下这一轮：否则重新打开会话时，只能看到"等你确认"，
    # 看不到你后来到底批了还是驳了。落在 assistant 侧，与前端追加的那条气泡一致。
    await run_in_threadpool(
        chat_history.record_message,
        thread_id,
        "assistant",
        f"{'已批准' if approved else '已驳回'} → {out.get('answer') or msg}",
    )

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
