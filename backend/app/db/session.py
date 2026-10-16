"""数据库会话：引擎 + 连接池 + FastAPI 依赖。

关键点
------
1. **同步引擎 + PyMySQL**（选型决定）：异步只留在 FastAPI 端点 / LLM / SSE / MQTT 层。
   LangGraph 的工具写成同步 `def` 时框架会自动丢线程池执行，所以在这里同步是安全的。
2. `pool_pre_ping=True` **对远程库是必须的**：虚拟机会挂起/网络抖动，
   没有它就会拿到已经死掉的连接然后报一堆莫名其妙的错。
3. `pool_recycle=3600`：MySQL 默认 wait_timeout 是 8 小时，1 小时回收一次更保险。
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import get_settings

_settings = get_settings()

engine: Engine = create_engine(
    _settings.db_url,
    pool_size=5,
    max_overflow=5,
    pool_recycle=3600,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
    echo=False,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：`def endpoint(db: Session = Depends(get_db))`。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ping() -> dict[str, object]:
    """健康检查用：返回库版本、字符集与当前身份。"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT VERSION(), @@character_set_server, CURRENT_USER(), DATABASE()")
        ).one()
    return {
        "version": row[0],
        "charset": row[1],
        "current_user": row[2],
        "database": row[3],
    }


def table_info(include_tables: list[str] | None = None) -> str:
    """生成"带 COMMENT 的表结构说明"，格式与 LangChain 的 get_table_info() 兼容。

    为什么要自己实现而不用 `langchain_community.utilities.SQLDatabase`：
    1. 选型里明确**不装 langchain-community**（v1 之后它的模块定位已变，依赖它容易踩升级坑）；
    2. 直接查 information_schema 能拿到**完整的字段 COMMENT**，而 COMMENT 正是
       让模型看懂表结构的关键（M1 验收项要确认这一点）；
    3. 输出格式完全可控，后面要按房间/角色裁剪提示词时改起来更自由。
    """
    sql = """
        SELECT c.TABLE_NAME, c.ORDINAL_POSITION, c.COLUMN_NAME, c.COLUMN_TYPE,
               c.IS_NULLABLE, c.COLUMN_KEY, c.COLUMN_DEFAULT, c.COLUMN_COMMENT,
               t.TABLE_COMMENT
        FROM information_schema.COLUMNS c
        JOIN information_schema.TABLES t
          ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
        WHERE c.TABLE_SCHEMA = DATABASE()
        ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).all()

    if include_tables:
        allow = set(include_tables)
        rows = [r for r in rows if r[0] in allow]

    lines: list[str] = []
    current = None
    for tbl, _pos, col, coltype, nullable, key, default, comment, tbl_comment in rows:
        if tbl != current:
            if current is not None:
                lines.append("")
            lines.append(f"CREATE TABLE `{tbl}` (  -- {tbl_comment}")
            current = tbl
        flags = []
        if key == "PRI":
            flags.append("PK")
        if nullable == "NO":
            flags.append("NOT NULL")
        if default is not None:
            flags.append(f"DEFAULT {default}")
        tail = f"  -- {comment}" if comment else ""
        lines.append(f"  `{col}` {coltype} {', '.join(flags)}{tail}")
    if current is not None:
        lines.append(")")
    return "\n".join(lines)
