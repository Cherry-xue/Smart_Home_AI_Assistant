-- ============================================================================
-- ⚠️⚠️⚠️  危险脚本：重置（删除）所有数据库对象  ⚠️⚠️⚠️
-- ============================================================================
--
-- ！！！执行本脚本会【永久删除】以下内容 ！！！
--
--     DROP DATABASE smart_home             —— 全部业务数据（设备/事件/记忆/审计）
--     DROP DATABASE smart_home_checkpoint  —— 全部 Agent 会话与中断现场
--     DROP USER app_rw / agent_ro          —— 两个应用账号
--
-- 删除后【不可恢复】。以下情况请先停手：
--   · 里面已经有你手动录入的数据
--   · 里面已经有跑通的场景/记忆，或者正准备答辩演示
--   · 你只是想重建某一张表 —— 那就单独 ALTER，不要用这个
--
-- 确认要重置时：
--   1. 先备份（强烈建议）：
--        mysqldump -u root -p --databases smart_home smart_home_checkpoint \
--            --single-transaction --routines --triggers > backup_YYYYMMDD.sql
--   2. 再执行：mysql -u root -p < 99_reset_DANGER.sql
--
-- 本文件特意命名为 99 开头，就是为了在按文件名顺序批量执行时排在最后，
-- 不会被顺手跑掉。
-- ============================================================================

SET NAMES utf8mb4;

-- 账号
DROP USER IF EXISTS 'app_rw'@'192.168.159.%';
DROP USER IF EXISTS 'app_rw'@'localhost';
DROP USER IF EXISTS 'agent_ro'@'192.168.159.%';
DROP USER IF EXISTS 'agent_ro'@'localhost';

-- 数据库
DROP DATABASE IF EXISTS `smart_home`;
DROP DATABASE IF EXISTS `smart_home_checkpoint`;

FLUSH PRIVILEGES;

-- ---------------------------------------------------------------------------
-- 自检：确认已经清干净
-- ---------------------------------------------------------------------------
SELECT '剩余相关库（应为空）' AS `检查项`;
SELECT SCHEMA_NAME AS `库名` FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME IN ('smart_home', 'smart_home_checkpoint');

SELECT '剩余相关账号（应为空）' AS `检查项`;
SELECT CONCAT(USER, '@', HOST) AS `账号` FROM mysql.`user`
WHERE USER IN ('app_rw', 'agent_ro');

-- 重置完成后，重新初始化请按顺序执行：
--   01_schema.sql → 02_accounts.sql → 03_checkpoint.sql → 04_seed.sql
