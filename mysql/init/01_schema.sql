-- ============================================================================
-- 智能家居 AI 助手平台 · 01 建库 + 业务表结构
-- ============================================================================
-- 执行环境：虚拟机内的 MySQL 8.0（本机通过 192.168.159.100:3306 访问）
-- 执行方式（在虚拟机里，用 root）：
--     mysql -u root -p < 01_schema.sql
-- 或者用 Navicat / Workbench 之类工具整段执行。
--
-- 设计约定（务必遵守，后面所有代码都按这个来）：
--   1. 字符集统一 utf8mb4 / utf8mb4_0900_ai_ci，中文与 emoji 都不会出问题
--   2. 表名全小写。虚拟机 MySQL 的 lower_case_table_names=0（大小写敏感），
--      写 SQL 时表名大小写必须一致
--   3. 存储引擎 InnoDB（要事务、要外键）
--   4. **每个表、每个字段都写 COMMENT** —— 这不是为了好看：
--      字段 COMMENT 会被 LangChain 的 get_table_info() 拼进提示词，
--      是提升模型"看懂表结构"性价比最高的一招（规划书 §7 明确写了）
--   5. 向量【不】存在 MySQL。embedding 统一放 Chroma（嵌入式），
--      MySQL 只保存原文 + 可追溯元数据，用 chroma_id 关联。
--      这样避免同一份向量存两处、也不会把 MySQL 撑肿
-- ============================================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS `smart_home`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `smart_home`;


