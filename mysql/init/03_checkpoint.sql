-- ============================================================================
-- 智能家居 AI 助手平台 · 03 LangGraph checkpoint 库
-- ============================================================================
-- 执行顺序：01 → 02 → 03 → 04
-- 执行方式（在虚拟机里，用 root）：
--     mysql -u root -p < 03_checkpoint.sql
--
-- ★ 为什么要单独一个库？
--   LangGraph 的 checkpointer 会自己建表、自己写、表结构由框架版本决定。
--   如果混进业务库 smart_home，会有两个后果：
--     1. 它的表会污染 get_table_info() 的白名单（规划书 §8.5 明确提醒过）
--     2. 框架升级时改表，可能和我们的 Alembic 迁移打架
--   所以：业务库和状态库彻底分开。
--
-- ★ 关于 DDL 权限的一个有意破例：
--   选型方案里说"不给后端 DDL 权限，改表结构在虚拟机本地执行"。
--   但 checkpoint 库必须破例给 CREATE —— 因为 PyMySQLSaver.setup()
--   是在后端首次启动时创建这些表的，我们无法预先知道它要建什么表。
--   这个破例是安全的：该库里只有会话/图状态，没有任何业务数据。
--   【业务库 smart_home 依然严格不给 DDL】。
--
-- 首次启动后端时的用法（代码里）：
--     from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
--     with PyMySQLSaver.from_conn_string(CHECKPOINT_DATABASE_URL) as cp:
--         cp.setup()          # 首次必须调用，之后可省
--         graph = builder.compile(checkpointer=cp)
--   ⚠️ 手工传连接时必须 autocommit=True，否则 setup() 建的表不会提交。
-- ============================================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS `smart_home_checkpoint`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;


-- ---------------------------------------------------------------------------
-- 授权：app_rw 需要「读写 + 建表」权限（因为 setup() 要建表）
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP
    ON `smart_home_checkpoint`.* TO 'app_rw'@'192.168.159.%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP
    ON `smart_home_checkpoint`.* TO 'app_rw'@'localhost';

-- agent_ro 不需要碰 checkpoint 库（Agent 不直接读会话状态，会话由后端管）
-- 若将来要排查，可临时打开下面这行：
-- GRANT SELECT ON `smart_home_checkpoint`.* TO 'agent_ro'@'192.168.159.%';

FLUSH PRIVILEGES;


-- ---------------------------------------------------------------------------
-- 自检：库建好了、权限也对
-- ---------------------------------------------------------------------------
SELECT SCHEMA_NAME AS `已创建的库`, DEFAULT_CHARACTER_SET_NAME AS `字符集`,
       DEFAULT_COLLATION_NAME AS `排序规则`
FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME IN ('smart_home', 'smart_home_checkpoint');

SELECT '--- app_rw 在 checkpoint 库上的权限 ---' AS `检查项`;
SHOW GRANTS FOR 'app_rw'@'192.168.159.%';

-- 提示：此刻 checkpoint 库还是空的（一张表都没有），这是正常的。
--      表会在后端第一次调用 PyMySQLSaver.setup() 时自动创建。
