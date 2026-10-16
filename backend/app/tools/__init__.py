"""工具层：白名单 + 工厂。

设计原则（规划书 §9 第 5 条）
--------------------------
**每个工具只开放完成任务所需的最小读写范围**，而且 Agent 拿到的工具是
**按名字显式列出的集合**，不是"全部工具"。

⚠️ 最重要的一条红线：**这里不存在任何"直接执行高风险动作"的工具。**
门锁、燃气阀、电气总闸的动作只能走到 `services.scenes.planner` 变成"待确认建议"，
执行通道在 `services.scenes.executor` 里（且高风险被强制 `pending_confirm`）。
Agent 侧没有绕过去的路径 —— 这不是靠提示词约束，是靠**工具列表里根本没有**。

用法
----
```python
ctx = ToolContext(catalog=catalog, cache=cache, home_id=1)
tools = build_tools(ctx, ["read_device_state", "list_capability"])
```
"""

from __future__ import annotations

import dataclasses
from itertools import chain
from typing import Any, Callable, Iterable

from langchain_core.tools import BaseTool

from app.db.catalog import DeviceCatalog
from app.mqtt.cache import LatestValueCache
from app.tools.context_tools import build_context_tools
from app.tools.device_tools import build_device_tools
from app.tools.energy_tools import build_energy_tools
from app.tools.knowledge_tools import build_knowledge_tools


@dataclasses.dataclass(frozen=True)
class ToolContext:
    """工具运行时需要的外部依赖（由 `graph` 在每个请求开始时注入）。"""

    catalog: DeviceCatalog
    cache: LatestValueCache
    home_id: int = 1
    member_name: str | None = None


#: 工具分组：组名 -> 该组提供的工具名
TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "device": ("read_device_state", "list_capability", "read_history"),
    "energy": ("calc_energy_cost", "list_tariff"),
    "knowledge": ("search_knowledge", "list_scene_intents"),
    "context": ("read_recent_events", "search_memory"),
}

#: 全部可用工具名（构建时会校验请求的名字都在这里，拼错立刻报错而不是静默少给）
ALL_TOOL_NAMES: frozenset[str] = frozenset(chain(*TOOL_GROUPS.values()))

_BUILDERS: dict[str, Callable[[Any], list[BaseTool]]] = {
    "device": build_device_tools,
    "energy": build_energy_tools,
    "knowledge": build_knowledge_tools,
    "context": build_context_tools,
}

#: ⚠️ 工具名里出现这些词，说明有人往工具层塞了"执行通道" —— 必须拦下。
#: 高风险动作在本项目里只能生成待确认项，不能有任何直达执行的工具。
_FORBIDDEN_KEYWORDS = ("execute", "unlock", "open_valve", "shutoff", "arm_", "disarm")


def build_tools(ctx: ToolContext, names: Iterable[str]) -> list[BaseTool]:
    """按名字构建工具集合（即白名单）。

    只构造被请求的分组，避免为了一次简单查询把 9 个工具全建一遍。
    """
    wanted = {str(n).strip() for n in names if str(n).strip()}
    if not wanted:
        return []

    unknown = wanted - ALL_TOOL_NAMES
    if unknown:
        raise ValueError(
            f"未登记的工具名：{sorted(unknown)}；可选：{sorted(ALL_TOOL_NAMES)}"
        )

    out: list[BaseTool] = []
    for group, group_names in TOOL_GROUPS.items():
        if not (wanted & set(group_names)):
            continue
        for t in _BUILDERS[group](ctx):
            if t.name in wanted:
                out.append(t)

    _assert_no_execution_channel(out)
    return out


def _assert_no_execution_channel(tools: list[BaseTool]) -> None:
    """自检：确保没有工具名暗示"可以直接执行高风险动作"。

    这条断言的价值不在今天，而在**半年后有人加工具时** —— 它会让那种改动当场失败，
    而不是等到某天门锁被自动打开才发现。
    """
    for t in tools:
        low = t.name.lower()
        for bad in _FORBIDDEN_KEYWORDS:
            if bad in low:
                raise AssertionError(
                    f"工具 {t.name!r} 名字里含 {bad!r}，疑似绕过高风险确认的执行通道。"
                    "高风险动作只能经由 services.scenes.planner 生成待确认建议。"
                )
