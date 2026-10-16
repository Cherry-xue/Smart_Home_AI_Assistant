-- ============================================================================
-- 智能家居 AI 助手平台 · 04 种子数据（演示家庭 · 两室一厅 · 30 台虚拟设备）
-- ============================================================================
-- 执行顺序：01 → 02 → 03 → 04（最后跑这个）
-- 执行方式（在虚拟机里，用 root）：
--     mysql -u root -p < 04_seed.sql
--
-- 本脚本会创建一套可以直接用来演示的完整数据：
--   1 个家庭 · 7 个区域 · 4 位成员 · 1 个登录账号
--   32 台虚拟设备（含门锁/燃气阀/总闸三台高风险设备）
--   设备能力档案（防模型臆造的关键数据）
--   成员偏好 · 免打扰规则
--
-- 设备构成（32 台）：玄关 3 / 客厅 8 / 厨房 5 / 卫生间 3 / 阳台 1 /
--                    主卧 6 / 次卧 6
--   温湿度传感器覆盖 客厅 · 主卧 · 次卧 三个房间（主卧/次卧是为"睡眠模式
--   要调卧室温度"补的，否则系统读不到卧室温度）
--
-- 脚本做了幂等处理（重复执行不会产生重复数据），但仍建议只跑一次。
-- 想彻底重来请用 99_reset_DANGER.sql。
-- ============================================================================

SET NAMES utf8mb4;

USE `smart_home`;


-- ---------------------------------------------------------------------------
-- 1. 家庭
-- ---------------------------------------------------------------------------
INSERT INTO `homes` (`name`, `timezone`, `address_hash`)
SELECT '演示家庭 · 两室一厅', 'Asia/Shanghai', SHA2('演示地址-已脱敏', 256)
WHERE NOT EXISTS (SELECT 1 FROM `homes` WHERE `name` = '演示家庭 · 两室一厅');

SET @home_id = (SELECT `id` FROM `homes` WHERE `name` = '演示家庭 · 两室一厅' LIMIT 1);


-- ---------------------------------------------------------------------------
-- 2. 房间 / 区域（7 个：含玄关与阳台）
--    code 会直接用在 MQTT topic 里，所以用英文小写下划线
--
--    ⚠️ floor 全部为 1 —— 这是一套**平层两室一厅**，不是复式。
--       曾经主卧/次卧被写成 floor=2，让户型看起来像两层；
--       但两室一厅本身就是单层户型，而且文档里没有任何"复式/二层"的表述，
--       所以统一改回 1。floor 字段保留是为了将来出现真正的多层住宅时可用，
--       **当前没有前后端逻辑依赖它的取值**（只有 ORM 映射与建表语句）。
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `rooms` (`home_id`, `code`, `name`, `floor`, `sort_no`) VALUES
    (@home_id, 'entryway',       '玄关',   1, 1),
    (@home_id, 'living_room',    '客厅',   1, 2),
    (@home_id, 'kitchen',        '厨房',   1, 3),
    (@home_id, 'bathroom',       '卫生间', 1, 4),
    (@home_id, 'balcony',        '阳台',   1, 5),
    (@home_id, 'master_bedroom', '主卧',   1, 6),
    (@home_id, 'second_bedroom', '次卧',   1, 7);


-- ---------------------------------------------------------------------------
-- 3. 家庭成员（4 位：两位户主 + 一位老人 + 一个孩子）
-- ---------------------------------------------------------------------------
INSERT INTO `members` (`home_id`, `nickname`, `role_code`, `birth_year`)
SELECT @home_id, '爸爸', 'owner', 1985
WHERE NOT EXISTS (SELECT 1 FROM `members` WHERE `home_id` = @home_id AND `nickname` = '爸爸');

INSERT INTO `members` (`home_id`, `nickname`, `role_code`, `birth_year`)
SELECT @home_id, '妈妈', 'owner', 1987
WHERE NOT EXISTS (SELECT 1 FROM `members` WHERE `home_id` = @home_id AND `nickname` = '妈妈');

INSERT INTO `members` (`home_id`, `nickname`, `role_code`, `birth_year`)
SELECT @home_id, '奶奶', 'elder', 1952
WHERE NOT EXISTS (SELECT 1 FROM `members` WHERE `home_id` = @home_id AND `nickname` = '奶奶');

