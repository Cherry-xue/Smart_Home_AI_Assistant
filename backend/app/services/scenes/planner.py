"""编排器：把候选动作变成**可执行计划**。

流程（顺序有意义，不能换）
------------------------
1. **能力校验**（最硬的一层）：设备存在？能力登记过？可写？值类型/范围/枚举合法？
   不过就剔除 —— 这是"防臆造设备功能"的落点，也是 M3 验收里的"先查能力档案"。
2. **重复检测**：同一 (设备, 能力) 出现多次 → **保留最后一条**（后写覆盖先写，
   符合"用户后来改了主意"的直觉），其余记为 `duplicate` 并说明。
3. **意图约束**：按场景意图模板检查（如观影照明 ≤ 40%）→ `reject` / `clamp` / `warn`。
4. **风险标记**：高风险设备（门锁 / 燃气阀 / 总闸）**不剔除**，只标记为待确认项 ——
   剔除会让用户误以为"系统不支持"，而事实是"支持，但需要你确认"。

为什么这个顺序
------------
- **先校验再判重**：否则会拿"根本不存在的设备"去互相比较；
- **判重放在意图约束前**：先确定"最终想设成什么值"，再看它是否符合场景意图。
  反过来会出现"两个值都被意图拒绝、但其实其中一个 clamp 之后是合法的"这种绕圈情况。
"""

from __future__ import annotations

from typing import Any

from app.db.catalog import CapabilityRange, DeviceCatalog
from app.services.scenes.intents import SceneIntent, match_intent
from app.services.scenes.models import ScenePlan, SceneStep, StepOutcome

#: 单次编排的动作数上限（防 LLM 吐出几百条把设备和库都压垮）
MAX_ACTIONS = 40

_TRUE_TOKENS = {"true", "1", "on", "open", "yes", "开", "打开", "是"}
_FALSE_TOKENS = {"false", "0", "off", "close", "no", "关", "关闭", "否"}


# ---------------------------------------------------------------- 值处理
def _coerce_value(cap: CapabilityRange, raw: Any) -> tuple[Any, str | None, bool]:
    """把原始值转成符合能力档案的类型，并夹到声明的范围内。

    返回 `(值, 错误, 是否发生了范围夹取)`。

    ⚠️ 第三个返回值**只表示"数值被范围夹取"**，不包括类型转换。
    这点很关键：把中文"开"解析成 `True` 是**类型转换**（值本身没变），
    如果也算成"夹取"，界面上就会出现"音箱开关被夹取为 True"这种莫名其妙的提示 ——
    用户会以为系统改了他的设置。**只有真的改了值，才说自己改了值。**

    范围超限用 clamp 而不是 reject：设备本来就无法设置到范围外的值，
    把"亮度 150"变成"亮度 100 并告知"比直接丢掉这条动作更有用。
    枚举则相反 —— 枚举没有"夹取"的概念，非法值只能拒绝。
    """
    vt = cap.value_type

    if vt == "bool":
        if isinstance(raw, bool):
            return raw, None, False
        if isinstance(raw, (int, float)):
            return bool(raw), None, False
        token = str(raw).strip().lower()
        if token in _TRUE_TOKENS:
            return True, None, False
        if token in _FALSE_TOKENS:
            return False, None, False
        return None, f"无法把 {raw!r} 解析为开关值（布尔）", False

    if vt in ("int", "float"):
        if isinstance(raw, bool):
            return None, f"{raw!r} 是布尔值，但该能力需要数值", False
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None, f"无法把 {raw!r} 解析为数值", False
        lo = float(cap.value_min) if cap.value_min is not None else None
        hi = float(cap.value_max) if cap.value_max is not None else None
        clamped = False
        if lo is not None and v < lo:
            v, clamped = lo, True
        if hi is not None and v > hi:
            v, clamped = hi, True
        return (int(round(v)) if vt == "int" else v), None, clamped

    if vt == "enum":
        token = str(raw).strip()
        if token not in cap.enum_values:
            return None, (
                f"取值 {token!r} 不在允许的枚举里"
                f"（可选：{'/'.join(cap.enum_values)}）"
            ), False
        return token, None, False

    # string
    if raw is None:
        return None, "值不能为空", False
    return str(raw), None, False