-- ============================================================================
-- 一、基础对象：家庭 / 房间 / 成员 / 账号 / 设备 / 能力 / 偏好
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. homes：家庭主体
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `homes` (
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '家庭ID',
    `name`          VARCHAR(64)     NOT NULL                COMMENT '家庭名称，如"我家的两室一厅"',
    `timezone`      VARCHAR(32)     NOT NULL DEFAULT 'Asia/Shanghai' COMMENT '时区，所有时间戳按此时区解释',
    `address_hash`  VARCHAR(128)    NULL                     COMMENT '地址脱敏哈希，不存明文地址（隐私要求）',
    `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='家庭主体（地址脱敏存储）';


-- ---------------------------------------------------------------------------
-- 2. rooms：房间 / 区域
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `rooms` (
    `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '房间ID',
    `home_id`    BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `code`       VARCHAR(48)     NOT NULL                COMMENT '房间英文编码（用于 MQTT topic），如 living_room',
    `name`       VARCHAR(48)     NOT NULL                COMMENT '房间中文名，如"客厅"',
    `floor`      TINYINT         NOT NULL DEFAULT 1      COMMENT '所在楼层',
    `sort_no`    SMALLINT        NOT NULL DEFAULT 0      COMMENT '展示排序号，小的在前',
    `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_rooms_home_code` (`home_id`, `code`),
    KEY `idx_rooms_home` (`home_id`),
    CONSTRAINT `fk_rooms_home` FOREIGN KEY (`home_id`) REFERENCES `homes` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='房间/区域（客厅、主卧、厨房…）';


-- ---------------------------------------------------------------------------
-- 3. members：家庭成员（注意：这是"人"，不是"登录账号"）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `members` (
    `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '成员ID',
    `home_id`    BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `nickname`   VARCHAR(32)     NOT NULL                COMMENT '成员昵称，如"爸爸""奶奶"',
    `role_code`  VARCHAR(32)     NOT NULL                COMMENT '家庭角色：owner户主/elder老人/child儿童/guest访客',
    `birth_year` SMALLINT        NULL                    COMMENT '出生年份（用于年龄段推断，不做医疗判断）',
    `voice_print_enabled` TINYINT(1) NOT NULL DEFAULT 0  COMMENT '是否启用声纹识别（首期不实现，留字段）',
    `enabled`    TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '是否启用',
    `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_members_home` (`home_id`),
    CONSTRAINT `fk_members_home` FOREIGN KEY (`home_id`) REFERENCES `homes` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='家庭成员画像（户主/老人/儿童/访客）';


-- ---------------------------------------------------------------------------
-- 4. users：登录账号（与 members 分开——一个人可以有账号，账号也可以属于访客）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `users` (
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '账号ID',
    `home_id`       BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `member_id`     BIGINT UNSIGNED NULL                    COMMENT '关联的家庭成员ID（可为空，如运维账号）',
    `username`      VARCHAR(64)     NOT NULL                COMMENT '登录名',
    `password_hash` VARCHAR(255)    NOT NULL                COMMENT '密码哈希（bcrypt，禁止存明文）',
    `display_name`  VARCHAR(64)     NOT NULL                COMMENT '界面显示名',
    `enabled`       TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '是否启用',
    `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_users_username` (`username`),
    KEY `idx_users_home` (`home_id`),
    CONSTRAINT `fk_users_home`   FOREIGN KEY (`home_id`)   REFERENCES `homes` (`id`),
    CONSTRAINT `fk_users_member` FOREIGN KEY (`member_id`) REFERENCES `members` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='登录账号（密码只存哈希）';


-- ---------------------------------------------------------------------------
-- 5. user_roles：账号的角色与数据范围（权限矩阵落地）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `user_roles` (
    `id`                      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `user_id`                 BIGINT UNSIGNED NOT NULL                COMMENT '账号ID',
    `role_code`               VARCHAR(32)     NOT NULL                COMMENT '角色：owner户主/elder老人/child儿童/guest访客/ops运维/auditor审计',
    `room_scope`              VARCHAR(255)    NOT NULL DEFAULT '*'    COMMENT '可见房间范围，逗号分隔的 room code，* 表示全部',
    `can_confirm_high_risk`   TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '是否有权确认高风险动作（门锁/燃气/总闸）',
    `created_at`              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_user_roles` (`user_id`, `role_code`),
    KEY `idx_user_roles_user` (`user_id`),
    CONSTRAINT `fk_user_roles_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='角色与数据/设备范围（最小权限落地）';


-- ---------------------------------------------------------------------------
-- 6. devices：设备实例
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `devices` (
    `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '设备ID',
    `home_id`     BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `room_id`     BIGINT UNSIGNED NULL                    COMMENT '所在房间ID（网关类设备可为空）',
    `code`        VARCHAR(64)     NOT NULL                COMMENT '设备编码（用于 MQTT topic），如 living_room_light_main',
    `name`        VARCHAR(64)     NOT NULL                COMMENT '设备中文名，如"客厅主灯"',
    `device_type` VARCHAR(48)     NOT NULL                COMMENT '设备类型：light/ac/curtain/tv/speaker/humidifier/water_heater/camera/door_lock/gas_valve/main_breaker/temp_sensor/air_sensor/gas_sensor/water_leak_sensor/smoke_sensor/mmwave_radar/door_sensor',
    `protocol`    VARCHAR(32)     NOT NULL DEFAULT 'mqtt' COMMENT '接入协议：mqtt/matter/zigbee/wifi',
    `vendor`      VARCHAR(64)     NULL                    COMMENT '厂商标识（模拟设备填 simulator）',
    `high_risk`   TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '是否高风险设备：门锁/燃气阀/电气总闸/布防撤防为1。高风险设备永不自动执行',
    `enabled`     TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '是否启用',
    `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_devices_home_code` (`home_id`, `code`),
    KEY `idx_devices_room` (`room_id`),
    KEY `idx_devices_type` (`device_type`),
    CONSTRAINT `fk_devices_home` FOREIGN KEY (`home_id`) REFERENCES `homes` (`id`),
    CONSTRAINT `fk_devices_room` FOREIGN KEY (`room_id`) REFERENCES `rooms` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='设备实例（含是否高风险标记）';


-- ---------------------------------------------------------------------------
-- 7. device_capabilities：设备能力档案
--    ★ 这张表是整个项目"防模型臆造"的核心 —— 模型必须依据它判断
--      "这台设备能不能做这件事、参数允许到多少"，不许凭想象发明功能。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `device_capabilities` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `device_id`       BIGINT UNSIGNED NOT NULL                COMMENT '设备ID',
    `capability_code` VARCHAR(64)     NOT NULL                COMMENT '能力编码，如 power_on/brightness/target_temp/mode',
    `display_name`    VARCHAR(64)     NOT NULL                COMMENT '能力中文名，如"开关""亮度""目标温度"',
    `value_type`      ENUM('bool','int','float','enum','string') NOT NULL COMMENT '取值类型',
    `unit`            VARCHAR(16)     NULL                    COMMENT '单位，如 % / ℃ / ppm；无单位为空',
    `value_min`       DECIMAL(10,3)   NULL                    COMMENT '最小值（数值型有效）',
    `value_max`       DECIMAL(10,3)   NULL                    COMMENT '最大值（数值型有效）',
    `step`            DECIMAL(10,3)   NULL                    COMMENT '步长（数值型有效）',
    `enum_values`     VARCHAR(255)    NULL                    COMMENT '枚举可选值，逗号分隔，如 cool,heat,fan,dry,auto',
    `writable`        TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '是否可写：0只读（传感器）/1可控制',
    `risk_level`      ENUM('low','medium','high') NOT NULL DEFAULT 'low' COMMENT '该能力的风险等级，high 需二次确认',
    `remark`          VARCHAR(255)    NULL                    COMMENT '备注/约束说明',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_dev_cap` (`device_id`, `capability_code`),
    KEY `idx_dev_cap_code` (`capability_code`),
    CONSTRAINT `fk_dev_cap_device` FOREIGN KEY (`device_id`) REFERENCES `devices` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='设备能力档案（防模型臆造设备功能的第一道防线）';


-- ---------------------------------------------------------------------------
-- 8. member_preferences：成员偏好
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `member_preferences` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `member_id`       BIGINT UNSIGNED NOT NULL                COMMENT '成员ID',
    `capability_code` VARCHAR(64)     NOT NULL                COMMENT '偏好对应的能力编码，如 target_temp',
    `value`           VARCHAR(64)     NOT NULL                COMMENT '偏好值，统一存字符串，按 capability 类型解释',
    `weight`          DECIMAL(4,3)    NOT NULL DEFAULT 1.000  COMMENT '权重（0~1），多人偏好冲突时用于协商裁决',
    `source`          VARCHAR(32)     NOT NULL DEFAULT 'manual' COMMENT '来源：manual手动设置/interaction对话推断/feedback反馈修正',
    `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_member_pref` (`member_id`, `capability_code`),
    CONSTRAINT `fk_member_pref_member` FOREIGN KEY (`member_id`) REFERENCES `members` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='成员偏好（温度/亮度/作息等，含来源与权重）';


-- ============================================================================
-- 二、运行态与事件
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 9. device_readings：设备遥测（按"指标"存，一条消息里的多个 metric 存多行）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `device_readings` (
    `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `device_id`    BIGINT UNSIGNED NOT NULL                COMMENT '设备ID',
    `observed_at`  DATETIME(3)     NOT NULL                COMMENT '观测时间（设备侧时间，不是入库时间）',
    `metric_code`  VARCHAR(64)     NOT NULL                COMMENT '指标编码，如 temperature/humidity/power_on/brightness',
    `value_num`    DECIMAL(14,4)   NULL                    COMMENT '数值型取值',
    `value_text`   VARCHAR(128)    NULL                    COMMENT '文本/枚举型取值，如 cool / locked',
    `unit`         VARCHAR(16)     NULL                    COMMENT '单位',
    `quality`      VARCHAR(16)     NOT NULL DEFAULT 'good' COMMENT '数据质量码：good/suspect/bad',
    `is_stale`     TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '是否已过期（过期数据不得当作实时值使用）',
    `seq`          BIGINT UNSIGNED NULL                    COMMENT '消息序号，用于丢包/乱序/重复检测',
    `source`       VARCHAR(32)     NOT NULL                COMMENT '数据来源：simulator/mqtt/csv/real_gateway',
    `created_at`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (`id`),
    KEY `idx_readings_device_time` (`device_id`, `observed_at`),
    KEY `idx_readings_metric_time` (`metric_code`, `observed_at`),
    KEY `idx_readings_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='设备遥测值（含质量标记与来源，过期不得伪装实时）';


-- ---------------------------------------------------------------------------
-- 10. energy_readings：用能明细（支持分时电价）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `energy_readings` (
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `device_id`     BIGINT UNSIGNED NOT NULL                COMMENT '设备ID',
    `observed_at`   DATETIME(3)     NOT NULL                COMMENT '观测时间',
    `power_w`       DECIMAL(10,3)   NULL                    COMMENT '瞬时功率（瓦）',
    `energy_kwh`    DECIMAL(12,6)   NULL                    COMMENT '累计电量（千瓦时）',
    `tariff_period` ENUM('peak','flat','valley') NULL       COMMENT '电价时段：peak峰/flat平/valley谷',
    `source`        VARCHAR(32)     NOT NULL DEFAULT 'simulator' COMMENT '数据来源',
    `created_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (`id`),
    KEY `idx_energy_device_time` (`device_id`, `observed_at`),
    KEY `idx_energy_tariff_time` (`tariff_period`, `observed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用能明细（支持分时电价峰平谷）';


-- ---------------------------------------------------------------------------
-- 11. device_commands：指令记录
--    ★ 高风险指令 status 必须经过 confirmed 才可执行。后端强制校验。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `device_commands` (
    `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '指令ID',
    `device_id`      BIGINT UNSIGNED NOT NULL                COMMENT '设备ID',
    `command`        VARCHAR(64)     NOT NULL                COMMENT '指令动作，如 turn_on/set_brightness/lock',
    `params_json`    JSON            NULL                    COMMENT '指令参数（JSON）',
    `issued_by`      VARCHAR(64)     NULL                    COMMENT '发起方：user:1 或 agent:scene_agent',
    `risk_level`     ENUM('low','medium','high') NOT NULL DEFAULT 'low' COMMENT '风险等级，high 的行为必须人工确认',
    `status`         ENUM('pending_confirm','confirmed','rejected','executed','failed','expired') NOT NULL DEFAULT 'pending_confirm' COMMENT '状态；高风险指令只有 confirmed 后才允许执行',
    `confirmed_by`   BIGINT UNSIGNED NULL                    COMMENT '确认人（users.id）',
    `confirmed_at`   DATETIME        NULL                    COMMENT '确认时间',
    `executed_at`    DATETIME        NULL                    COMMENT '实际执行时间',
    `result_message` VARCHAR(255)    NULL                    COMMENT '执行结果或失败原因',
    `created_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_cmd_device_time` (`device_id`, `created_at`),
    KEY `idx_cmd_status` (`status`),
    CONSTRAINT `fk_cmd_device` FOREIGN KEY (`device_id`) REFERENCES `devices` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='指令记录（高风险指令必须 confirmed 后才执行）';


-- ---------------------------------------------------------------------------
-- 12. scenes：场景定义
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `scenes` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '场景ID',
    `home_id`         BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `name`            VARCHAR(64)     NOT NULL                COMMENT '场景名，如"观影模式""睡眠模式"',
    `trigger_type`    ENUM('manual','schedule','sensor','voice') NOT NULL DEFAULT 'manual' COMMENT '触发方式',
    `trigger_config_json` JSON        NULL                    COMMENT '触发配置（如定时表达式）',
    `definition_json` JSON            NULL                    COMMENT '场景定义：设备动作序列 + 冲突处理结果',
    `created_by`      BIGINT UNSIGNED NULL                    COMMENT '创建人（users.id）',
    `enabled`         TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '是否启用',
    `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_scenes_home` (`home_id`),
    CONSTRAINT `fk_scenes_home` FOREIGN KEY (`home_id`) REFERENCES `homes` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='场景定义（睡眠/观影/起床/离家）';


-- ---------------------------------------------------------------------------
-- 13. scene_runs：场景执行记录（含降级情况）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `scene_runs` (
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '执行记录ID',
    `scene_id`      BIGINT UNSIGNED NOT NULL                COMMENT '场景ID',
    `started_at`    DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '开始时间',
    `finished_at`   DATETIME(3)     NULL                    COMMENT '结束时间',
    `steps_json`    JSON            NULL                    COMMENT '实际执行步骤与结果',
    `failed_steps`  JSON            NULL                    COMMENT '失败/被剔除的步骤（含原因，用于演示冲突检测）',
    `fallback_used` TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '是否使用了降级执行（如断网走本地规则）',
    `status`        ENUM('running','success','partial','failed') NOT NULL DEFAULT 'running' COMMENT '执行结果',
    PRIMARY KEY (`id`),
    KEY `idx_scene_runs_scene` (`scene_id`, `started_at`),
    CONSTRAINT `fk_scene_runs_scene` FOREIGN KEY (`scene_id`) REFERENCES `scenes` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='场景执行记录（含失败步骤与降级标记）';


-- ---------------------------------------------------------------------------
-- 14. sensor_events：传感器事件
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sensor_events` (
    `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '事件ID',
    `device_id`   BIGINT UNSIGNED NOT NULL                COMMENT '触发设备ID',
    `event_code`  VARCHAR(48)     NOT NULL                COMMENT '事件编码：gas_leak/water_leak/smoke/intrusion/door_open',
    `level`       ENUM('info','warning','critical') NOT NULL DEFAULT 'warning' COMMENT '事件级别',
    `started_at`  DATETIME(3)     NOT NULL                COMMENT '事件开始时间',
    `ended_at`    DATETIME(3)     NULL                    COMMENT '事件结束时间（持续中为空）',
    `payload`     JSON            NULL                    COMMENT '事件原始报文（含观测值与置信度）',
    `source`      VARCHAR(32)     NOT NULL DEFAULT 'mqtt' COMMENT '数据来源',
    PRIMARY KEY (`id`),
    KEY `idx_sensor_events_device_time` (`device_id`, `started_at`),
    KEY `idx_sensor_events_code` (`event_code`, `started_at`),
    CONSTRAINT `fk_sensor_events_device` FOREIGN KEY (`device_id`) REFERENCES `devices` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='传感器事件（燃气/水浸/烟感/入侵/门窗）';


-- ---------------------------------------------------------------------------
-- 15. alarms：告警及确认状态
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `alarms` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '告警ID',
    `room_id`         BIGINT UNSIGNED NULL                    COMMENT '相关房间ID',
    `device_id`       BIGINT UNSIGNED NULL                    COMMENT '相关设备ID',
    `alarm_code`      VARCHAR(48)     NOT NULL                COMMENT '告警编码',
    `level`           ENUM('info','warning','critical') NOT NULL DEFAULT 'warning' COMMENT '告警级别（影响打扰分级）',
    `title`           VARCHAR(128)    NOT NULL                COMMENT '告警标题',
    `detail`          TEXT            NULL                    COMMENT '告警详情与置信度说明',
    `status`          ENUM('active','acknowledged','resolved','dismissed') NOT NULL DEFAULT 'active' COMMENT '状态',
    `acknowledged_by` BIGINT UNSIGNED NULL                    COMMENT '确认人（users.id）',
    `acknowledged_at` DATETIME        NULL                    COMMENT '确认时间',
    `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '产生时间',
    PRIMARY KEY (`id`),
    KEY `idx_alarms_room_status` (`room_id`, `status`),
    KEY `idx_alarms_level_time` (`level`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='告警及人工确认状态';


-- ---------------------------------------------------------------------------
-- 16. care_observations：照护观察
--    ★ 只能用非摄像头手段（毫米波雷达/门磁/水电节律），且结论只能表述为"待确认线索"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `care_observations` (
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `member_id`     BIGINT UNSIGNED NOT NULL                COMMENT '被照护成员ID',
    `observed_at`   DATETIME(3)     NOT NULL                COMMENT '观察时间',
    `activity_type` VARCHAR(48)     NOT NULL                COMMENT '活动类型：presence/passage/medicine_check/no_activity/night_wake',
    `duration_min`  INT             NULL                    COMMENT '持续时长（分钟）',
    `source_device` BIGINT UNSIGNED NULL                    COMMENT '数据来源设备ID（**不得为摄像头**）',
    `confidence`    DECIMAL(4,3)    NULL                    COMMENT '置信度 0~1；低的只做提示不做结论',
    `remark`        VARCHAR(255)    NULL                    COMMENT '备注，健康类结论必须标注"待确认线索"',
    PRIMARY KEY (`id`),
    KEY `idx_care_member_time` (`member_id`, `observed_at`),
    CONSTRAINT `fk_care_member` FOREIGN KEY (`member_id`) REFERENCES `members` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='照护观察（活动节律/用药打卡，仅非摄像头数据源）';


-- ============================================================================
-- 三、知识、记忆与审计
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 17. knowledge_documents：知识文档元数据（说明书/保修单/安防预案）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `knowledge_documents` (
    `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '文档ID',
    `title`       VARCHAR(128)    NOT NULL                COMMENT '文档标题',
    `doc_type`    VARCHAR(48)     NOT NULL DEFAULT 'device_manual' COMMENT '类型：device_manual说明书/safety_plan预案/warranty保修单',
    `version`     VARCHAR(32)     NOT NULL DEFAULT 'v1'    COMMENT '版本号',
    `valid_from`  DATE            NULL                    COMMENT '生效日期',
    `valid_to`    DATE            NULL                    COMMENT '失效日期',
    `status`      ENUM('draft','active','archived') NOT NULL DEFAULT 'draft' COMMENT '状态：只有 active 才允许进入检索集合',
    `approved_by` BIGINT UNSIGNED NULL                    COMMENT '审核人（users.id）',
    `room_scope`  VARCHAR(255)    NOT NULL DEFAULT 'all'  COMMENT '适用房间范围，all 或逗号分隔的 room code',
    `file_path`   VARCHAR(255)    NULL                    COMMENT '原始文件相对路径',
    `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_kdoc_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='知识文档元数据（草稿/现行/废止状态控制检索范围）';


-- ---------------------------------------------------------------------------
-- 18. knowledge_chunks：知识片段（原文；向量在 Chroma）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `knowledge_chunks` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '片段ID',
    `document_id`     BIGINT UNSIGNED NOT NULL                COMMENT '所属文档ID',
    `page_or_section` VARCHAR(64)     NULL                    COMMENT '页码或章节，用于"引用可定位"',
    `content`         TEXT            NOT NULL                COMMENT '片段原文',
    `chroma_id`       VARCHAR(64)     NULL                    COMMENT '对应 Chroma 中的向量ID（向量不存 MySQL）',
    `room_scope`      VARCHAR(255)    NOT NULL DEFAULT 'all'  COMMENT '适用房间范围，检索时按此过滤',
    `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_kchunk_doc` (`document_id`),
    KEY `idx_kchunk_chroma` (`chroma_id`),
    CONSTRAINT `fk_kchunk_doc` FOREIGN KEY (`document_id`) REFERENCES `knowledge_documents` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='知识片段原文（向量存 Chroma，此处仅留可追溯引用）';


-- ---------------------------------------------------------------------------
-- 19. memories：长期记忆
--    ★ 每条记忆必须带 source 与 confidence，可被用户查看/修正/删除；
--      低置信度不参与个性化推荐（防"记忆污染"）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `memories` (
    `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '记忆ID',
    `home_id`     BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `member_id`   BIGINT UNSIGNED NULL                    COMMENT '关联成员ID（家庭级记忆可为空）',
    `memory_type` VARCHAR(32)     NOT NULL                COMMENT '类型：preference偏好/taboo禁忌/routine作息/event事件/feedback反馈',
    `content`     TEXT            NOT NULL                COMMENT '记忆内容（自然语言）',
    `chroma_id`   VARCHAR(64)     NULL                    COMMENT '对应 Chroma 向量ID',
    `source`      VARCHAR(64)     NOT NULL                COMMENT '来源：哪次对话/哪次设置，用于可解释引用',
    `confidence`  DECIMAL(4,3)    NOT NULL DEFAULT 0.800  COMMENT '置信度 0~1',
    `decay_at`    DATETIME        NULL                    COMMENT '遗忘时间（到期后降权或归档）',
    `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_memories_member_type` (`member_id`, `memory_type`),
    KEY `idx_memories_confidence` (`confidence`),
    CONSTRAINT `fk_memories_home` FOREIGN KEY (`home_id`) REFERENCES `homes` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='长期记忆（含来源/置信度/遗忘时间，可人工修正删除）';


-- ---------------------------------------------------------------------------
-- 20. suggestions：主动建议
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `suggestions` (
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '建议ID',
    `home_id`           BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `member_id`         BIGINT UNSIGNED NULL                    COMMENT '面向成员ID',
    `content`           VARCHAR(512)    NOT NULL                COMMENT '建议内容',
    `reason_json`       JSON            NULL                    COMMENT '推荐原因（关联了哪条记忆/哪个传感器数据），用于"可解释"',
    `disturbance_level` ENUM('urgent','important','normal','silent') NOT NULL DEFAULT 'normal' COMMENT '打扰等级',
    `status`            ENUM('pending','shown','accepted','dismissed') NOT NULL DEFAULT 'pending' COMMENT '状态',
    `dismissed_at`      DATETIME        NULL                    COMMENT '被关闭时间',
    `created_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_sugg_home_level` (`home_id`, `disturbance_level`),
    KEY `idx_sugg_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='主动建议（含推荐原因与打扰等级）';


-- ---------------------------------------------------------------------------
-- 21. disturbance_rules：免打扰配置
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `disturbance_rules` (
    `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `home_id`    BIGINT UNSIGNED NOT NULL                COMMENT '所属家庭ID',
    `member_id`  BIGINT UNSIGNED NULL                    COMMENT '成员ID（家庭级规则可为空）',
    `level`      ENUM('urgent','important','normal','silent') NOT NULL DEFAULT 'normal' COMMENT '该规则约束的打扰等级',
    `quiet_start` TIME           NOT NULL                COMMENT '免打扰开始时间',
    `quiet_end`   TIME           NOT NULL                COMMENT '免打扰结束时间',
    `enabled`    TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '是否启用',
    PRIMARY KEY (`id`),
    KEY `idx_disturb_home` (`home_id`),
    CONSTRAINT `fk_disturb_home` FOREIGN KEY (`home_id`) REFERENCES `homes` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='免打扰配置（按打扰等级分档）';


-- ---------------------------------------------------------------------------
-- 22. conversations：用户会话
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `conversations` (
    `id`         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '会话ID',
    `user_id`    BIGINT UNSIGNED NULL                    COMMENT '所属账号ID',
    `home_id`    BIGINT UNSIGNED NULL                    COMMENT '所属家庭ID',
    `thread_id`  VARCHAR(64)     NOT NULL                COMMENT 'LangGraph 会话ID（与 checkpoint 表关联）',
    `title`      VARCHAR(128)    NULL                    COMMENT '会话标题',
    `created_at` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_conv_thread` (`thread_id`),
    KEY `idx_conv_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户会话（thread_id 关联 LangGraph checkpoint）';


-- ---------------------------------------------------------------------------
-- 23. agent_runs：Agent 执行记录（可审计、可算成本）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `agent_runs` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `conversation_id` BIGINT UNSIGNED NULL                    COMMENT '会话ID',
    `intent`          VARCHAR(64)     NULL                    COMMENT '识别出的意图',
    `route_agent`     VARCHAR(48)     NULL                    COMMENT '路由到的专业Agent：scene/security/energy/care/assistant',
    `tools_used`      JSON            NULL                    COMMENT '调用的工具列表（白名单工具的调用摘要）',
    `status`          ENUM('running','success','failed','interrupted') NOT NULL DEFAULT 'running' COMMENT '执行状态；interrupted 表示停在人工确认',
    `duration_ms`     INT             NULL                    COMMENT '耗时（毫秒）',
    `token_usage`     JSON            NULL                    COMMENT 'token 用量（成本观测）',
    `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    KEY `idx_agent_runs_conv` (`conversation_id`, `created_at`),
    KEY `idx_agent_runs_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Agent 执行记录与工具调用摘要';


-- ---------------------------------------------------------------------------
-- 24. audit_events：审计日志
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `audit_events` (
    `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    `user_id`     BIGINT UNSIGNED NULL                    COMMENT '操作人（users.id）',
    `action`      VARCHAR(48)     NOT NULL                COMMENT '动作：view查看/export导出/confirm确认/reject驳回/config_change配置变更/login登录',
    `object_type` VARCHAR(48)     NULL                    COMMENT '对象类型：device/scene/memory/member/command',
    `object_id`   VARCHAR(64)     NULL                    COMMENT '对象ID',
    `detail`      JSON            NULL                    COMMENT '变更详情（改前/改后）',
    `ip_address`  VARCHAR(64)     NULL                    COMMENT '来源IP',
    `created_at`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发生时间',
    PRIMARY KEY (`id`),
    KEY `idx_audit_user_time` (`user_id`, `created_at`),
    KEY `idx_audit_action_time` (`action`, `created_at`),
    KEY `idx_audit_object` (`object_type`, `object_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='审计日志（查看/导出/确认/配置变更）';


-- ============================================================================
-- 完成。表清单核对：
--   基础 8 张：homes / rooms / members / users / user_roles / devices /
--              device_capabilities / member_preferences
--   运行 8 张：device_readings / energy_readings / device_commands / scenes /
--              scene_runs / sensor_events / alarms / care_observations
--   知识 8 张：knowledge_documents / knowledge_chunks / memories / suggestions /
--              disturbance_rules / conversations / agent_runs / audit_events
--   合计 24 张
-- ============================================================================
