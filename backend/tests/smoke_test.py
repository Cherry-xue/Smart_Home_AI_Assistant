# ============================================================
# M0 冒烟测试：确认工具链真的可用
#
# 运行：conda activate ai0707
#       cd <项目根>
#       python backend/tests/smoke_test.py
#
# 检查四类：
#   1) 8 条 v1 API import（LangChain v1 / LangGraph v1 的写法是否成立）
#   2) 依赖包 import 完整性
#   3) 远程 MySQL 连通性（读 .env）
#   4) DashScope 生成 / 向量模型可用性（读 .env）
#   5) MQTT Broker 连通性（未装 broker 时记为 SKIP，不算失败）
#
# 设计原则：单项失败不影响其他项，最后给汇总表。
# ============================================================

from __future__ import annotations

import json
import pathlib
import socket
import sys
import urllib.error
import urllib.request

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    icon = {PASS: "[OK]  ", FAIL: "[FAIL]", SKIP: "[SKIP]"}[status]
    print(f"  {icon} {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------- 1) v1 API
print("=" * 72)
print("1) LangChain / LangGraph v1 API 关键 import")
print("=" * 72)

V1_IMPORTS = [
    ("langchain.agents.create_agent", "from langchain.agents import create_agent"),
    ("middleware: ModelCallLimit", "from langchain.agents.middleware import ModelCallLimitMiddleware"),
    ("middleware: ToolCallLimit", "from langchain.agents.middleware import ToolCallLimitMiddleware"),
    ("middleware: HITL 配置", "from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig"),
    ("langgraph interrupt/Command", "from langgraph.types import interrupt, Command"),
    ("langgraph StateGraph", "from langgraph.graph import StateGraph, START, END"),
    ("checkpointer: PyMySQLSaver", "from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver"),
    ("ChatOpenAI", "from langchain_openai import ChatOpenAI"),
]

for label, stmt in V1_IMPORTS:
    try:
        exec(stmt, {})
        record(label, PASS)
    except Exception as e:  # noqa: BLE001
        record(label, FAIL, f"{type(e).__name__}: {e}")

# ------------------------------------------------------------ 2) 依赖完整性
print()
print("=" * 72)
print("2) 依赖包 import")
print("=" * 72)

DEPS = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic_settings"),
    ("dotenv", "dotenv"),
    ("sqlalchemy", "sqlalchemy"),
    ("pymysql", "pymysql"),
    ("alembic", "alembic"),
    ("sqlglot", "sqlglot"),
    ("aiomqtt", "aiomqtt"),
    ("paho.mqtt", "paho.mqtt"),
    ("yaml", "yaml"),
    ("langchain", "langchain"),
    ("langgraph", "langgraph"),
    ("langchain_openai", "langchain_openai"),
    ("langchain_chroma", "langchain_chroma"),
    ("langchain_text_splitters", "langchain_text_splitters"),
    ("chromadb", "chromadb"),
    ("pypdf", "pypdf"),
    ("dashscope", "dashscope"),
    ("tiktoken", "tiktoken"),
    ("apscheduler", "apscheduler"),
    ("jwt", "jwt"),
    ("loguru", "loguru"),
    ("httpx", "httpx"),
    ("chroma 版本", "chromadb"),
]

