-- ============================================================================
-- 智能家居 AI 助手平台 · 05 历史会话（对话消息）
-- ============================================================================
-- 执行方式（在虚拟机里，用 root）：
--     mysql -u root -p < 05_chat_history.sql
--
-- 为什么单独加一张表
-- ------------------
-- `conversations` 只记「会话」本身（thread_id / 标题 / 创建时间），
-- `agent_runs` 只记「执行」的元数据（意图、路由、耗时、token），**都不存对话正文**。
-- 大屏右侧的「历史会话」要把上一次的问答原样回放出来，就必须有正文落库的地方。
--
-- 为什么不用 checkpointer 里的状态
-- ------------------------------
-- LangGraph checkpoint 存的是**图状态**（messages / plan / decisions…），
-- 它的结构随 Agent 演进会变、且只保留最近一次运行的快照；
-- 拿它当"历史记录"既不稳定也不好分页。对话正文单独存，职责更清楚。
--
-- ⚠️ `app_rw` 没有 DDL 权限，所以这张表**必须先用 root 执行本脚本**。
--    后端只做 SELECT / INSERT / DELETE。
-- ============================================================================

SET NAMES utf8mb4;

USE `smart_home`;


-- ---------------------------------------------------------------------------
-- 25. chat_messages：对话消息（历史会话回放）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `chat_messages` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `conversation_id` BIGINT UNSIGNED NOT NULL                COMMENT '所属会话（conversations.id）',
    `thread_id`       VARCHAR(64)     NOT NULL                COMMENT '会话标识（与 checkpoint 关联）',
    `role`            ENUM('user','assistant') NOT NULL       COMMENT '角色：user 提问 / assistant 回复',
    `content`         MEDIUMTEXT      NULL                    COMMENT '正文（提问原文或最终回答）',
    `sections`        JSON            NULL                    COMMENT '分区回答（事实/分析/待确认…）',
    `trace`           JSON            NULL                    COMMENT '执行过程（节点/标签/详情/耗时）',
    `interrupted`     TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '该轮是否停在人工确认',
    `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_chat_msg_conv` (`conversation_id`, `id`),
    KEY `idx_chat_msg_thread` (`thread_id`, `id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='对话消息（历史会话回放）';


-- ============================================================================
-- 完成。表清单：24 + 1 = 25 张。
-- ============================================================================
