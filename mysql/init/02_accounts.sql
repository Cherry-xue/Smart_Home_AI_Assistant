-- ============================================================================
-- 智能家居 AI 助手平台 · 02 业务库账号与授权
-- ============================================================================
-- 执行顺序：必须在 01_schema.sql 之后（要先有 smart_home 库才能授权）
-- 执行方式（在虚拟机里，用 root）：
--     mysql -u root -p < 02_accounts.sql
--
-- 为什么要单独建账号？（规划书里的"护栏第 1 层"）
--   即使应用层代码被绕过、或者模型幻觉出一句 UPDATE/DELETE，
--   只要账号本身没有写权限，数据库这一层就拦住了。
--   把限制从"应用层自觉"下沉到"数据库强制"，是性价比最高的安全措施。
--
-- ⚠️ 下面的密码是【开发用密码】，写死在脚本里只为方便。
--    正式部署请改成强密码，并考虑改用环境变量注入或 mysql_config_editor。
-- ============================================================================

SET NAMES utf8mb4;

USE `smart_home`;


-- ---------------------------------------------------------------------------
-- 账号 1：app_rw —— 业务读写
--   用途：后端服务（FastAPI）执行设备状态写入、事件落库、场景增删改查
--   权限：业务库的 SELECT/INSERT/UPDATE/DELETE（**不含 DDL**，改表结构走 01_schema）
-- ---------------------------------------------------------------------------

-- 来自宿主机（虚拟机眼中宿主机的地址是 192.168.159.1，属于这个网段）
CREATE USER IF NOT EXISTS 'app_rw'@'192.168.159.%' IDENTIFIED BY 'AppRw@2026';
-- 来自虚拟机内部（万一后续把后端也部署到虚拟机里）
CREATE USER IF NOT EXISTS 'app_rw'@'localhost'     IDENTIFIED BY 'AppRw@2026';

GRANT SELECT, INSERT, UPDATE, DELETE ON `smart_home`.* TO 'app_rw'@'192.168.159.%';
GRANT SELECT, INSERT, UPDATE, DELETE ON `smart_home`.* TO 'app_rw'@'localhost';

-- 如果将来需要从别的网段访问，再打开下面这行（安全性更低，谨慎）：
-- CREATE USER IF NOT EXISTS 'app_rw'@'%' IDENTIFIED BY 'AppRw@2026';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON `smart_home`.* TO 'app_rw'@'%';


-- ---------------------------------------------------------------------------
-- 账号 2：agent_ro —— Agent 侧只读
--   用途：Agent 查询设备状态、能耗数据、成员偏好时使用
--   权限：**只有 SELECT**。这是"模型永远不能改数据"的物理保证
-- ---------------------------------------------------------------------------

CREATE USER IF NOT EXISTS 'agent_ro'@'192.168.159.%' IDENTIFIED BY 'AgentRo@2026';
CREATE USER IF NOT EXISTS 'agent_ro'@'localhost'     IDENTIFIED BY 'AgentRo@2026';

GRANT SELECT ON `smart_home`.* TO 'agent_ro'@'192.168.159.%';
GRANT SELECT ON `smart_home`.* TO 'agent_ro'@'localhost';


FLUSH PRIVILEGES;


-- ---------------------------------------------------------------------------
-- 自检：看看两个账号的权限是不是符合预期
-- ---------------------------------------------------------------------------
SELECT '--- app_rw 的权限 ---' AS `检查项`;
SHOW GRANTS FOR 'app_rw'@'192.168.159.%';
SELECT '--- agent_ro 的权限 ---' AS `检查项`;
SHOW GRANTS FOR 'agent_ro'@'192.168.159.%';
