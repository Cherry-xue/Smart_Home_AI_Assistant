"""单台设备的手动控制（M6）。

为什么单独一个模块
------------------
前端"器件开关"需要一个"直接控制一台设备"的入口。后端原本只有两条控制通道
（多 Agent 对话 `/api/chat`、场景执行 `/api/scenes/{id}/run`），没有第三条。

⚠️⚠️ 实现上**必须复用** `services.scenes.executor.execute_plan()`，不许另写下发逻辑。
原因：那条链路里有两道项目红线，重写一次就等于把它们绕过去了：

1. `executor.create_command()` 内部会**强制**把高风险设备的指令状态改写成
   `pending_confirm`（`if device.high_risk and status != "pending_confirm"`），
   数据库 `device_commands.status` 的默认值 `pending_confirm` 是第二道保险；
2. 指令记录是审计留痕，**失败也要留**（先写记录拿 command_id，再下发）。

所以这里的做法是：把这一条动作**包装成一个只有一条动作的 `ScenePlan`** 丢给
`execute_plan`。校验（设备存在 / 能力存在 / 可写 / 值在范围内）在这一层做，
执行与留痕全部交给 executor。这样"单台控制"和"场景控制"走的是同一条路，
不会出现"场景里高风险的被拦住了、大屏上点一下却执行了"这种漏洞。

关于措辞
--------
返回体的 `status` 只说明**指令**走到了哪一步；
`acknowledged` 才说明**设备**有没有回报。二者必须分开表达 ——
本项目红线：**只有真的改了值，才说自己改了值**。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from app.db.catalog import CapabilityRange, DeviceCatalog, DeviceInfo
from app.models.orm import DeviceCommand
from app.mqtt.cache import LatestValueCache
from app.schemas.command import CommandListOut, CommandRecordOut, DeviceCommandOut
from app.services.scenes.executor import execute_plan
from app.services.scenes.models import ScenePlan, StepOutcome

log = logging.getLogger("app.services.device_control")

#: 能力里的"开/关"字面量，允许前端传这些等价写法
_TRUE_WORDS = {"true", "1", "on", "open", "yes", "开", "开启", "打开"}
_FALSE_WORDS = {"false", "0", "off", "close", "no", "关", "关闭", "关上"}

#: cmd_ack 回执在 result_message 里的标记（由 `mqtt/subscriber.py` 写入）
ACK_MARK = "[回执]"

#: 手动下发产生的待确认项的 thread_id 前缀：`manual-cmd-12` = 指令 12 在等人批准。
#: ⚠️ 这个前缀是**判定依据** —— `/api/confirmations/{thread_id}` 收到它就下发真实指令，
#:    收到别的就去恢复 Agent 会话。两者不能混。
MANUAL_PENDING_PREFIX = "manual-cmd-"


class DeviceCommandError(Exception):
    """校验不通过。带 HTTP 状态码，交给 API 层直接抛。"""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ============================================================ 校验
def _coerce_value(cap: CapabilityRange, raw: Any) -> Any:
    """把请求里的值规整成能力档案声明的类型；不合法就抛 `DeviceCommandError`。

    规则刻意"**严格**"：越界的值直接拒绝，而不是悄悄夹到边界。
    理由：这是**用户明确指定的目标值**，不是 LLM 编排出来的建议值。
    前者被悄悄改掉会让人以为设置生效了（而实际设成了别的数），后者夹取是合理的。
    → 场景编排（planner）夹取；手动控制（这里）拒绝。两者的区别是有意的。
    """
    vt = (cap.value_type or "").lower()

    if vt == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in _TRUE_WORDS:
                return True
            if s in _FALSE_WORDS:
                return False
        raise DeviceCommandError(
            f"能力「{cap.display_name}」是开关型，值只能是 true / false，收到 {raw!r}"
        )

    if vt in ("int", "float"):
        num: float
        if isinstance(raw, bool):
            raise DeviceCommandError(f"能力「{cap.display_name}」需要数值，收到布尔值 {raw!r}")
        if isinstance(raw, (int, float)):
            num = float(raw)
        elif isinstance(raw, str):
            try:
                num = float(raw.strip())
            except ValueError as e:
                raise DeviceCommandError(
                    f"能力「{cap.display_name}」需要数值，无法解析 {raw!r}"
                ) from e
        else:
            raise DeviceCommandError(f"能力「{cap.display_name}」需要数值，收到 {raw!r}")

        if vt == "int":
            if abs(num - round(num)) > 1e-9:
                raise DeviceCommandError(
                    f"能力「{cap.display_name}」是整数型，不接受小数 {num}"
                )
            num = round(num)

        if not cap.contains(num):
            rng = f"{cap.value_min:g} ~ {cap.value_max:g}"
            if cap.unit:
                rng += f" {cap.unit}"
            raise DeviceCommandError(
                f"能力「{cap.display_name}」的目标值 {num:g} 超出许可范围（{rng}）。"
                "手动控制不做自动夹取 —— 请直接给范围内的值"
            )
        return int(num) if vt == "int" else num

    if vt == "enum":
        options = list(cap.enum_values or ())
        if not options:
            raise DeviceCommandError(f"能力「{cap.display_name}」声明为枚举但没有可选值")
        if not isinstance(raw, str):
            raise DeviceCommandError(
                f"能力「{cap.display_name}」是枚举型，值必须是字符串，收到 {raw!r}"
            )
        s = raw.strip()
        for opt in options:
            if opt.lower() == s.lower():
                return opt
        raise DeviceCommandError(
            f"能力「{cap.display_name}」不接受 {raw!r}，可选：{'/'.join(options)}"
        )

    # 未知类型：放行但不做类型断言（避免把没见过的能力卡死）
    log.warning("能力 %s 的 value_type=%r 未登记，跳过类型校验", cap.code, cap.value_type)
    return raw


def _require_writable(device: DeviceInfo, code: str) -> CapabilityRange:
    cap = device.cap(code)
    if cap is None:
        available = "、".join(sorted(device.capabilities)) or "（无）"
        raise DeviceCommandError(
            f"设备「{device.name}」没有能力 {code!r}。它有：{available}",
            status_code=404,
        )
    if not cap.writable:
        raise DeviceCommandError(
            f"能力「{cap.display_name}」（{code}）是只读的，不能下发指令。"
            "传感器类读数不可能被写 —— 检查是不是把「观测值」当成「可控项」了"
        )
    return cap


# ============================================================ 执行
def _register_manual_pending(
    *,
    thread_id: str,
    device: DeviceInfo,
    cap: CapabilityRange,
    value: Any,
    command_id: int,
    note: str | None,
) -> None:
    """把手动控制产生的高风险指令登记进待确认索引。

    ⚠️ 只写 `device_commands` 是不够的：顶栏「消息」抽屉和手机端的待确认弹窗
    读的都是 `runner._PENDING`。不登记的话，用户会看到"已生成待确认项"，
    去「消息」里却什么都没有 —— 这正是被当成 bug 的那个现象。

    载荷结构与 Agent 中断时**完全一致**，这样列表接口和前端卡片不需要两套渲染。
    """
    from app.agent.runner import register_pending

    register_pending(
        thread_id,
        {
            "interventions": [
                {
                    "kind": "high_risk",
                    "device_code": device.code,
                    "device_name": device.name,
                    "capability": cap.code,
                    "value": value,
                    # ⚠️ 不要拼 room_name：设备名本身已经带房间（"厨房燃气阀"），
                    #    再拼一次就成了"厨房厨房燃气阀"。
                    "prompt": f"是否允许 {device.name} 执行「{cap.display_name} = {value}」？",
                    "detail": note or "该设备属于高风险设备，必须人工批准后才会下发。",
                    "options": ["approve", "reject"],
                    "command_id": command_id,
                    # 手动待办不设过期：人什么时候看到就什么时候处理。
                    "expires_at": None,
                }
            ],
            "expires_at": None,
            "message": "手动控制：1 个高风险动作需要你确认",
        },
        extra={"home_id": device.home_id, "question": note or "手动控制"},
    )


def _expire_stale_manual(
    db: Session, *, device_id: int, capability: str, keep_id: int
) -> list[int]:
    """把同一设备、同一能力下**更早的手动待确认项**标为作废（保留 `keep_id`）。

    没有这一步的话，用户在面板上连点三次"解锁"就会攒出三张一模一样的待办；
    更糟的是批准其中任意一张都会真的下发一次 —— 等于重复开锁。

    ⚠️ 库里作废还不够：待确认项同时登记在**进程内存** `_PENDING` 里，
    只改库的结果是"库里只剩一条、接口还列三条"。所以这里把作废掉的
    command_id 一并返回，调用方负责把内存里那几条也摘掉，两边必须同步。
    """
    stale = [
        int(r[0])
        for r in db.execute(
            text(
                """
                SELECT id
                  FROM device_commands
                 WHERE device_id = :d
                   AND command = :c
                   AND status = 'pending_confirm'
                   AND id < :keep
                   AND JSON_UNQUOTE(JSON_EXTRACT(params_json, '$.source')) = 'dashboard'
                """
            ),
            {"d": device_id, "c": capability, "keep": keep_id},
        ).fetchall()
    ]
    if not stale:
        return []
    db.execute(
        text(
            """
            UPDATE device_commands
               SET status = 'expired',
                   result_message = '被同一设备的新指令取代，自动作废'
             WHERE id IN :ids
            """
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": stale},
    )
    db.commit()
    return stale


def run_command(
    db: Session,
    catalog: DeviceCatalog,
    cache: LatestValueCache,
    *,
    device: DeviceInfo,
    capability: str,
    raw_value: Any,
    issued_by: str | None = None,
    note: str | None = None,
) -> DeviceCommandOut:
    """校验 + 下发一条手动指令。校验失败抛 `DeviceCommandError`。"""
    cap = _require_writable(device, capability)
    value = _coerce_value(cap, raw_value)

    previous = cache.value_of(device.code, capability, None)
    sender = (issued_by or "dashboard").strip() or "dashboard"

    outcome = StepOutcome(
        device_code=device.code,
        capability=capability,
        value=value,
        device_id=device.id,
        device_name=device.name,
        room_name=device.room_name,
        original_value=previous,
        status="ok",
        risk_level=cap.risk_level or "low",
        high_risk_device=device.high_risk,
        reason=note or "",
    )

    # ⚠️ 包成"只有一条动作的场景计划"，复用同一条执行链路（含红线与留痕）
    plan = ScenePlan(
        intent_key="manual",
        intent_label="手动控制",
        raw_count=1,
        actions=[outcome],
    )
    result = execute_plan(
        plan,
        catalog,
        db=db,
        home_id=device.home_id,
        issued_by=sender,
        extra_params={"source": "dashboard", "note": note} if note else {"source": "dashboard"},
    )

    executed = result.executed[0] if result.executed else None
    pending = result.pending_confirm[0] if result.pending_confirm else None
    failed = result.failed[0] if result.failed else None
    final = executed or pending or failed or outcome

    record = db.get(DeviceCommand, final.command_id) if final.command_id else None
    ack = bool(record and record.result_message and ACK_MARK in record.result_message)

    # ---- 消息措辞：只说做过的事 ----
    if pending is not None:
        # ⚠️ 只写库是不够的：顶栏「消息」抽屉读的是 `_PENDING` 索引，
        #    不登记的话界面上只会得到一句"已生成待确认项"，却**找不到那条待办**
        #    —— 用户看到的就是"提示说有，消息里没有"。
        #    thread_id 用 `manual-cmd-{id}` 前缀，批准时靠它区分"手动控制"与"Agent 会话"。
        if final.command_id:
            # 连点三次"解锁"不该攒三张待办：更早的手动待确认项直接作废。
            # ⚠️ 只动 source=dashboard 的行 —— Agent 的待确认有它自己的中断索引，
            #    在这里改状态会让"库里有、内存里没有"两边对不上。
            stale_ids = _expire_stale_manual(
                db, device_id=device.id, capability=capability, keep_id=final.command_id
            )
            # 库里作废了、内存里还挂着 → 接口会列出来但批准时又会被拦，
            # 用户看到的就是"一堆点不动的待办"。两边必须一起清。
            if stale_ids:
                from app.agent.runner import clear_pending

                for cid in stale_ids:
                    clear_pending(f"{MANUAL_PENDING_PREFIX}{cid}")
            _register_manual_pending(
                thread_id=f"{MANUAL_PENDING_PREFIX}{final.command_id}",
                device=device,
                cap=cap,
                value=value,
                command_id=final.command_id,
                note=note,
            )
        else:  # pragma: no cover - 落库失败时 command_id 才会为空
            log.warning("高风险指令没有拿到 command_id，无法登记待确认：%s", device.code)
        message = (
            f"「{device.name} {cap.display_name}」属于高风险设备，已生成**待确认项**，"
            "不会自动执行。请到「消息」里人工批准 —— 批准后会由系统下发，驳回则作废。"
        )
        status = "pending_confirm"
    elif executed is not None:
        message = (
            f"已向「{device.name}」下发 {cap.display_name} = {value}，"
            "指令已写入记录；**设备状态要等它回报后才算变**。"
        )
        status = "executed"
    else:
        message = f"下发失败：{final.error or '未知原因'}（已重试 {final.attempts} 次）"
        status = "failed"

    log.info(
        "手动控制 device=%s cap=%s value=%r → %s（command_id=%s）",
        device.code, capability, value, status, final.command_id,
    )

    return DeviceCommandOut(
        device_id=device.id,
        device_code=device.code,
        device_name=device.name,
        room_name=device.room_name,
        capability=capability,
        capability_label=cap.display_name,
        value_type=cap.value_type,
        unit=cap.unit,
        requested_value=raw_value,
        applied_value=value,
        previous_value=previous,
        status=status,
        risk_level=cap.risk_level or "low",
        high_risk=device.high_risk,
        command_id=final.command_id,
        attempts=final.attempts,
        acknowledged=ack,
        message=message,
    )


# ============================================================ 查询
def list_commands(
    db: Session, device: DeviceInfo, *, limit: int = 20
) -> CommandListOut:
    """某台设备的指令记录（新的在前）。`acknowledged` 由回执标记推出。"""
    stmt = (
        select(DeviceCommand)
        .where(DeviceCommand.device_id == device.id)
        .order_by(DeviceCommand.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    rows = list(db.execute(stmt).scalars())

    items = [
        CommandRecordOut(
            id=r.id,
            device_id=r.device_id,
            capability=r.command,
            params=r.params_json,
            issued_by=r.issued_by,
            risk_level=r.risk_level,
            status=r.status,
            acknowledged=bool(r.result_message and ACK_MARK in r.result_message),
            executed_at=r.executed_at,
            created_at=r.created_at,
            result_message=r.result_message,
        )
        for r in rows
    ]

    total_stmt = select(DeviceCommand.id).where(DeviceCommand.device_id == device.id)
    total = len(list(db.execute(total_stmt).scalars()))

    return CommandListOut(
        device_id=device.id,
        device_code=device.code,
        device_name=device.name,
        items=items,
        total=total,
    )


# ============================================================ 小工具
def summarize_ack(message: str | None) -> str:
    """从 `result_message` 里把回执正文抠出来，给界面显示用。

    `subscriber` 写进去的格式是 `[回执] 设备已确认：✅power_on=false（…）`，
    这里只保留冒号后面的部分；没有回执就返回空串（界面显示"等待回报"）。
    """
    if not message:
        return ""
    if ACK_MARK not in message:
        return ""
    tail = message.split(ACK_MARK, 1)[1].strip()
    tail = re.sub(r"^设备已确认[:：]?\s*", "", tail)
    return tail.strip()


def now_local() -> dt.datetime:
    return dt.datetime.now()