seen: set[str] = set()
for label, mod in DEPS:
    if mod in seen:
        continue
    seen.add(mod)
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "")
        record(label, PASS, f"v{ver}" if ver else "")
    except Exception as e:  # noqa: BLE001
        record(label, FAIL, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------- 读 .env
root = pathlib.Path(__file__).resolve().parents[2]
env_path = root / ".env"
env: dict[str, str] = {}
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            env[k.strip()] = v.strip()
print()
print(f"(配置文件：{env_path}  存在={env_path.exists()}  变量数={len(env)})")

# -------------------------------------------------------------- 3) MySQL
print()
print("=" * 72)
print("3) 远程 MySQL 连通性")
print("=" * 72)

mysql_ok = False
if not env.get("MYSQL_HOST"):
    record("MySQL 连接", SKIP, ".env 缺少 MYSQL_HOST")
else:
    try:
        import pymysql

        conn = pymysql.connect(
            host=env["MYSQL_HOST"], port=int(env.get("MYSQL_PORT", 3306)),
            user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"],
            charset="utf8mb4", connect_timeout=8,
        )
        cur = conn.cursor()
        cur.execute("SELECT VERSION(), @@character_set_server, CURRENT_USER()")
        ver, cs, who = cur.fetchone()
        record("MySQL 连接", PASS, f"{who} @ {ver} / {cs}")
        cur.execute("SHOW DATABASES LIKE 'smart_home%'")
        existing = [r[0] for r in cur.fetchall()]
        record("业务库 / checkpoint 库", PASS if existing else SKIP,
               f"已存在: {existing}" if existing else "尚未创建（M1 建库）")
        conn.close()
        mysql_ok = True
    except Exception as e:  # noqa: BLE001
        record("MySQL 连接", FAIL, f"{type(e).__name__}: {e}")

# ----------------------------------------------------------- 4) DashScope
print()
print("=" * 72)
print("4) DashScope 模型可用性")
print("=" * 72)


def post(path: str, payload: dict) -> tuple[int | None, object]:
    req = urllib.request.Request(
        f"{env['DASHSCOPE_BASE_URL']}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {env['DASHSCOPE_API_KEY']}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())["error"].get("code", "")
        except Exception:  # noqa: BLE001
            return e.code, "unparsable"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


if not env.get("DASHSCOPE_API_KEY"):
    record("生成模型", SKIP, ".env 缺少 DASHSCOPE_API_KEY")
else:
    # ⚠️ 兜底值也必须带版本号：动态别名（qwen-plus / -latest）在"免费额度用完即停"
    # 模式下会被判为无免费额度，直接 403（详见 settings.py 的说明）
    model = env.get("QWEN_MODEL_NAME", "qwen-plus-2025-11-05")
    st, res = post("/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：正常"}],
        "max_tokens": 16, "temperature": 0,
    })
    if st == 200:
        record(f"生成模型 {model}", PASS, repr(res["choices"][0]["message"]["content"].strip()))
    else:
        record(f"生成模型 {model}", FAIL, f"HTTP {st} {res}")

    st, res = post("/embeddings", {
        "model": env.get("EMBEDDING_MODEL_NAME", "qwen3.7-text-embedding"),
        "input": "客厅温度二十六度",
    })
    if st == 200:
        record(f"向量模型 {env.get('EMBEDDING_MODEL_NAME')}", PASS,
               f"维度 {len(res['data'][0]['embedding'])}")
    else:
        record(f"向量模型 {env.get('EMBEDDING_MODEL_NAME')}", FAIL, f"HTTP {st} {res}")

# --------------------------------------------------------------- 5) MQTT
print()
print("=" * 72)
print("5) MQTT Broker 连通性")
print("=" * 72)

mqtt_host = env.get("MQTT_HOST", "127.0.0.1")
mqtt_port = int(env.get("MQTT_PORT", 1883))
try:
    s = socket.socket()
    s.settimeout(5)
    s.connect((mqtt_host, mqtt_port))
    cid = b"smart_home_smoke"
    payload = b"\x00\x04MQTT\x04\x02\x00\x3c" + len(cid).to_bytes(2, "big") + cid
    s.sendall(b"\x10" + bytes([len(payload)]) + payload)
    resp = s.recv(4)
    s.close()
    if resp[:1] == b"\x20":
        record(f"MQTT {mqtt_host}:{mqtt_port}", PASS, f"CONNACK {resp.hex()}")
    else:
        record(f"MQTT {mqtt_host}:{mqtt_port}", FAIL, f"CONNACK 异常 {resp.hex()}")
except Exception as e:  # noqa: BLE001
    record(f"MQTT {mqtt_host}:{mqtt_port}", SKIP,
           f"未连通（{type(e).__name__}）——装了 Mosquitto 或改指公共 broker 后重跑")

# ------------------------------------------------------------------ 汇总
print()
print("=" * 72)
n_pass = sum(1 for _, s, _ in results if s == PASS)
n_fail = sum(1 for _, s, _ in results if s == FAIL)
n_skip = sum(1 for _, s, _ in results if s == SKIP)
print(f"汇总：PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}（共 {len(results)} 项）")
if n_fail:
    print("\n以下项目失败（需要处理）：")
    for name, status, detail in results:
        if status == FAIL:
            print(f"  - {name}: {detail}")
else:
    print("\n没有失败项。")
print("=" * 72)

sys.exit(1 if n_fail else 0)
