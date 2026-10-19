"""历史会话：把每一轮问答落库，供大屏右侧的「历史会话」回放。

为什么不让 Agent 自己去存
------------------------
落库属于"展示层需求"，跟 Agent 的推理无关。放在 API 层收尾处，
Agent 图本身保持纯粹（`agent_check` 里用 stub graph 跑也不会碰库）。

为什么自己开 Session
--------------------
调用点有两种：① 普通端点（已持有 `Depends(get_db)` 的 Session）；
② SSE 生成器（没有请求级 Session，且在事件循环里）。统一由本模块
自己开短会话、自己提交，调用方就不必关心生命周期；
SSE 那边用线程池调用即可（`starlette.concurrency.run_in_threadpool`）。

⚠️ 落库失败**不能影响对话本身**：问答已经生成好了，写不进历史只是少一条记录，
不能让用户看到 500。所以这里所有函数对调用方都"尽力而为"（异常吞掉并记日志），
只有显式查询（列表/详情）才把异常抛出去。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select

from app.db.session import SessionLocal
from app.models.orm import ChatMessage, Conversation

log = logging.getLogger("app.services.chat_history")

#: 会话标题取首句提问的前 N 个字符（conversations.title 是 VARCHAR(128)）
TITLE_MAX = 40
#: 单次回放最多取多少条消息（防止一个超长会话把页面拖死）
MESSAGES_LIMIT = 200


def _title_of(question: str | None) -> str:
    text = (question or "").strip().replace("\n", " ")
    if not text:
        return "新会话"
    return text[:TITLE_MAX]


def _ensure_conversation(db, thread_id: str, *, home_id: int | None, title: str) -> Conversation:
    """按 thread_id 取会话，没有就建一条（首次落库时建）。"""
    conv = db.scalar(select(Conversation).where(Conversation.thread_id == thread_id))
    if conv is None:
        conv = Conversation(thread_id=thread_id, home_id=home_id, title=title)
        db.add(conv)
        db.flush()
        return conv
    # 老会话标题为空时补上；已有标题不覆盖（用户先问的那句才是"这个会话在聊什么"）
    if not conv.title:
        conv.title = title
    if conv.home_id is None and home_id is not None:
        conv.home_id = home_id
    return conv


def record_turn(
    thread_id: str,
    *,
    question: str,
    answer: str,
    home_id: int | None = None,
    sections: dict[str, Any] | None = None,
    trace: list[dict[str, Any]] | None = None,
    interrupted: bool = False,
) -> None:
    """把一轮问答（用户提问 + 助手回答）写进库。

    ⚠️ 一次调用写**两条**消息：提问和回答。分两条而不是一行存问答，
    是因为前端回放时要按时间顺序渲染成气泡，两条更自然；
    将来要支持多轮追问也只管继续 append。
    """
    try:
        with SessionLocal() as db:
            conv = _ensure_conversation(db, thread_id, home_id=home_id, title=_title_of(question))
            db.add(
                ChatMessage(
                    conversation_id=conv.id,
                    thread_id=thread_id,
                    role="user",
                    content=question,
                )
            )
            db.add(
                ChatMessage(
                    conversation_id=conv.id,
                    thread_id=thread_id,
                    role="assistant",
                    content=answer or "",
                    sections=sections or None,
                    trace=trace or None,
                    interrupted=interrupted,
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("历史会话落库失败 thread=%s（不影响本次对话）", thread_id)


def record_message(
    thread_id: str,
    role: str,
    content: str,
    *,
    home_id: int | None = None,
    sections: dict[str, Any] | None = None,
    trace: list[dict[str, Any]] | None = None,
) -> None:
    """单独追加一条消息（例如人工确认之后系统补的那句结论）。"""
    try:
        with SessionLocal() as db:
            conv = _ensure_conversation(db, thread_id, home_id=home_id, title=_title_of(content))
            db.add(
                ChatMessage(
                    conversation_id=conv.id,
                    thread_id=thread_id,
                    role=role,
                    content=content or "",
                    sections=sections or None,
                    trace=trace or None,
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001
        log.exception("历史会话追加消息失败 thread=%s", thread_id)


def list_sessions(home_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """历史会话列表（最近说过的排前面）。

    排序用**最后一条消息的时间**而不是会话创建时间：一个会话可能被接着聊了很多轮，
    按创建时间排会让活跃会话沉底。
    """
    with SessionLocal() as db:
        last_at = func.max(ChatMessage.created_at)
        msg_count = func.count(ChatMessage.id)
        stmt = (
            select(Conversation, last_at, msg_count)
            .join(
                ChatMessage,
                ChatMessage.conversation_id == Conversation.id,
                isouter=True,
            )
            .group_by(Conversation.id)
            .order_by(func.coalesce(last_at, Conversation.created_at).desc())
            .limit(max(1, min(limit, 100)))
        )
        if home_id is not None:
            stmt = stmt.where(Conversation.home_id == home_id)

        rows = db.execute(stmt).all()
        return [
            {
                "thread_id": conv.thread_id,
                "title": conv.title or "新会话",
                "home_id": conv.home_id,
                "created_at": conv.created_at,
                "last_at": last or conv.created_at,
                "message_count": int(count or 0),
            }
            for conv, last, count in rows
        ]


def list_messages(thread_id: str, limit: int = MESSAGES_LIMIT) -> list[dict[str, Any]]:
    """某个会话的全部消息（按写入顺序）。"""
    with SessionLocal() as db:
        rows = db.scalars(
            select(ChatMessage)
            .where(ChatMessage.thread_id == thread_id)
            .order_by(ChatMessage.id)
            .limit(max(1, min(limit, 500)))
        ).all()
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content or "",
                "sections": m.sections or {},
                "trace": m.trace or [],
                "interrupted": bool(m.interrupted),
                "created_at": m.created_at,
            }
            for m in rows
        ]


def delete_session(thread_id: str) -> bool:
    """删掉一个历史会话（连同它的消息）。返回是否真的删到了东西。"""
    with SessionLocal() as db:
        conv = db.scalar(select(Conversation).where(Conversation.thread_id == thread_id))
        if conv is None:
            return False
        db.execute(delete(ChatMessage).where(ChatMessage.thread_id == thread_id))
        db.delete(conv)
        db.commit()
        return True


def clear_home(home_id: int) -> int:
    """删套房时顺带清理它的历史会话（`homes_admin` 删除流程调用）。"""
    with SessionLocal() as db:
        ids = [
            row
            for row in db.scalars(
                select(Conversation.thread_id).where(Conversation.home_id == home_id)
            ).all()
        ]
        if not ids:
            return 0
        db.execute(delete(ChatMessage).where(ChatMessage.thread_id.in_(ids)))
        db.execute(delete(Conversation).where(Conversation.home_id == home_id))
        db.commit()
        return len(ids)


__all__ = [
    "record_turn",
    "record_message",
    "list_sessions",
    "list_messages",
    "delete_session",
    "clear_home",
]