# ---------------------------------------------------------------- 单条校验
def _validate(catalog: DeviceCatalog, step: SceneStep) -> StepOutcome:
    out = StepOutcome(
        device_code=step.device_code,
        capability=step.capability,
        value=step.value,
        original_value=step.value,
    )

    if not step.device_code or not step.capability:
        out.status = "rejected"
        out.reason = "缺少 device_code 或 capability"
        return out

    dev = catalog.get(step.device_code)
    if dev is None:
        out.status = "rejected"
        out.reason = f"设备 {step.device_code} 不在设备目录中 —— 不得对未登记的设备下发指令"
        return out

    out.device_id = dev.id
    out.device_name = dev.name
    out.room_name = dev.room_name
    out.high_risk_device = dev.high_risk

    cap = dev.cap(step.capability)
    if cap is None:
        out.status = "rejected"
        out.reason = (
            f"{dev.name} 的能力档案里没有「{step.capability}」—— 不得臆造设备功能"
        )
        return out

    out.risk_level = cap.risk_level

    if not cap.writable:
        out.status = "rejected"
        out.reason = f"{dev.name} 的「{cap.display_name}」是只读能力，不能下发控制指令"
        return out

    value, err, clamped = _coerce_value(cap, step.value)
    if err:
        out.status = "rejected"
        out.reason = f"{dev.name} 的「{cap.display_name}」{err}"
        return out

    out.value = value
    if clamped:
        out.status = "clamped"
        out.reason = (
            f"取值 {step.value!r} 超出能力档案允许的范围，已夹取为 {value!r}"
            f"（{cap.value_min}~{cap.value_max}{cap.unit or ''}）"
        )
    return out


# ---------------------------------------------------------------- 重复检测
def _dedupe(outcomes: list[StepOutcome], plan: ScenePlan) -> list[StepOutcome]:
    """同一 (设备, 能力) 只保留最后一条。"""
    last_index: dict[tuple[str, str], int] = {}
    for i, o in enumerate(outcomes):
        last_index[(o.device_code, o.capability)] = i

    kept: list[StepOutcome] = []
    for i, o in enumerate(outcomes):
        key = (o.device_code, o.capability)
        if last_index[key] != i:
            winner = outcomes[last_index[key]]
            o.status = "duplicate"
            o.reason = (
                f"同一设备同一能力被设置了多次 —— 保留最后一条（{o.capability}={winner.value!r}），"
                f"本条 {o.value!r} 被覆盖"
            )
            plan.rejected.append(o)
        else:
            kept.append(o)
    return kept


# ---------------------------------------------------------------- 意图约束
def _apply_intent(outcome: StepOutcome, intent: SceneIntent, plan: ScenePlan) -> None:
    rule = intent.rule_for(outcome.capability)
    if rule is None:
        return

    result, new_value, reason = rule.check(outcome.value)

    if result == "reject":
        outcome.status = "rejected"
        outcome.reason = f"与「{intent.label}」场景冲突：{reason}"
        plan.rejected.append(outcome)
    elif result == "clamp" and new_value != outcome.value:
        outcome.original_value = outcome.value
        outcome.value = new_value
        outcome.status = "clamped"
        outcome.reason = f"按「{intent.label}」场景调整：{reason}（{outcome.original_value!r} → {new_value!r}）"
    elif result == "warn":
        if outcome.status == "ok":
            outcome.status = "warn"
        outcome.reason = f"「{intent.label}」场景提示：{reason}"


# ---------------------------------------------------------------- 主入口
def plan_scene(
    catalog: DeviceCatalog,
    candidates: list[Any],
    *,
    scene_name: str = "",
    description: str = "",
    max_actions: int = MAX_ACTIONS,
) -> ScenePlan:
    """把候选动作编排成可执行计划。

    `candidates` 可以是 `SceneStep`，也可以是 `{"device_code":..,"capability":..,"value":..}` 字典
    （后者是 LLM 的直接产出形式）。
    """
    query = f"{scene_name} {description}".strip()
    intent: SceneIntent = match_intent(query)

    steps: list[SceneStep] = [
        c if isinstance(c, SceneStep) else SceneStep.from_dict(dict(c)) for c in candidates
    ]

    plan = ScenePlan(
        intent_key=intent.key,
        intent_label=intent.label,
        raw_count=len(steps),
    )

    if intent.key == "default":
        plan.notes.append("未匹配到特定场景意图，只做能力校验与重复动作检测")
    else:
        plan.notes.append(f"匹配到场景意图「{intent.label}」：{intent.description}")
    if intent.note:
        plan.notes.append(intent.note)

    if len(steps) > max_actions:
        plan.notes.append(
            f"候选动作 {len(steps)} 条，超过单次上限 {max_actions} 条，只处理前 {max_actions} 条"
        )
        steps = steps[:max_actions]

    # 1) 能力校验
    validated: list[StepOutcome] = []
    for step in steps:
        outcome = _validate(catalog, step)
        if outcome.is_rejected:
            plan.rejected.append(outcome)
        else:
            validated.append(outcome)

    # 2) 重复检测
    kept = _dedupe(validated, plan)

    # 3) 意图约束
    for outcome in kept:
        _apply_intent(outcome, intent, plan)

    # 4) 风险标记（不剔除，只标出需要人工确认的）
    plan.actions = [o for o in kept if not o.is_rejected]
    for o in plan.actions:
        if o.high_risk_device and o.status in ("ok", "warn"):
            o.reason = (o.reason + "；" if o.reason else "") + "高风险设备：只会生成待确认建议，不会自动执行"

    if plan.pending_confirm_count:
        plan.notes.append(
            f"其中 {plan.pending_confirm_count} 条涉及高风险设备（门锁/燃气阀/总闸），"
            f"只会生成待确认建议，不会自动执行"
        )

    return plan
