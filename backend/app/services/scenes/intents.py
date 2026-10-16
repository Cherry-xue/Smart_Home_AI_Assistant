"""场景意图模板：给"自然语言场景"一个**可解释**的判据。

解决的问题
----------
LLM 生成的动作可能自相矛盾，而最典型的冲突**跨设备**：
「看电影」却把客厅主灯开到 100% —— 它和"观影要暗"矛盾，但主灯和灯带是两台不同设备，
"同一设备同一能力被设两次"这种检查根本抓不到。

所以这里为常见场景定义合理区间（观影时房间照明 ≤ 40%），
超出区间的动作被**剔除**并给出可复现的原因。这正是规划书 §14.1 的要求：
"生成『看电影模式』时，先查能力档案再做冲突检测，**能复现被剔除的动作及原因**"。

为什么放 YAML 而不是写死在代码里
------------------------------
判据会随家庭习惯变（有人观影时喜欢留一盏亮灯）。放配置里，
调整判据不用改代码、不用重新测试，也让"为什么剔除了这一条"更容易解释给用户听。
"""

from __future__ import annotations

import dataclasses
import functools
from pathlib import Path
from typing import Any

import yaml

from app.config.settings import PROJECT_ROOT

#: 复用 settings 里算好的项目根 —— 不要在这里自己数 `parents[n]`：
#: 模块所在层级一变（比如从 `app/core/` 挪到 `app/services/scenes/`），
#: 层数就会算错，而且报错形式是"文件找不到"，非常容易误判成配置没写对。
INTENTS_PATH = PROJECT_ROOT / "config" / "scene_intents.yaml"

VALID_ACTIONS = ("reject", "clamp", "warn")


@dataclasses.dataclass(frozen=True)
class IntentRule:
    """一条意图约束。"""

    capability: str
    action: str                       # reject / clamp / warn
    min: float | None = None
    max: float | None = None
    reason: str = ""
    disabled: bool = False            # 配置里用来留占位（如"必须开灯"这类正向约束暂未实现）

    def check(self, value: Any) -> tuple[str, Any, str]:
        """检查一个值是否满足本规则。

        返回 (结果, 处理后的值, 原因)。结果取 `ok` / `reject` / `clamp` / `warn`。
        """
        if self.disabled:
            return "ok", value, ""
        # 只对数值型做区间判断（枚举型由能力档案校验，意图规则不参与）
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "ok", value, ""

        v = float(value)
        lo, hi = self.min, self.max

        if self.action == "clamp":
            if lo is not None and v < lo:
                return "clamp", int(lo) if float(lo).is_integer() else lo, self.reason
            if hi is not None and v > hi:
                return "clamp", int(hi) if float(hi).is_integer() else hi, self.reason
            return "ok", value, ""

        if self.action == "reject":
            if lo is not None and v < lo:
                return "reject", value, self.reason or f"低于场景要求的下限 {lo}"
            if hi is not None and v > hi:
                return "reject", value, self.reason or f"高于场景要求的上限 {hi}"
            return "ok", value, ""

        # warn
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return "warn", value, self.reason
        return "ok", value, ""


@dataclasses.dataclass(frozen=True)
class SceneIntent:
    key: str
    label: str
    description: str
    keywords: tuple[str, ...]
    rules: tuple[IntentRule, ...]
    note: str = ""

    def rule_for(self, capability: str) -> IntentRule | None:
        for r in self.rules:
            if r.capability == capability and not r.disabled:
                return r
        return None


DEFAULT_INTENT = SceneIntent(
    key="default",
    label="通用",
    description="无特定场景约束，只做能力校验与重复动作检测",
    keywords=(),
    rules=(),
)


def _parse_intent(key: str, raw: dict[str, Any]) -> SceneIntent:
    rules: list[IntentRule] = []
    for r in raw.get("rules") or []:
        action = str(r.get("action", "warn")).lower()
        if action not in VALID_ACTIONS:
            continue
        rules.append(
            IntentRule(
                capability=str(r.get("capability", "")),
                action=action,
                min=float(r["min"]) if r.get("min") is not None else None,
                max=float(r["max"]) if r.get("max") is not None else None,
                reason=str(r.get("reason", "")),
                disabled=bool(r.get("disabled", False)),
            )
        )
    return SceneIntent(
        key=key,
        label=str(raw.get("label") or key),
        description=str(raw.get("description") or ""),
        keywords=tuple(str(k) for k in (raw.get("keywords") or [])),
        rules=tuple(rules),
        note=str(raw.get("note") or ""),
    )


@functools.lru_cache(maxsize=1)
def load_intents(path: Path | str = INTENTS_PATH) -> dict[str, SceneIntent]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, SceneIntent] = {}
    for key, value in (raw.get("intents") or {}).items():
        if isinstance(value, dict):
            out[str(key)] = _parse_intent(str(key), value)
    if "default" in raw and isinstance(raw["default"], dict):
        out["default"] = _parse_intent("default", raw["default"])
    if "default" not in out:
        out["default"] = DEFAULT_INTENT
    return out


def match_intent(text: str, *, intents: dict[str, SceneIntent] | None = None) -> SceneIntent:
    """按关键词匹配场景意图。

    匹配范围是**场景名 + 自然语言描述**拼接后的文本，命中第一个意图为准
    （配置里靠前的意图优先，所以配置顺序有意义）。
    """
    table = intents if intents is not None else load_intents()
    haystack = (text or "").strip()
    if haystack:
        for intent in table.values():
            if intent.key == "default":
                continue
            for kw in intent.keywords:
                if kw and kw in haystack:
                    return intent
    return table.get("default", DEFAULT_INTENT)


def describe_intents(intents: dict[str, SceneIntent] | None = None) -> str:
    """给提示词用的意图清单（让 LLM 知道系统认得哪些场景）。"""
    table = intents if intents is not None else load_intents()
    lines: list[str] = []
    for it in table.values():
        if it.key == "default":
            continue
        rules = "；".join(
            f"{r.capability} {r.action}"
            + (f"[{r.min if r.min is not None else ''}~{r.max if r.max is not None else ''}]" if (r.min is not None or r.max is not None) else "")
            for r in it.rules
            if not r.disabled
        )
        lines.append(f"- {it.label}（{it.key}）：{it.description}｜约束：{rules or '无'}")
    return "\n".join(lines)
