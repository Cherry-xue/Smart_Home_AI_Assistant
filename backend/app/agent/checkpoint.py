"""LangGraph 的会话持久化（checkpointer）。

为什么单独成一个模块
--------------------
1. **序列化白名单**要显式登记 —— 图状态里放的是 Pydantic 模型，
   不登记的话 LangGraph 每次反序列化都会警告
   "Deserializing unregistered type app.agent.models.RouteDecision"，
   并明确说了"未来版本会直接禁止"。这是**定时炸弹**，不是可忽略的噪声。
2. **后端可切换** —— 开发期用内存（零配置），接 MySQL 时换 `PyMySQLSaver`
   落到独立库 `smart_home_checkpoint`（不与业务库混）。

⚠️ 为什么默认仍是内存
--------------------
`PyMySQLSaver` 的 async 方法内部走的是**同步** DBAPI 调用，
在事件循环里会阻塞几十毫秒；本项目后端主体也是同步 SQLAlchemy，
量级一致，可以接受 —— 但它需要 broker 之外再依赖一次数据库连通性。

所以默认 `memory`（保证"不配也能跑"），把 MySQL 做成显式开关：
`CHECKPOINT_BACKEND=mysql`。切换失败会**自动退回内存**并告警，
绝不因为"存档失败"就让整个对话不可用。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("app.agent.checkpoint")

#: 图状态里会出现的所有自定义类型。**加新模型时必须同步登记在这里**，
#: 否则反序列化会先警告、后报错。
ALLOWED_MSGPACK_MODULES: tuple[tuple[str, str], ...] = (
    ("app.agent.models", "RouteDecision"),
    ("app.agent.models", "Finding"),
    ("app.agent.models", "Fact"),
    ("app.agent.models", "Citation"),
    ("app.agent.models", "MemoryRef"),
    ("app.agent.models", "ProposedAction"),
    ("app.agent.models", "InterventionItem"),
    ("app.agent.models", "Decision"),
)


def build_serde() -> Any:
    """构造带白名单的序列化器。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=list(ALLOWED_MSGPACK_MODULES),
    )


def build_checkpointer(backend: str = "memory") -> Any:
    """按配置构造 checkpointer。

    - `memory`（默认）：进程内字典，**重启即丢**，但零依赖；
    - `mysql`：`PyMySQLSaver` + 独立库 `smart_home_checkpoint`，首次会建表。
      连接失败时退回 memory 并告警。
    """
    serde = build_serde()

    if str(backend).strip().lower() == "mysql":
        try:
            saver = _mysql_saver(serde)
            # ⚠️ 首次必须 setup()：它会建 checkpoints / checkpoint_writes 等表
            saver.setup()
            log.info("已启用 MySQL checkpointer（库 smart_home_checkpoint）")
            return saver
        except Exception as e:  # noqa: BLE001
            log.warning(
                "MySQL checkpointer 初始化失败，退回内存版：%s: %s", type(e).__name__, e
            )

    from langgraph.checkpoint.memory import MemorySaver

    log.warning("使用内存 checkpointer（仅适合开发）；中断现场在进程重启后会丢失")
    return MemorySaver(serde=serde)


def _mysql_saver(serde: Any) -> Any:
    """用 PyMySQL 直连构造 saver。

    直接传 DBAPI 连接而不是连接串：`from_conn_string()` 是个上下文管理器，
    退出时会关连接，不适合长期持有。
    ⚠️ `autocommit=True` 是**必须的** —— 否则 checkpoint 写不进去且不报错
    （表现为"中断后恢复不了，但日志一切正常"）。
    """
    import pymysql

    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

    from app.config.settings import get_settings

    s = get_settings()
    conn = pymysql.connect(
        host=s.mysql_host,
        port=int(s.mysql_port),
        user=s.mysql_user,
        password=s.mysql_password,
        database=s.mysql_db_checkpoint,
        charset="utf8mb4",
        autocommit=True,
    )
    return PyMySQLSaver(conn, serde=serde)
