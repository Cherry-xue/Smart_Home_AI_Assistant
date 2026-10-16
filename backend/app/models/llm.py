"""模型单例：LLM 与 Embedding。

规划书 §13.1 给了骨架，但**有两处必须改**（选型已定）：

1. **不用 `langchain_community.embeddings.dashscope.DashScopeEmbeddings`** ——
   本项目不依赖 langchain-community（v1 之后它的模块定位已变，依赖它容易踩升级坑）。
   统一走 `langchain_openai` 的 OpenAI 兼容实现：**换厂商只改 base_url，代码零改动**。
2. 规划书示例里的 `LLM_API_KEY` / `LLM_BASE_URL` 变量名，本项目实际用
   `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL`（沿用既有 RAG 项目的约定）。

⚠️ DashScope 兼容模式的两个坑（实测）
----------------------------------
- `check_embedding_ctx_length=False` **必须显式关掉**：默认 True 时 langchain 会把文本
  先转成 token 数组（OpenAI 特有协议），DashScope 不接受这种输入，会直接报错。
- `chunk_size=10`：DashScope 的 embedding 接口**单次请求有文本条数上限**，
  一次喂太多会 400。既有 RAG 项目用的就是 10，这里沿用。
"""

from __future__ import annotations

from functools import lru_cache

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config.settings import get_settings


@lru_cache(maxsize=4)
def get_llm(temperature: float = 0.0, model: str | None = None) -> ChatOpenAI:
    """对话/工具调用模型。

    `temperature=0` 是默认值：处理设备与数据要的是确定性，不是创意。
    """
    s = get_settings()
    # 思考型模型 + 结构化输出必须关掉思考模式，否则 tool_choice 会被拒（见 settings 注释）。
    # ⚠️ 只能用 `extra_body`，不能塞进 `model_kwargs`：前者才是 OpenAI SDK 的顶层字段。
    extra: dict[str, bool] = {"enable_thinking": False} if s.llm_disable_thinking else {}
    return ChatOpenAI(
        model=model or s.qwen_model_name,
        api_key=s.dashscope_api_key,
        base_url=s.dashscope_base_url,
        temperature=temperature,
        timeout=60,
        max_retries=2,
        extra_body=extra or None,
    )


@lru_cache(maxsize=1)
def get_embeddings() -> OpenAIEmbeddings:
    """向量化模型（灌库与检索共用同一份，保证向量空间一致）。"""
    s = get_settings()
    return OpenAIEmbeddings(
        model=s.embedding_model_name,
        api_key=s.dashscope_api_key,
        base_url=s.dashscope_base_url,
        chunk_size=10,                      # DashScope 单次请求条数上限（见模块说明）
        check_embedding_ctx_length=False,   # 必须关：否则会发 token 数组，DashScope 不接受
    )
