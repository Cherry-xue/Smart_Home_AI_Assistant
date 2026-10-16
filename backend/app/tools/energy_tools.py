"""能耗类工具。

这里体现规划书 §9 第 3 条："**数值计算交给确定性代码**"。
工具本身不做任何估算 —— 它只是把 `services/analytics` 算好的结果读给模型听。
模型负责的是"解释"和"建议"，不是"心算"。

⚠️ 时间锚点
----------
默认窗口以**数据自身的最新时间**为锚点（`overview.data_anchor`），而不是"现在"。
原因在 M2 踩过：模拟器按倍速跑，数据时间戳会跑到真实时钟之外，
用"现在往前 24 小时"会**一条数据都查不到**，然后模型就会说"没有数据"——
可数据明明全在库里。锚点 ≈ 现在时，两种写法行为完全等价。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from langchain_core.tools import tool

from app.core.tariff import get_tariff
from app.db.catalog import DeviceCatalog
from app.db.session import SessionLocal
from app.services import analytics
from app.tools._common import resolve_window


def build_energy_tools(ctx: Any) -> list[Any]:
    catalog: DeviceCatalog = ctx.catalog
    tariff = get_tariff()

    @tool
    def calc_energy_cost(hours: int = 24) -> str:
        """计算最近一段时间的**用电量与电费**（按分时电价：峰 / 平 / 谷）。

        什么时候用：用户问"这个月电费多少""哪里最费电""怎么省电""峰谷电价"这类问题。
        **不要自己心算任何金额** —— 金额必须来自本工具。

        参数：hours —— 往前统计多少小时，默认 24；一周用 168，一个月用 720。

        返回：总电量与总金额、分时段明细（峰/平/谷各自的电量与金额）、
        各设备类型耗电排行、逐小时曲线，以及系统自动生成的几条观察结论。
        """
        try:
            span = max(1, min(int(hours or 24), 24 * 31))
        except (TypeError, ValueError):
            span = 24

        from_dt, to_dt = resolve_window(ctx, span)
        with SessionLocal() as db:
            try:
                data = analytics.energy_summary(
                    db, catalog, tariff, from_dt=from_dt, to_dt=to_dt
                )
            except Exception as e:  # noqa: BLE001
                return f"能耗查询失败：{type(e).__name__}: {e}"

        total_kwh = data.get("total_kwh") or 0
        total_cost = data.get("total_cost") or 0
        if not total_kwh:
            return (
                f"最近 {span} 小时（{from_dt:%m-%d %H:%M} ~ {to_dt:%m-%d %H:%M}）没有能耗数据。"
                "请如实告知用户查不到，**不要给出任何省费数字**。"
            )

        cur = data.get("currency") or "CNY"
        unit = data.get("unit") or "kWh"
        lines = [
            f"统计区间：{from_dt:%Y-%m-%d %H:%M} ~ {to_dt:%Y-%m-%d %H:%M}（共 {span} 小时）",
            f"总用电：{total_kwh} {unit}，总电费：{total_cost} {cur}",
            "",
            "【分时段】",
        ]
        period_cn = {"peak": "峰电", "flat": "平电", "valley": "谷电"}
        for p in data.get("by_period") or []:
            name = period_cn.get(str(p.get("period")), str(p.get("period")))
            share = float(p.get("share") or 0) * 100
            lines.append(
                f"  {name}：{p.get('kwh')} {unit} × {p.get('price')} = {p.get('cost')} {cur}"
                f"（占总电量 {share:.1f}%）"
            )

        by_device = data.get("by_device") or []
        if by_device:
            lines.append("")
            lines.append("【耗电排行】")
            for d in by_device[:6]:
                share = float(d.get("share") or 0) * 100
                lines.append(
                    f"  {d.get('name') or d.get('device_type')}："
                    f"{d.get('kwh')} {unit}（{share:.1f}%）"
                )

        hourly = data.get("hourly") or []
        if hourly:
            peak_h = max(hourly, key=lambda h: h.get("kwh") or 0)
            lines.append("")
            lines.append(f"【逐小时】用电最多的时段是 {peak_h.get('hour')} 点（{peak_h.get('kwh')} {unit}）")

        obs = data.get("observations") or []
        if obs:
            lines.append("")
            lines.append("【系统观察（由确定性代码算出）】")
            for o in obs:
                lines.append(f"  · {o}")

        lines.append("")
        lines.append("（以上金额均由确定性代码按分时电价计算，非模型估算）")
        return "\n".join(lines)

    @tool
    def list_tariff() -> str:
        """查询**分时电价的时段划分与单价**。

        什么时候用：用户问"峰谷电价怎么算""什么时候用电便宜"，或你要解释某条省费建议的依据时。
        """
        return (
            f"当前电价方案（{tariff.currency}/{tariff.unit}）：\n{tariff.describe()}\n"
            "说明：峰电单价最高、谷电最低。把热水器这类可延时的加热负载挪到谷电时段，"
            "能在总电量不变的前提下降低电费。"
        )

    return [calc_energy_cost, list_tariff]
