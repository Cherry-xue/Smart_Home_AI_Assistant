"""ORM 模型与真实数据库的逐表逐字段一致性校验。

为什么需要它
------------
表结构由 `mysql/init/01_schema.sql` 在虚拟机里手工执行，而 ORM 是我们写的 Python 代码。
**两处独立维护，必然会有跑偏的一天**（改了 SQL 忘了改 ORM，或反过来）。
这个脚本就是那道防线：把 SQLAlchemy 的 metadata 与 information_schema 做集合比对。

它检查三件事：
  1. 表：库里有的 ORM 有没有、ORM 有的库里有没有
  2. 字段：每张表的列名集合是否一致
  3. 可空性：NOT NULL / NULL 是否一致
  4. 类型：做宽松比对（只归一化后比较，避免 display width 之类的噪音误报）

运行：
    cd backend
    python tests/schema_check.py
退出码：0 = 完全一致，1 = 有差异。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.models.orm import ALL_MODELS, Base  # noqa: E402


def normalize_type(db_type: str) -> str:
    """把类型名归一化，抹平"同义但写法不同"的表达。

    为什么必须做这一步：SQLAlchemy 生成 `INTEGER` / `NUMERIC(10,3)`，
    而 MySQL 的 DDL 里通常写 `int` / `decimal(10,3)` —— 在 MySQL 里它们是**同一个类型**。
    如果不对齐同义词，校验器就会天天报一堆假差异，
    **然后人就再也不看它的输出了** —— 那这道防线的价值就归零了。
    """
    t = db_type.lower().strip()

    # ---- 同义类型归一 ----
    t = t.replace("integer", "int")          # INTEGER ≡ INT
    t = t.replace("numeric", "decimal")      # NUMERIC ≡ DECIMAL（MySQL 同义）
    t = t.replace("doubleprecision", "double")
    t = t.replace("character varying", "varchar")

    # ---- 等价写法归一 ----
    if t in ("tinyint(1)", "bool", "boolean"):
        return "bool"                        # BOOLEAN 在 MySQL 里就是 TINYINT(1)

    # tinyint(3) unsigned -> tinyint unsigned（display width 已废弃，不影响语义）
    if t.startswith("tinyint("):
        t = "tinyint" + t[t.index(")") + 1 :]

    return t.replace(" ", "")


def orm_type_str(col) -> str:
    """取 SQLAlchemy 列在 MySQL 上的类型字符串。"""
    try:
        return normalize_type(col.type.compile(dialect=engine.dialect))
    except Exception:  # noqa: BLE001
        return str(col.type).lower()


def main() -> int:
    settings = get_settings()
    print("=" * 74)
    print("ORM ↔ 数据库 一致性校验")
    print("=" * 74)
    print(f"  目标库: {settings.mysql_host}:{settings.mysql_port}/{settings.mysql_db}")
    print(f"  身份  : {settings.mysql_user}")
    print(f"  ORM   : {len(ALL_MODELS)} 个模型")
    print()

    # ---- 真实库 ----
    with engine.connect() as conn:
        db_tables = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE='BASE TABLE'"
                )
            )
        }
        db_cols: dict[str, dict[str, tuple[bool, str, bool]]] = {}
        for tbl, col, ctype, nullable, key in conn.execute(
            text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
            )
        ):
            db_cols.setdefault(tbl, {})[col] = (nullable == "NO", ctype, key == "PRI")

    # ---- ORM ----
    orm_tables = {m.__tablename__: m for m in ALL_MODELS}
    all_orm_tables = set(Base.metadata.tables)

    problems: list[str] = []

    missing_orm = sorted(db_tables - all_orm_tables - {"_grant_probe"})
    extra_orm = sorted(all_orm_tables - db_tables)
    if missing_orm:
        problems.append(f"库里存在但 ORM 未建模的表: {missing_orm}")
    if extra_orm:
        problems.append(f"ORM 里有但库里不存在的表: {extra_orm}")

    print(f"  库中表 {len(db_tables)} 张 / ORM 表 {len(all_orm_tables)} 张")
    print()

    col_mismatch = null_mismatch = type_mismatch = pk_mismatch = 0
    for tname in sorted(set(orm_tables) & db_tables):
        table = Base.metadata.tables[tname]
        orm_cols = {c.name: c for c in table.columns}
        real_cols = db_cols.get(tname, {})

        only_orm = sorted(set(orm_cols) - set(real_cols))
        only_db = sorted(set(real_cols) - set(orm_cols))
        if only_orm:
            problems.append(f"[{tname}] ORM 多出的字段: {only_orm}")
            col_mismatch += 1
        if only_db:
            problems.append(f"[{tname}] 库里多出的字段: {only_db}")
            col_mismatch += 1

        for cname in sorted(set(orm_cols) & set(real_cols)):
            ocol = orm_cols[cname]
            db_notnull, db_ctype, db_pk = real_cols[cname]

            if bool(not ocol.nullable) != db_notnull:
                problems.append(
                    f"[{tname}.{cname}] 可空性不一致: ORM nullable={ocol.nullable}, 库 NOT NULL={db_notnull}"
                )
                null_mismatch += 1

            if bool(ocol.primary_key) != db_pk:
                problems.append(
                    f"[{tname}.{cname}] 主键标记不一致: ORM={ocol.primary_key}, 库={db_pk}"
                )
                pk_mismatch += 1

            o_t, d_t = orm_type_str(ocol), normalize_type(db_ctype)
            if o_t != d_t:
                problems.append(f"[{tname}.{cname}] 类型不一致: ORM={o_t}, 库={d_t}")
                type_mismatch += 1

    print("-" * 74)
    if not problems:
        print(f"  ✅ 完全一致：{len(set(orm_tables) & db_tables)} 张表、"
              f"{sum(len(v) for v in db_cols.values())} 个字段，表名/字段名/可空性/主键/类型全部对得上")
        print("=" * 74)
        return 0

    print(f"  ⚠️ 发现 {len(problems)} 处差异（字段 {col_mismatch} / 可空 {null_mismatch} / "
          f"主键 {pk_mismatch} / 类型 {type_mismatch}）：")
    for p in problems[:60]:
        print(f"    - {p}")
    if len(problems) > 60:
        print(f"    ...（还有 {len(problems) - 60} 处）")
    print("=" * 74)
    return 1


if __name__ == "__main__":
    sys.exit(main())
