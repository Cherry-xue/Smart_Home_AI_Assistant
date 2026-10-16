"""场景执行器。

一条动作只有三种归宿
------------------
| 情况 | 处理 | 落库 |
|---|---|---|
| **高风险设备**（门锁 / 燃气阀 / 总闸） | 只生成待确认建议，**绝不执行** | `device_commands.status = pending_confirm` |
| 普通设备 | 发 MQTT 指令 | `device_commands.status = executed` |
| 发送失败（重试后仍失败） | 记录失败，**继续执行其余动作** | `device_commands.status = failed` |

关于"失败重试与降级"
------------------
单步失败**不中断整场**：看电影模式里"音箱没连上"不该导致"灯也不开了"。
失败的动作记进 `scene_runs.failed_steps`，整体状态标 `partial` ——
这是规划书 §19"执行（含失败重试降级）"的落点。

⚠️ 但有一条**不能降级**：高风险设备。
网络再差、重试再多，门锁也不会因此被"顺手打开"。风险判断在**发送之前**完成，
而且失败重试只发生在普通设备的发送环节。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time

from sqlalchemy.orm import Session

from app.db.catalog import DeviceCatalog, DeviceInfo
from app.models.orm import DeviceCommand, SceneRun
from app.mqtt import publisher as mqtt_publisher
from app.services.scenes.models import RunResult, ScenePlan, StepOutcome

log = logging.getLogger("app.services.scenes.executor")

#: 单条指令最多尝试几次
MAX_ATTEMPTS = 3
#: 重试间隔（秒）
RETRY_DELAY_SEC = 0.4


def create_command(
    db: Session,
    device: DeviceInfo,
    outcome: StepOutcome,
    *,
    status: str,
    risk_level: str,
    result_message: str | None = None,
    issued_by: str | None = None,
    extra_params: dict | None = None,
) -> DeviceCommand:
    """写一条指令记录。

    ⚠️ 高风险指令的 `status` **永远只会是 `pending_confirm`** ——
    这不靠调用方自觉：下面这行会把任何试图给高风险设备写"已执行"的尝试掰回来。
    数据库那一层还有默认值 `pending_confirm` 作为第二道保险。

    `extra_params` 会合并进 `params_json`（例如 Agent 调用时带上 `thread_id`，
    这样"待确认列表"能反查到是哪个会话挂起的）。
    """
    if device.high_risk and status != "pending_confirm":
        log.warning(
            "拦截：高风险设备 %s 的指令状态被强制改为 pending_confirm（原为 %s）",
            device.code, status,
        )
        status = "pending_confirm"
        result_message = "高风险设备：必须人工确认后执行"

    params: dict = {
        "value": outcome.value,
        "original_value": outcome.original_value,
        "source": "scene",
    }
    if extra_params:
        params.update(extra_params)

    now = dt.datetime.now()
    record = DeviceCommand(
        device_id=device.id,
        command=outcome.capability,
        params_json=params,
        issued_by=issued_by,
        risk_level=risk_level,
        status=status,
        executed_at=now if status == "executed" else None,
        result_message=result_message,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _send_with_retry(
    device: DeviceInfo,
    outcome: StepOutcome,
    *,
    home_id: int,
    command_id: int | None,
    issued_by: str,
) -> tuple[bool, str | None, int]:
    """带重试地下发一条指令。返回 (是否成功, 错误信息, 尝试次数)。"""
    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            mqtt_publisher.publish_command(
                home_id=home_id,
                room_code=device.room_code,
                device_code=device.code,
                capability=outcome.capability,
                value=outcome.value,
                command_id=command_id,
                issued_by=issued_by,
            )
            return True, None, attempt
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"
            log.warning(
                "下发失败（第 %d/%d 次）：%s.%s —— %s",
                attempt, MAX_ATTEMPTS, device.code, outcome.capability, last_error,
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SEC)
    return False, last_error, MAX_ATTEMPTS


def execute_plan(
    plan: ScenePlan,
    catalog: DeviceCatalog,
    *,
    db: Session | None,
    home_id: int,
    issued_by: str = "scene",
    dry_run: bool = False,
    extra_params: dict | None = None,
) -> RunResult:
    """执行编排好的计划。

    - `dry_run=True`：不发 MQTT、不写指令记录，只走一遍流程返回"会发生什么"（用于预览确认）。
      此时 `db` 可以为 `None`。
    - `dry_run=False`：`db` 必填（指令记录本身就是审计留痕，**失败也要留**）。
    """
    if not dry_run and db is None:
        raise ValueError("execute_plan(dry_run=False) 需要传入 db 会话（指令记录要落库）")

    result = RunResult(status="running", started_at=dt.datetime.now())
    if dry_run:
        result.notes.append("dry-run：未下发任何指令，也未写入指令记录")

    for outcome in plan.actions:
        device = catalog.get(outcome.device_code)
        if device is None:
            # planner 已经校验过设备存在，走到这里说明目录在编排后被刷新过
            outcome.status = "failed"
            outcome.error = "设备目录中已不存在该设备"
            result.failed.append(outcome)
            continue

        # ---------- 高风险：只生成待确认建议 ----------
        if outcome.high_risk_device:
            if dry_run:
                outcome.status = "pending_confirm"
                outcome.reason = (outcome.reason + "；" if outcome.reason else "") + "（dry-run 未写库）"
            else:
                record = create_command(
                    db, device, outcome,
                    status="pending_confirm",
                    risk_level="high",
                    result_message="高风险设备：需授权成员二次确认后执行",
                    issued_by=issued_by,
                    extra_params=extra_params,
                )
                outcome.command_id = record.id
                outcome.status = "pending_confirm"
            result.pending_confirm.append(outcome)
            continue

        # ---------- 普通设备：直接执行 ----------
        risk_level = outcome.risk_level if outcome.risk_level in ("low", "medium") else "low"
        if dry_run:
            outcome.status = "executed"
            outcome.error = None
            result.executed.append(outcome)
            continue

        # 先写记录拿到 command_id（指令记录本身就是审计留痕，失败也要留）
        record = create_command(
            db, device, outcome,
            status="executed",
            risk_level=risk_level,
            result_message=None,
            issued_by=issued_by,
            extra_params=extra_params,
        )
        outcome.command_id = record.id

        ok, error, attempts = _send_with_retry(
            device, outcome, home_id=home_id, command_id=record.id, issued_by=issued_by
        )
        outcome.attempts = attempts
        outcome.error = error

        if ok:
            outcome.status = "executed"
            result.executed.append(outcome)
        else:
            outcome.status = "failed"
            record.status = "failed"
            record.result_message = f"下发失败（重试 {attempts} 次）：{error}"
            db.commit()
            result.failed.append(outcome)

    # ---------- 汇总 ----------
    result.finished_at = dt.datetime.now()
    done = len(result.executed) + len(result.pending_confirm)
    if result.failed and done:
        result.status = "partial"
        result.fallback_used = True
        result.notes.append(
            f"降级执行：{len(result.failed)} 条下发失败，其余 {done} 条已处理"
            f"（单步失败不中断整场）"
        )
    elif result.failed and not done:
        result.status = "failed"
        result.notes.append("全部动作下发失败 —— 请检查 MQTT broker 是否在运行")
    else:
        result.status = "success"

    if result.pending_confirm:
        result.notes.append(
            f"{len(result.pending_confirm)} 条涉及高风险设备，已生成待确认建议（不会自动执行）"
        )
    return result


def save_run(
    db: Session,
    *,
    scene_id: int | None,
    result: RunResult,
    plan: ScenePlan | None = None,
) -> SceneRun:
    """把一次执行写进 `scene_runs`（含被剔除的步骤与失败步骤，便于复盘）。"""
    steps_json = {
        "executed": [s.as_dict() for s in result.executed],
        "pending_confirm": [s.as_dict() for s in result.pending_confirm],
        "failed": [s.as_dict() for s in result.failed],
    }
    if plan is not None:
        steps_json["rejected"] = [s.as_dict() for s in plan.rejected]
        steps_json["intent"] = plan.intent_key

    failed_steps = [
        {
            "device_code": s.device_code,
            "device_name": s.device_name,
            "capability": s.capability,
            "value": s.value,
            "attempts": s.attempts,
            "error": s.error,
        }
        for s in result.failed
    ]

    run = SceneRun(
        scene_id=scene_id,
        started_at=result.started_at,
        finished_at=result.finished_at,
        steps_json=steps_json,
        failed_steps=failed_steps or None,
        fallback_used=result.fallback_used,
        status=result.status,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    result.run_id = run.id
    return run
