"""
全局配置：从项目根目录的 .env 读取，用 pydantic-settings 做强类型校验。

设计要点
--------
1. **env_file 允许两处**：项目内 .env 优先，上级目录兜底（复用既有课程的集群凭据）。
2. **连接串用 URL.create() 从分量拼**，不手拼字符串。
   原因：密码里含 `@` 这类字符时必须 URL 编码，手拼极易出错（写成
   `AppRw@2026@192.168...` 会被解析成主机名的一部分，报错方向还是"连不上主机"，
   非常误导）。用 URL.create() 天然免转义。
3. 三个连接身份分开：应用读写(app_rw) / Agent 只读(agent_ro) / 管理(root)。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# backend/app/config/settings.py -> 上溯 4 层为项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
# 上级目录：D:/Code/PycharmProjects/AI0707/.env（兜底用）
PARENT_ENV = PROJECT_ROOT.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # ⚠️ 顺序有讲究：pydantic-settings 对多个 env_file 是**后加载的覆盖先加载的**，
        # 所以"项目内优先"必须写成「上级在前、项目在后」。
        #
        # 这里踩过一次：原先写成 (项目, 上级) 与注释里的"项目内优先"正好相反，
        # 于是上级 .env 的 QWEN_MODEL_NAME=qwen-turbo 悄悄盖掉了项目内的 qwen-plus。
        # 症状是模型调用稳定 429 limit_requests（而 curl 直连同一个 key 却 200），
        # 排查方向一度被带到"配额用尽"，实际上只是模型名被换掉了。
        env_file=(PARENT_ENV, PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 运行环境 ----------------
    app_env: str = "development"
    # mock / mqtt / csv / real_gateway —— 上层对数据来源无感
    data_mode: str = "mqtt"
    # LangGraph 会话存档：memory（默认，重启即丢） / mysql（独立库 smart_home_checkpoint）
    checkpoint_backend: str = "memory"
    # 业务时区。分时电价（峰谷时段）与作息节律都按本地时间定义，
    # 所以入库统一转成本地时间（见 app/mqtt/envelope.py 的 to_local_naive）。
    # 跨时区部署时改这里一处即可。
    timezone: str = "Asia/Shanghai"

    # ---------------- 大模型（DashScope）----------------
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # ⚠️⚠️ 模型名**必须写具体版本号**，不要写 `qwen-plus` / `qwen-plus-latest` 这类
    # 动态别名——实测（2026-10-15 同一 key 同一时刻）：
    #   qwen-plus ❌ 403 / qwen-plus-latest ❌ 403      ← 别名一律被判"无免费额度"
    #   qwen-plus-2025-01-25 ✅ / -04-28 ✅ / -07-14 ✅ / -11-05 ✅
    # 原因：免费额度按「具体模型版本」发放，别名不做映射。
    #
    # ⚠️⚠️ 更关键：不同模型版本的 **QPM（每分钟请求数）配额差异巨大**，而多 Agent
    # 一次对话要连续发 5~10 个请求（router + Agent 工具循环 + respond），很容易撞限。
    # 实测（2026-10-15 同一 key）：
    #   qwen3.7-plus           单次/连发10/并发5  → 全过 ✅（无 QPM 压力）
    #   qwen-plus-2025-11-05   连发 10 次被拒 3 次、并发 5 次被拒 1 次
    #                          错误码 429 limit_requests（请求速率超限，不是额度问题）
    # → 选模型要看 QPM 而不只是能力档位；换版本前务必实测"连发 + 并发"。
    qwen_model_name: str = "qwen3.7-plus"

    # ⚠️ 思考型模型（qwen3.x 系列）在思考模式下**拒绝 object 形式的 tool_choice**，
    # 而我们的结构化输出（`response_format=Finding`）正是用它 →
    # 400 "The tool_choice parameter does not support being set to required or object
    # in thinking mode"。关掉思考模式即可（实测结构化输出照常返回）。
    # 非思考模型（qwen-plus-2025-xx）传这个参数无害，所以默认开。
    llm_disable_thinking: bool = True
    embedding_model_name: str = "qwen3.7-text-embedding"
    # 注意：百炼实际模型 ID 是 qwen3.7-text-rerank（不是 qwen3-rerank）
    rerank_model_name: str = "qwen3.7-text-rerank"
    tavily_api_key: str = ""

    # ---------------- MySQL ----------------
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_db: str = "smart_home"
    mysql_db_checkpoint: str = "smart_home_checkpoint"

    # 应用读写账号（默认用它）
    mysql_user: str = "app_rw"
    mysql_password: str = ""
    # Agent 侧只读账号（只有 SELECT）
    mysql_user_ro: str = "agent_ro"
    mysql_password_ro: str = ""
    # 管理账号（仅建库/DDL 时临时用，禁止出现在业务代码里）
    mysql_user_admin: str = ""
    mysql_password_admin: str = ""

    # 可选：直接给整串（若填了，优先于上面的分量）
    database_url: str = ""
    checkpoint_database_url: str = ""

    # ---------------- MQTT ----------------
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_client_id_prefix: str = "smart_home_sim"

    # ---------------- 本服务监听地址（开发用本地直跑）----------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    #: 允许跨域的来源，逗号分隔（如 `http://192.168.198.64:5173,http://localhost:5173`）。
    #: 留空 = 不启用 CORS —— Web 端走 Vite proxy 同源转发，本来就不需要。
    #: ⚠️ 手机上的 **H5 版**（浏览器内核）会带 Origin，直连后端时必须在列表里；
    #: 原生 App 的请求不带 Origin，不受 CORS 约束。
    #: 开发期图省事可以填 `*`，但要清楚那等于对任何网页开放这个接口。
    cors_origins: str = ""
    #: 是否允许在**局域网**上访问（手机连电脑时必须开）。
    #: 它只是把 api_host 的解释放宽，不改变"默认只监听本机"的立场。
    allow_lan: bool = False

    # ---------------- 安全边界 ----------------
    # 门锁 / 燃气阀 / 总闸 / 布防撤防的自动执行：永久保持 False
    high_risk_auto_exec: bool = False
    quiet_hours_default: str = "22:00-07:00"

    # ---------------- 派生连接串 ----------------
    @staticmethod
    def _build(host: str, port: int, user: str, password: str, db: str) -> URL:
        return URL.create(
            drivername="mysql+pymysql",
            username=user,
            password=password,
            host=host,
            port=port,
            database=db,
            query={"charset": "utf8mb4"},
        )

    @property
    def db_url(self) -> URL:
        """业务库，应用读写身份。"""
        return self._build(
            self.mysql_host, self.mysql_port,
            self.mysql_user, self.mysql_password, self.mysql_db,
        )

    @property
    def db_url_ro(self) -> URL:
        """业务库，只读身份（护栏第 1 层：Agent 侧查询用）。"""
        user = self.mysql_user_ro or self.mysql_user
        pwd = self.mysql_password_ro or self.mysql_password
        return self._build(self.mysql_host, self.mysql_port, user, pwd, self.mysql_db)

    @property
    def checkpoint_url(self) -> URL:
        """LangGraph checkpoint 专用库（与业务库分开）。"""
        return self._build(
            self.mysql_host, self.mysql_port,
            self.mysql_user, self.mysql_password, self.mysql_db_checkpoint,
        )

    # ---------------- 便利属性 ----------------
    @property
    def tzinfo(self) -> ZoneInfo:
        """业务时区对象（入库时间戳统一按它转成 naive 本地时间）。"""
        return ZoneInfo(self.timezone)

    @property
    def effective_api_host(self) -> str:
        """实际监听地址。

        `allow_lan=True` 时监听 `0.0.0.0`（局域网可达，手机/平板才能连进来）。
        默认仍是 `127.0.0.1` —— "默认只监听本机"是安全立场，放开要显式声明。
        """
        if self.api_host in ("0.0.0.0", "::"):
            return self.api_host
        return "0.0.0.0" if self.allow_lan else self.api_host

    @property
    def cors_origin_list(self) -> list[str]:
        """解析逗号分隔的来源白名单。"""
        return [x.strip() for x in (self.cors_origins or "").split(",") if x.strip()]

    def is_high_risk_auto_exec_enabled(self) -> bool:
        """高风险自动执行开关。永远返回 False——即使 .env 被改成 true 也不放行。

        规划书 §11.3 明确要求这条"不可被 prompt 绕过"，所以在代码层再挡一次：
        想启用必须改这里，而不是改环境变量。
        """
        return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
