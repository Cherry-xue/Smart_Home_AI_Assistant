"""自然语言 → 场景候选动作（**本模块是全流程唯一用到 LLM 的地方**）。

职责边界（很重要）
----------------
本模块只负责"**想做什么**"，产出的是**候选**动作。
"**能不能做**"由 `planner` 用确定性代码判断（设备是否存在、能力是否登记、值是否越界、是否与场景冲突）。

这么分工的收益：模型说错话最多导致**动作被剔除**，
而不是**对设备下发非法指令** —— 错误被挡在编排层，到不了执行层。

为什么不用 function calling / `create_agent`
------------------------------------------
本任务本质是"把一句话翻译成结构化动作"：输出结构固定、不需要多轮工具调用。
用一次结构化输出的提示词更简单、更可复现（`temperature=0`），也更好调试 ——
出问题直接看提示词和模型原始输出即可。多 Agent 编排是 M4 的事，这里不提前引入。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.db.catalog import DeviceCatalog
from app.services.scenes.intents import describe_intents
from app.services.scenes.models import SceneStep

log = logging.getLogger("app.services.scenes.nl")

#: 从模型输出里抠 JSON 对象（容忍 ```json 包裹或前后有解释文字）
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


def render_device_catalog(catalog: DeviceCatalog, *, writable_only: bool = True) -> str:
    """把设备与可写能力渲染成提示词里的"菜单"。

    **只列可写能力**：场景是"控制场景"，只读能力（传感器）不参与其中，
    列出来反而会诱导模型生成"把温度设为 25"这种对传感器下发的非法指令。
    """
    lines: list[str] = []
    for room in catalog.rooms:
        devices = [d for d in catalog.in_room(room.code)]
        if not devices:
            continue
        lines.append(f"### {room.name}（room_code={room.code}）")
        for dev in devices:
            caps = [c for c in dev.capabilities.values() if c.writable or not writable_only]
            if not caps:
                continue
            flag = "  ⚠️高风险(只生成待确认建议)" if dev.high_risk else ""
            lines.append(f"- {dev.name}（device_code={dev.code}, 类型={dev.device_type}）{flag}")
            for c in caps:
                desc = f"{c.code}({c.value_type}"
                if c.unit:
                    desc += f", {c.unit}"
                if c.enum_values:
                    desc += f", 可选: {'/'.join(c.enum_values)}"
                elif c.value_min is not None and c.value_max is not None:
                    desc += f", {c.value_min}~{c.value_max}"
                desc += ")"
                mark = " [只读]" if not c.writable else ""
                lines.append(f"    · {desc}{mark}")
    return "\n".join(lines)


PROMPT_TEMPLATE = """你是智能家居场景编排助手。请把用户的一句话转换成一组**设备控制动作**。

## 可用设备与能力（**只能使用下面列出的 device_code 与 capability，不得编造**）

{catalog}

## 系统支持的场景意图（生成时请遵循对应约束）

{intents}

## 输出要求

只输出一个 JSON 对象，不要输出任何解释文字、不要用 markdown 代码块包裹：

{{"actions": [{{"device_code": "...", "capability": "...", "value": ...}}], "understanding": "一句话说明你的理解"}}

规则：
1. **device_code 与 capability 必须来自上面的列表**，不得创造新的编码；
2. 值必须符合能力声明的类型与范围（枚举能力只能取列出的取值）；
3. 只生成与该场景**直接相关**的动作，不要把所有设备都写一遍；
4. 如果用户的描述里包含高风险设备（门锁、燃气阀、总闸），照常生成动作 ——
   系统会自动把它转成"待确认建议"，不需要你特殊处理；
5. 如果无法理解用户意图，返回 {{"actions": [], "understanding": "无法理解的原因"}}。

## 用户输入

{query}
"""


def build_prompt(catalog: DeviceCatalog, query: str) -> str:
    return PROMPT_TEMPLATE.format(
        catalog=render_device_catalog(catalog),
        intents=describe_intents(),
        query=query.strip(),
    )


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON 对象（容忍 ```json 包裹与前后废话）。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(raw)
        if not m:
            raise ValueError(f"模型输出里找不到 JSON 对象：{raw[:200]!r}") from None
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("模型输出的 JSON 顶层不是对象")
    return data


class SceneBuildError(RuntimeError):
    """自然语言解析失败（模型不可用 / 输出无法解析）。"""


def build_scene_from_text(
    catalog: DeviceCatalog,
    query: str,
    *,
    llm: Any | None = None,
) -> tuple[list[SceneStep], str, str]:
    """把一句话变成候选动作。

    返回 `(候选步骤, 模型的理解说明, 原始输出)`。
    **注意：返回的是候选，尚未经过能力校验** —— 调用方必须再走一遍 `planner.plan_scene`。
    """
    from app.models.llm import get_llm

    prompt = build_prompt(catalog, query)
    model = llm if llm is not None else get_llm()

    try:
        message = model.invoke(prompt)
    except Exception as e:  # noqa: BLE001
        raise SceneBuildError(f"模型调用失败：{type(e).__name__}: {e}") from e

    text = getattr(message, "content", "") or ""
    try:
        data = extract_json(text)
    except (ValueError, json.JSONDecodeError) as e:
        raise SceneBuildError(f"模型输出无法解析为动作序列：{e}") from e

    raw_actions = data.get("actions") or []
    if not isinstance(raw_actions, list):
        raise SceneBuildError("模型输出的 actions 不是数组")

    steps = [SceneStep.from_dict(a) for a in raw_actions if isinstance(a, dict)]
    understanding = str(data.get("understanding") or "").strip()
    return steps, understanding, text