INSERT INTO `members` (`home_id`, `nickname`, `role_code`, `birth_year`)
SELECT @home_id, '小宇', 'child', 2016
WHERE NOT EXISTS (SELECT 1 FROM `members` WHERE `home_id` = @home_id AND `nickname` = '小宇');

SET @member_dad   = (SELECT `id` FROM `members` WHERE `home_id` = @home_id AND `nickname` = '爸爸');
SET @member_mom   = (SELECT `id` FROM `members` WHERE `home_id` = @home_id AND `nickname` = '妈妈');
SET @member_grand = (SELECT `id` FROM `members` WHERE `home_id` = @home_id AND `nickname` = '奶奶');
SET @member_child = (SELECT `id` FROM `members` WHERE `home_id` = @home_id AND `nickname` = '小宇');


-- ---------------------------------------------------------------------------
-- 4. 登录账号 + 角色
--    ⚠️ password_hash 是占位符！真实的 bcrypt 哈希会在 M4 做鉴权时
--       由初始化脚本写入。现在这个值没法登录，是故意留空的。
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `users` (`home_id`, `member_id`, `username`, `password_hash`, `display_name`) VALUES
    (@home_id, @member_dad, 'owner', '$2b$12$PLACEHOLDER_REPLACE_WITH_REAL_BCRYPT_HASH_IN_M4', '爸爸');

SET @user_owner = (SELECT `id` FROM `users` WHERE `username` = 'owner' LIMIT 1);

INSERT IGNORE INTO `user_roles` (`user_id`, `role_code`, `room_scope`, `can_confirm_high_risk`) VALUES
    (@user_owner, 'owner', '*', 1);


-- 把房间 ID 先取到变量里。
-- 注意这里带了 home_id 条件：只靠 code 匹配的话，
-- 将来存在第二个家庭时会串到别人家的房间上。
SET @room_entryway       = (SELECT id FROM rooms WHERE home_id = @home_id AND code = 'entryway');
SET @room_living_room    = (SELECT id FROM rooms WHERE home_id = @home_id AND code = 'living_room');
SET @room_kitchen        = (SELECT id FROM rooms WHERE home_id = @home_id AND code = 'kitchen');
SET @room_bathroom       = (SELECT id FROM rooms WHERE home_id = @home_id AND code = 'bathroom');
SET @room_balcony        = (SELECT id FROM rooms WHERE home_id = @home_id AND code = 'balcony');
SET @room_master_bedroom = (SELECT id FROM rooms WHERE home_id = @home_id AND code = 'master_bedroom');
SET @room_second_bedroom = (SELECT id FROM rooms WHERE home_id = @home_id AND code = 'second_bedroom');


