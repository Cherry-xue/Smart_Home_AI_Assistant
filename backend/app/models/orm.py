"""
SQLAlchemy 2.0 声明式模型 —— 对应 `mysql/init/01_schema.sql` 里的 24 张表。

维护约定
--------
1. **字段 COMMENT 不在这里重复**。数据库才是 COMMENT 的唯一来源
   （模型读表结构时读的就是 DB 里的 COMMENT），两处维护必然漂移。
   这里只写表级 docstring，字段含义请查 `01_schema.sql`。
2. 表结构变更走 `01_schema.sql`（在虚拟机本地执行），**不要用 create_all() 改线上表**。
   `tests/schema_check.py` 会把本文件与真实库做逐表逐字段比对，防止两边跑偏。
3. 类型用 MySQL 方言（本项目只跑 MySQL），`BIGINT UNSIGNED` / `TINYINT(1)` / `ENUM` 等
   与 DDL 一一对应，不追求跨库可移植。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 常用类型的简写（只读复用，不要在列上再改属性）
PK = mysql.BIGINT(unsigned=True)
FK = mysql.BIGINT(unsigned=True)
JSON = mysql.JSON
DT3 = mysql.DATETIME(fsp=3)


class Base(DeclarativeBase):
    """声明式基类。"""


# ============================================================================
# 一、基础对象
# ============================================================================


class Home(Base):
    """家庭主体（地址脱敏存储）。"""

    __tablename__ = "homes"
    __table_args__ = {"comment": "家庭主体（地址脱敏存储）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(32), server_default=text("'Asia/Shanghai'"))
    address_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class Room(Base):
    """房间/区域。`code` 会直接用在 MQTT topic 里。"""

    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("home_id", "code", name="uk_rooms_home_code"),
        {"comment": "房间/区域（客厅、主卧、厨房…）"},
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    code: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(48))
    floor: Mapped[int] = mapped_column(mysql.TINYINT, server_default=text("1"))
    sort_no: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class Member(Base):
    """家庭成员画像（是"人"，不是登录账号）。"""

    __tablename__ = "members"
    __table_args__ = {"comment": "家庭成员画像（户主/老人/儿童/访客）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    nickname: Mapped[str] = mapped_column(String(32))
    role_code: Mapped[str] = mapped_column(String(32))
    birth_year: Mapped[int | None] = mapped_column(SmallInteger)
    voice_print_enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class User(Base):
    """登录账号。与 Member 分开：一个人可以有账号，账号也可以属于访客/运维。"""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uk_users_username"),
        {"comment": "登录账号（密码只存哈希）"},
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    member_id: Mapped[int | None] = mapped_column(FK, ForeignKey("members.id"))
    username: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class UserRole(Base):
    """角色与数据/设备范围（最小权限落地）。"""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_code", name="uk_user_roles"),
        {"comment": "角色与数据/设备范围（最小权限落地）"},
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(FK, ForeignKey("users.id"))
    role_code: Mapped[str] = mapped_column(String(32))
    room_scope: Mapped[str] = mapped_column(String(255), server_default=text("'*'"))
    can_confirm_high_risk: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class Device(Base):
    """设备实例。`high_risk=1` 的设备永远不会被自动执行，只能生成待确认建议。"""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("home_id", "code", name="uk_devices_home_code"),
        {"comment": "设备实例（含是否高风险标记）"},
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    room_id: Mapped[int | None] = mapped_column(FK, ForeignKey("rooms.id"))
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    device_type: Mapped[str] = mapped_column(String(48))
    protocol: Mapped[str] = mapped_column(String(32), server_default=text("'mqtt'"))
    vendor: Mapped[str | None] = mapped_column(String(64))
    high_risk: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class DeviceCapability(Base):
    """设备能力档案 —— 防模型臆造设备功能的核心表。

    模型必须先查这里，才知道某台设备能不能做某件事、参数范围多少。
    """

    __tablename__ = "device_capabilities"
    __table_args__ = (
        UniqueConstraint("device_id", "capability_code", name="uk_dev_cap"),
        {"comment": "设备能力档案（防模型臆造设备功能的第一道防线）"},
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(FK, ForeignKey("devices.id", ondelete="CASCADE"))
    capability_code: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(64))
    value_type: Mapped[str] = mapped_column(
        mysql.ENUM("bool", "int", "float", "enum", "string")
    )
    unit: Mapped[str | None] = mapped_column(String(16))
    value_min: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    value_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    step: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    enum_values: Mapped[str | None] = mapped_column(String(255))
    writable: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    risk_level: Mapped[str] = mapped_column(
        mysql.ENUM("low", "medium", "high"), server_default=text("'low'")
    )
    remark: Mapped[str | None] = mapped_column(String(255))


class MemberPreference(Base):
    """成员偏好（多人偏好冲突时用 weight 做协商裁决）。"""

    __tablename__ = "member_preferences"
    __table_args__ = (
        UniqueConstraint("member_id", "capability_code", name="uk_member_pref"),
        {"comment": "成员偏好（温度/亮度/作息等，含来源与权重）"},
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(FK, ForeignKey("members.id", ondelete="CASCADE"))
    capability_code: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(64))
    weight: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default=text("1.000"))
    source: Mapped[str] = mapped_column(String(32), server_default=text("'manual'"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )


# ============================================================================
# 二、运行态与事件
# ============================================================================


class DeviceReading(Base):
    """遥测值。按"指标"存（EAV）：一条消息里的多个 metric 存多行。"""

    __tablename__ = "device_readings"
    __table_args__ = {"comment": "设备遥测值（含质量标记与来源，过期不得伪装实时）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(FK)
    observed_at: Mapped[datetime] = mapped_column(DT3)
    metric_code: Mapped[str] = mapped_column(String(64))
    value_num: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    value_text: Mapped[str | None] = mapped_column(String(128))
    unit: Mapped[str | None] = mapped_column(String(16))
    quality: Mapped[str] = mapped_column(String(16), server_default=text("'good'"))
    is_stale: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    seq: Mapped[int | None] = mapped_column(mysql.BIGINT(unsigned=True))
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class EnergyReading(Base):
    """用能明细，支持分时电价峰平谷。"""

    __tablename__ = "energy_readings"
    __table_args__ = {"comment": "用能明细（支持分时电价峰平谷）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(FK)
    observed_at: Mapped[datetime] = mapped_column(DT3)
    power_w: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    tariff_period: Mapped[str | None] = mapped_column(mysql.ENUM("peak", "flat", "valley"))
    source: Mapped[str] = mapped_column(String(32), server_default=text("'simulator'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class DeviceCommand(Base):
    """指令记录。

    高风险指令必须经过 confirmed 才可执行；`status` 的数据库默认值就是
    `pending_confirm` —— 注意是"默认拒绝"而不是"默认放行"。
    """

    __tablename__ = "device_commands"
    __table_args__ = {"comment": "指令记录（高风险指令必须 confirmed 后才执行）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(FK, ForeignKey("devices.id"))
    command: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[dict | None] = mapped_column(JSON)
    issued_by: Mapped[str | None] = mapped_column(String(64))
    risk_level: Mapped[str] = mapped_column(
        mysql.ENUM("low", "medium", "high"), server_default=text("'low'")
    )
    status: Mapped[str] = mapped_column(
        mysql.ENUM("pending_confirm", "confirmed", "rejected", "executed", "failed", "expired"),
        server_default=text("'pending_confirm'"),
    )
    confirmed_by: Mapped[int | None] = mapped_column(FK)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)
    result_message: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class Scene(Base):
    """场景定义（睡眠/观影/起床/离家）。"""

    __tablename__ = "scenes"
    __table_args__ = {"comment": "场景定义（睡眠/观影/起床/离家）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    name: Mapped[str] = mapped_column(String(64))
    trigger_type: Mapped[str] = mapped_column(
        mysql.ENUM("manual", "schedule", "sensor", "voice"), server_default=text("'manual'")
    )
    trigger_config_json: Mapped[dict | None] = mapped_column(JSON)
    definition_json: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[int | None] = mapped_column(FK)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class SceneRun(Base):
    """场景执行记录。`failed_steps` 存被剔除/失败的步骤（演示冲突检测就靠它）。"""

    __tablename__ = "scene_runs"
    __table_args__ = {"comment": "场景执行记录（含失败步骤与降级标记）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    scene_id: Mapped[int] = mapped_column(FK, ForeignKey("scenes.id"))
    started_at: Mapped[datetime] = mapped_column(DT3, server_default=text("CURRENT_TIMESTAMP(3)"))
    finished_at: Mapped[datetime | None] = mapped_column(DT3)
    steps_json: Mapped[dict | None] = mapped_column(JSON)
    failed_steps: Mapped[dict | None] = mapped_column(JSON)
    fallback_used: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        mysql.ENUM("running", "success", "partial", "failed"), server_default=text("'running'")
    )


class SensorEvent(Base):
    """传感器事件（燃气/水浸/烟感/入侵/门窗）。"""

    __tablename__ = "sensor_events"
    __table_args__ = {"comment": "传感器事件（燃气/水浸/烟感/入侵/门窗）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(FK, ForeignKey("devices.id"))
    event_code: Mapped[str] = mapped_column(String(48))
    level: Mapped[str] = mapped_column(
        mysql.ENUM("info", "warning", "critical"), server_default=text("'warning'")
    )
    started_at: Mapped[datetime] = mapped_column(DT3)
    ended_at: Mapped[datetime | None] = mapped_column(DT3)
    payload: Mapped[dict | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32), server_default=text("'mqtt'"))


class Alarm(Base):
    """告警及人工确认状态。"""

    __tablename__ = "alarms"
    __table_args__ = {"comment": "告警及人工确认状态"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    room_id: Mapped[int | None] = mapped_column(FK)
    device_id: Mapped[int | None] = mapped_column(FK)
    alarm_code: Mapped[str] = mapped_column(String(48))
    level: Mapped[str] = mapped_column(
        mysql.ENUM("info", "warning", "critical"), server_default=text("'warning'")
    )
    title: Mapped[str] = mapped_column(String(128))
    detail: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        mysql.ENUM("active", "acknowledged", "resolved", "dismissed"),
        server_default=text("'active'"),
    )
    acknowledged_by: Mapped[int | None] = mapped_column(FK)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class CareObservation(Base):
    """照护观察。只用非摄像头手段，结论只能表述为"待确认线索"。"""

    __tablename__ = "care_observations"
    __table_args__ = {"comment": "照护观察（活动节律/用药打卡，仅非摄像头数据源）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(FK, ForeignKey("members.id"))
    observed_at: Mapped[datetime] = mapped_column(DT3)
    activity_type: Mapped[str] = mapped_column(String(48))
    duration_min: Mapped[int | None] = mapped_column(Integer)
    source_device: Mapped[int | None] = mapped_column(FK)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    remark: Mapped[str | None] = mapped_column(String(255))


# ============================================================================
# 三、知识、记忆与审计
# ============================================================================


class KnowledgeDocument(Base):
    """知识文档元数据。只有 status='active' 才允许进入检索集合。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = {"comment": "知识文档元数据（草稿/现行/废止状态控制检索范围）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128))
    doc_type: Mapped[str] = mapped_column(String(48), server_default=text("'device_manual'"))
    version: Mapped[str] = mapped_column(String(32), server_default=text("'v1'"))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        mysql.ENUM("draft", "active", "archived"), server_default=text("'draft'")
    )
    approved_by: Mapped[int | None] = mapped_column(FK)
    room_scope: Mapped[str] = mapped_column(String(255), server_default=text("'all'"))
    file_path: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class KnowledgeChunk(Base):
    """知识片段原文。向量存 Chroma，这里只留可追溯引用（chroma_id）。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = {"comment": "知识片段原文（向量存 Chroma，此处仅留可追溯引用）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(FK, ForeignKey("knowledge_documents.id", ondelete="CASCADE"))
    page_or_section: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    chroma_id: Mapped[str | None] = mapped_column(String(64))
    room_scope: Mapped[str] = mapped_column(String(255), server_default=text("'all'"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class Memory(Base):
    """长期记忆。必须带 source 与 confidence；低置信度不参与个性化（防记忆污染）。"""

    __tablename__ = "memories"
    __table_args__ = {"comment": "长期记忆（含来源/置信度/遗忘时间，可人工修正删除）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    member_id: Mapped[int | None] = mapped_column(FK)
    memory_type: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    chroma_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default=text("0.800"))
    decay_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    )


class Suggestion(Base):
    """主动建议（含推荐原因与打扰等级，实现"可解释、可关闭"）。"""

    __tablename__ = "suggestions"
    __table_args__ = {"comment": "主动建议（含推荐原因与打扰等级）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    member_id: Mapped[int | None] = mapped_column(FK)
    content: Mapped[str] = mapped_column(String(512))
    reason_json: Mapped[dict | None] = mapped_column(JSON)
    disturbance_level: Mapped[str] = mapped_column(
        mysql.ENUM("urgent", "important", "normal", "silent"), server_default=text("'normal'")
    )
    status: Mapped[str] = mapped_column(
        mysql.ENUM("pending", "shown", "accepted", "dismissed"), server_default=text("'pending'")
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class DisturbanceRule(Base):
    """免打扰配置（按打扰等级分档）。"""

    __tablename__ = "disturbance_rules"
    __table_args__ = {"comment": "免打扰配置（按打扰等级分档）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    home_id: Mapped[int] = mapped_column(FK, ForeignKey("homes.id"))
    member_id: Mapped[int | None] = mapped_column(FK)
    level: Mapped[str] = mapped_column(
        mysql.ENUM("urgent", "important", "normal", "silent"), server_default=text("'normal'")
    )
    quiet_start: Mapped[time] = mapped_column(Time)
    quiet_end: Mapped[time] = mapped_column(Time)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("1"))


class Conversation(Base):
    """用户会话。thread_id 与 LangGraph checkpoint 库关联。"""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uk_conv_thread"),
        {"comment": "用户会话（thread_id 关联 LangGraph checkpoint）"},
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(FK)
    home_id: Mapped[int | None] = mapped_column(FK)
    thread_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class AgentRun(Base):
    """Agent 执行记录。status='interrupted' 表示停在人工确认节点。"""

    __tablename__ = "agent_runs"
    __table_args__ = {"comment": "Agent 执行记录与工具调用摘要"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int | None] = mapped_column(FK)
    intent: Mapped[str | None] = mapped_column(String(64))
    route_agent: Mapped[str | None] = mapped_column(String(48))
    tools_used: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        mysql.ENUM("running", "success", "failed", "interrupted"), server_default=text("'running'")
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class AuditEvent(Base):
    """审计日志（查看/导出/确认/配置变更）。"""

    __tablename__ = "audit_events"
    __table_args__ = {"comment": "审计日志（查看/导出/确认/配置变更）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(FK)
    action: Mapped[str] = mapped_column(String(48))
    object_type: Mapped[str | None] = mapped_column(String(48))
    object_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


class ChatMessage(Base):
    """对话消息（历史会话回放的正文）。

    `conversations` 只记会话本身、`agent_runs` 只记执行元数据，两边都没有正文，
    所以大屏右侧的「历史会话」需要这张表才能把上一轮问答原样还原。
    """

    __tablename__ = "chat_messages"
    __table_args__ = {"comment": "对话消息（历史会话回放）"}

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(FK)
    thread_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(mysql.ENUM("user", "assistant"))
    content: Mapped[str | None] = mapped_column(mysql.MEDIUMTEXT)
    sections: Mapped[dict | None] = mapped_column(JSON)
    trace: Mapped[list | None] = mapped_column(JSON)
    interrupted: Mapped[bool] = mapped_column(Boolean, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"))


#: 全部模型（供 schema_check 与 metadata 使用）
ALL_MODELS = [
    Home, Room, Member, User, UserRole, Device, DeviceCapability, MemberPreference,
    DeviceReading, EnergyReading, DeviceCommand, Scene, SceneRun, SensorEvent,
    Alarm, CareObservation,
    KnowledgeDocument, KnowledgeChunk, Memory, Suggestion, DisturbanceRule,
    Conversation, AgentRun, AuditEvent, ChatMessage,
]
