"""人工确认接口的请求/响应模型。

对应规划书 §10.3 契约里的 `/api/confirmations`。

⚠️ 高风险确认与"目标冲突裁决"**共用这套模型**：
两者都是"暂停 → 等人决定 → 继续"，只是 `kind` 不同
（`high_risk` / `conflict`），选项不同。前端可以复用同一个弹窗组件。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class InterventionOut(BaseModel):
    """一条待人工决定的事项。"""

    thread_id: str = Field(description="所属会话，确认时要带着它回来")
    kind: str = Field(description="high_risk=高风险动作确认；conflict=目标冲突裁决")
    device_code: str | None = Field(default=None, description="涉及设备编码")
    device_name: str = Field(default="", description="设备中文名")
    capability: str | None = Field(default=None, description="动作能力")
    value: Any | None = Field(default=None, description="目标值")
    prompt: str = Field(default="", description="要展示给人的问题")
    detail: str = Field(default="", description="风险说明 / 冲突原因")
    options: list[str] = Field(default_factory=list, description="可选项，如 approve / reject")
    expires_at: str | None = Field(default=None, description="失效时间（超时后不再接受决定）")
    created_at: dt.datetime | None = Field(default=None, description="挂起时间")
    question: str | None = Field(default=None, description="触发这次确认的原始问题")


class PendingListOut(BaseModel):
    items: list[InterventionOut] = Field(default_factory=list)
    total: int = Field(default=0, description="待确认事项总数")


class DecisionIn(BaseModel):
    """一个决定。

    - `high_risk` 场景：`choice` 取 `approve` / `reject`；
    - `conflict` 场景：`choice` 取系统给出的某个方案标签。
    """

    choice: str = Field(description="选择项。高风险确认用 approve / reject")
    intervention_index: int = Field(
        default=0,
        description="第几条介入事项（同一轮可能有多个高风险动作同时待确认）",
    )
    note: str = Field(default="", description="备注（可选）")


class DecisionOut(BaseModel):
    thread_id: str
    accepted: bool = Field(description="该决定是否被接受（例如已超时则不接受）")
    choice: str
    message: str = Field(default="", description="给用户看的说明")
    answer: str | None = Field(default=None, description="恢复后生成的最终回答")
    run_result: dict[str, Any] | None = Field(default=None, description="执行结果摘要")
    remaining: int = Field(default=0, description="该会话还剩几条待确认")