-- ---------------------------------------------------------------------------
-- 5. 设备清单（32 台）
--    high_risk=1 的三台是项目安全边界的核心：
--    入户门锁 / 厨房燃气阀 / 配电箱总闸 —— 它们永远不会被自动执行，
--    只能生成"待确认建议"。
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `devices`
    (`home_id`, `room_id`, `code`, `name`, `device_type`, `protocol`, `vendor`, `high_risk`) VALUES
    -- 玄关（3）：含门锁与总闸两台高风险设备
    (@home_id, @room_entryway,       'entryway_light',            '玄关灯',           'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_entryway,       'front_door_lock',           '入户门锁',         'door_lock',         'mqtt', 'simulator', 1),
    (@home_id, @room_entryway,       'entryway_main_breaker',     '配电箱总闸',       'main_breaker',      'mqtt', 'simulator', 1),
    -- 客厅（8）
    (@home_id, @room_living_room,    'living_room_light_main',    '客厅主灯',         'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_living_room,    'living_room_light_strip',   '客厅氛围灯带',     'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_living_room,    'living_room_ac',            '客厅空调',         'ac',                'mqtt', 'simulator', 0),
    (@home_id, @room_living_room,    'living_room_curtain',       '客厅窗帘',         'curtain',           'mqtt', 'simulator', 0),
    (@home_id, @room_living_room,    'living_room_tv',            '客厅电视',         'tv',                'mqtt', 'simulator', 0),
    (@home_id, @room_living_room,    'living_room_temp_sensor',   '客厅温湿度传感器', 'temp_sensor',       'mqtt', 'simulator', 0),
    (@home_id, @room_living_room,    'living_room_air_sensor',    '客厅空气质量传感器','air_sensor',       'mqtt', 'simulator', 0),
    (@home_id, @room_living_room,    'living_room_speaker',       '客厅智能音箱',     'speaker',           'mqtt', 'simulator', 0),
    -- 主卧（6）
    (@home_id, @room_master_bedroom, 'master_bedroom_light_main', '主卧主灯',         'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_master_bedroom, 'master_bedroom_light_bedside','主卧床头灯',     'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_master_bedroom, 'master_bedroom_ac',         '主卧空调',         'ac',                'mqtt', 'simulator', 0),
    (@home_id, @room_master_bedroom, 'master_bedroom_curtain',    '主卧窗帘',         'curtain',           'mqtt', 'simulator', 0),
    (@home_id, @room_master_bedroom, 'master_bedroom_humidifier', '主卧加湿器',       'humidifier',        'mqtt', 'simulator', 0),
    (@home_id, @room_master_bedroom, 'master_bedroom_temp_sensor','主卧温湿度传感器', 'temp_sensor',       'mqtt', 'simulator', 0),
    -- 次卧（6）：老人/儿童房，照护数据源都在这里
    (@home_id, @room_second_bedroom, 'second_bedroom_light_main', '次卧主灯',         'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_second_bedroom, 'second_bedroom_ac',         '次卧空调',         'ac',                'mqtt', 'simulator', 0),
    (@home_id, @room_second_bedroom, 'second_bedroom_curtain',    '次卧窗帘',         'curtain',           'mqtt', 'simulator', 0),
    (@home_id, @room_second_bedroom, 'second_bedroom_mmwave',     '次卧毫米波雷达',   'mmwave_radar',      'mqtt', 'simulator', 0),
    (@home_id, @room_second_bedroom, 'second_bedroom_door_sensor','次卧门磁',         'door_sensor',       'mqtt', 'simulator', 0),
    (@home_id, @room_second_bedroom, 'second_bedroom_temp_sensor','次卧温湿度传感器', 'temp_sensor',       'mqtt', 'simulator', 0),
    -- 厨房（5）：燃气阀是第三台高风险设备
    (@home_id, @room_kitchen,        'kitchen_light',             '厨房灯',           'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_kitchen,        'kitchen_gas_sensor',        '厨房燃气传感器',   'gas_sensor',        'mqtt', 'simulator', 0),
    (@home_id, @room_kitchen,        'kitchen_gas_valve',         '厨房燃气阀',       'gas_valve',         'mqtt', 'simulator', 1),
    (@home_id, @room_kitchen,        'kitchen_water_leak_sensor', '厨房水浸传感器',   'water_leak_sensor', 'mqtt', 'simulator', 0),
    (@home_id, @room_kitchen,        'kitchen_smoke_sensor',      '厨房烟感',         'smoke_sensor',      'mqtt', 'simulator', 0),
    -- 卫生间（3）
    (@home_id, @room_bathroom,       'bathroom_light',            '卫生间灯',         'light',             'mqtt', 'simulator', 0),
    (@home_id, @room_bathroom,       'bathroom_water_heater',     '卫生间热水器',     'water_heater',      'mqtt', 'simulator', 0),
    (@home_id, @room_bathroom,       'bathroom_water_leak_sensor','卫生间水浸传感器', 'water_leak_sensor', 'mqtt', 'simulator', 0),
    -- 阳台（1）
    (@home_id, @room_balcony,        'balcony_light',             '阳台灯',           'light',             'mqtt', 'simulator', 0);


-- ---------------------------------------------------------------------------
-- 6. 设备能力档案
--    ★ 这张表是"防模型臆造"的核心：模型必须先查这里，才知道某台设备
--      到底能不能做某件事、参数能取到多少。
--    写法：用一张"按设备类型定义的能力模板"去 JOIN devices 批量插入，
--    这样新增设备时不用一条条手写能力，维护成本低。
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `device_capabilities`
    (`device_id`, `capability_code`, `display_name`, `value_type`, `unit`,
     `value_min`, `value_max`, `step`, `enum_values`, `writable`, `risk_level`, `remark`)
SELECT d.`id`, t.capability_code, t.display_name, t.value_type, t.unit,
       t.value_min, t.value_max, t.step, t.enum_values, t.writable, t.risk_level, t.remark
FROM `devices` d
JOIN (
    -- ---- 灯具：开关 + 亮度 ----
    SELECT 'light' AS device_type, 'power_on' AS capability_code, '开关' AS display_name,
           'bool' AS value_type, NULL AS unit, NULL AS value_min, NULL AS value_max,
           NULL AS step, NULL AS enum_values, 1 AS writable, 'low' AS risk_level, NULL AS remark
    UNION ALL SELECT 'light','brightness','亮度','int','%',0.0,100.0,1.0,NULL,1,'low',NULL
    -- ---- 窗帘：开合度 ----
    UNION ALL SELECT 'curtain','position','开合度','int','%',0.0,100.0,1.0,NULL,1,'low','0=全关, 100=全开'
    -- ---- 空调：开关 + 模式 + 温度 + 风速 ----
    UNION ALL SELECT 'ac','power_on','开关','bool',NULL,NULL,NULL,NULL,NULL,1,'low',NULL
    UNION ALL SELECT 'ac','mode','模式','enum',NULL,NULL,NULL,NULL,'cool,heat,fan,dry,auto',1,'low',NULL
    UNION ALL SELECT 'ac','target_temp','目标温度','int','℃',16.0,30.0,1.0,NULL,1,'low','人体舒适区 24~28℃'
    UNION ALL SELECT 'ac','fan_speed','风速','enum',NULL,NULL,NULL,NULL,'auto,low,mid,high',1,'low',NULL
    -- ---- 电视 / 音箱：开关 + 音量 ----
    UNION ALL SELECT 'tv','power_on','开关','bool',NULL,NULL,NULL,NULL,NULL,1,'low',NULL
    UNION ALL SELECT 'tv','volume','音量','int','%',0.0,100.0,1.0,NULL,1,'low',NULL
    UNION ALL SELECT 'speaker','power_on','开关','bool',NULL,NULL,NULL,NULL,NULL,1,'low',NULL
    UNION ALL SELECT 'speaker','volume','音量','int','%',0.0,100.0,1.0,NULL,1,'low',NULL
    -- ---- 加湿器 ----
    UNION ALL SELECT 'humidifier','power_on','开关','bool',NULL,NULL,NULL,NULL,NULL,1,'low',NULL
    UNION ALL SELECT 'humidifier','target_humidity','目标湿度','int','%',30.0,80.0,5.0,NULL,1,'low',NULL
    -- ---- 热水器：中风险（涉及水电）----
    UNION ALL SELECT 'water_heater','power_on','开关','bool',NULL,NULL,NULL,NULL,NULL,1,'medium',NULL
    UNION ALL SELECT 'water_heater','target_temp','目标温度','int','℃',35.0,75.0,1.0,NULL,1,'medium',NULL
    -- ---- 摄像头：默认关闭，隐私优先 ----
    UNION ALL SELECT 'camera','power_on','开关','bool',NULL,NULL,NULL,NULL,NULL,1,'medium','默认关闭；仅本地处理'
    UNION ALL SELECT 'camera','recording','录像中','bool',NULL,NULL,NULL,NULL,NULL,1,'medium',NULL
    -- ---- ★ 高风险三台：能力可写，但执行必须人工确认 ----
    UNION ALL SELECT 'door_lock','lock_state','锁状态','enum',NULL,NULL,NULL,NULL,'locked,unlocked',1,'high','开锁属高风险操作，只生成待确认项'
    UNION ALL SELECT 'gas_valve','valve_state','阀门状态','enum',NULL,NULL,NULL,NULL,'open,closed',1,'high','关阀属高风险操作，只生成待确认项'
    UNION ALL SELECT 'main_breaker','power_state','总闸状态','enum',NULL,NULL,NULL,NULL,'on,off',1,'high','拉合闸属高风险操作，只生成待确认项'
    -- ---- 传感器：全部只读 ----
    UNION ALL SELECT 'temp_sensor','temperature','温度','float','℃',-10.0,50.0,0.1,NULL,0,'low',NULL
    UNION ALL SELECT 'temp_sensor','humidity','湿度','float','%',0.0,100.0,0.1,NULL,0,'low',NULL
    UNION ALL SELECT 'air_sensor','pm25','PM2.5','float','μg/m³',0.0,500.0,0.1,NULL,0,'low',NULL
    UNION ALL SELECT 'air_sensor','co2','二氧化碳','float','ppm',400.0,5000.0,1.0,NULL,0,'low',NULL
    UNION ALL SELECT 'gas_sensor','gas_concentration','燃气浓度','float','%LEL',0.0,100.0,0.1,NULL,0,'high','只读，但是高风险事件的触发源'
    UNION ALL SELECT 'water_leak_sensor','water_detected','检测到水浸','bool',NULL,NULL,NULL,NULL,NULL,0,'high',NULL
    UNION ALL SELECT 'smoke_sensor','smoke_detected','检测到烟雾','bool',NULL,NULL,NULL,NULL,NULL,0,'high',NULL
    UNION ALL SELECT 'mmwave_radar','presence','有人','bool',NULL,NULL,NULL,NULL,NULL,0,'low','非摄像头手段，隐私优先'
    UNION ALL SELECT 'mmwave_radar','activity_level','活动量','int',NULL,0.0,100.0,1.0,NULL,0,'low',NULL
    UNION ALL SELECT 'door_sensor','open','门磁开合','bool',NULL,NULL,NULL,NULL,NULL,0,'low',NULL
) t ON t.device_type = d.`device_type`
WHERE d.`home_id` = @home_id;


-- ---------------------------------------------------------------------------
-- 7. 成员偏好（多成员温度诉求不同 —— 这正是"多用户冲突"的素材）
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `member_preferences` (`member_id`, `capability_code`, `value`, `weight`, `source`) VALUES
    (@member_dad,   'target_temp', '26', 1.000, 'manual'),
    (@member_mom,   'target_temp', '27', 1.000, 'manual'),
    (@member_grand, 'target_temp', '28', 1.000, 'manual'),   -- 老人怕冷
    (@member_child, 'target_temp', '25', 0.800, 'manual'),
    (@member_dad,   'brightness',  '40', 0.900, 'manual');   -- 观影偏好偏暗


-- ---------------------------------------------------------------------------
-- 8. 免打扰规则（对应"主动但不烦人"这条设计目标）
-- ---------------------------------------------------------------------------
INSERT INTO `disturbance_rules` (`home_id`, `level`, `quiet_start`, `quiet_end`)
SELECT @home_id, 'normal', '22:00:00', '07:00:00'
WHERE NOT EXISTS (SELECT 1 FROM `disturbance_rules` WHERE `home_id` = @home_id AND `level` = 'normal');

INSERT INTO `disturbance_rules` (`home_id`, `level`, `quiet_start`, `quiet_end`)
SELECT @home_id, 'important', '23:00:00', '06:30:00'
WHERE NOT EXISTS (SELECT 1 FROM `disturbance_rules` WHERE `home_id` = @home_id AND `level` = 'important');


-- ============================================================================
-- 自检报告
-- ============================================================================
SELECT '家庭' AS `对象`, COUNT(*) AS `数量` FROM `homes`
UNION ALL SELECT '房间', COUNT(*) FROM `rooms`              WHERE home_id = @home_id
UNION ALL SELECT '成员', COUNT(*) FROM `members`            WHERE home_id = @home_id
UNION ALL SELECT '账号', COUNT(*) FROM `users`              WHERE home_id = @home_id
UNION ALL SELECT '设备', COUNT(*) FROM `devices`            WHERE home_id = @home_id
UNION ALL SELECT '其中高风险设备', COUNT(*) FROM `devices`  WHERE home_id = @home_id AND high_risk = 1
UNION ALL SELECT '设备能力', COUNT(*) FROM `device_capabilities`
UNION ALL SELECT '成员偏好', COUNT(*) FROM `member_preferences`
UNION ALL SELECT '免打扰规则', COUNT(*) FROM `disturbance_rules` WHERE home_id = @home_id;

-- 高风险设备清单（这三台是演示"只生成待确认项"的主角）
SELECT d.`code` AS `设备编码`, d.`name` AS `设备名`, r.`name` AS `房间`,
       d.`device_type` AS `类型`, d.`high_risk` AS `高风险`
FROM `devices` d LEFT JOIN `rooms` r ON r.`id` = d.`room_id`
WHERE d.`home_id` = @home_id AND d.`high_risk` = 1;
