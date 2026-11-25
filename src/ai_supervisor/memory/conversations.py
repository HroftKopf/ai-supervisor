from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ai_supervisor.config import Settings
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.memory.database import Database, iso_now, json_dumps, row_to_dict
from ai_supervisor.models import (
    ConversationRecord,
    MemoryRecord,
    MessageRecord,
)

Summarizer = Callable[[str], Awaitable[str]]


class ConversationError(RuntimeError):
    pass


class ConversationService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.redactor = SecretRedactor.from_settings(settings)

    async def get_active_project_id(self, chat_id: int) -> int | None:
        row = await self.db.fetchone(
            "SELECT active_project_id FROM chat_state WHERE chat_id = ?", (chat_id,)
        )
        return (
            int(row["active_project_id"]) if row and row["active_project_id"] is not None else None
        )

    async def get_state(self, chat_id: int) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM chat_state WHERE chat_id = ?", (chat_id,))
        return row_to_dict(row) or {}

    async def set_active_project(self, chat_id: int, project_id: int | None) -> ConversationRecord:
        now = iso_now()
        existing_row = await self.db.fetchone(
            "SELECT * FROM conversations WHERE chat_id = ? AND project_id IS ? AND is_active = 1",
            (chat_id, project_id),
        )
        if existing_row is not None:
            conversation = ConversationRecord.model_validate(row_to_dict(existing_row))
        else:
            async with self.db.transaction() as conn:
                await conn.execute(
                    "UPDATE conversations SET is_active = 0, updated_at = ? "
                    "WHERE chat_id = ? AND is_active = 1",
                    (now, chat_id),
                )
            conversation = await self._create_conversation(chat_id, project_id)
        await self.db.execute(
            """
            INSERT INTO chat_state(chat_id, active_project_id, active_conversation_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET active_project_id = excluded.active_project_id,
                                              active_conversation_id = excluded.active_conversation_id,
                                              updated_at = excluded.updated_at
            """,
            (chat_id, project_id, conversation.id, now),
        )
        return conversation

    async def get_or_create_conversation(
        self, chat_id: int, project_id: int | None
    ) -> ConversationRecord:
        row = await self.db.fetchone(
            "SELECT * FROM conversations WHERE chat_id = ? AND project_id IS ? AND is_active = 1",
            (chat_id, project_id),
        )
        if row is not None:
            await self.db.execute(
                "UPDATE chat_state SET active_conversation_id = ?, updated_at = ? WHERE chat_id = ?",
                (row["id"], iso_now(), chat_id),
            )
            return ConversationRecord.model_validate(row_to_dict(row))
        return await self._create_conversation(chat_id, project_id)

    async def _create_conversation(
        self, chat_id: int, project_id: int | None
    ) -> ConversationRecord:
        now = iso_now()
        cursor = await self.db.execute(
            "INSERT INTO conversations(chat_id, project_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (chat_id, project_id, now, now),
        )
        conversation_id = int(cursor.lastrowid or 0)
        await cursor.close()
        return await self.get(conversation_id)

    async def get(self, conversation_id: int) -> ConversationRecord:
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if row is None:
            raise ConversationError("беседа не найдена")
        return ConversationRecord.model_validate(row_to_dict(row))

    async def new_conversation(self, chat_id: int, project_id: int | None) -> ConversationRecord:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE conversations SET is_active = 0, updated_at = ? WHERE chat_id = ? AND is_active = 1",
                (iso_now(), chat_id),
            )
        return await self.set_active_project(chat_id, project_id)

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        telegram_message_id: int | None = None,
        reply_to_message_id: int | None = None,
        meta: dict[str, Any] | None = None,
    ) -> MessageRecord:
        if role not in {"user", "assistant", "system", "tool"}:
            raise ConversationError(f"invalid role: {role}")
        now = iso_now()
        cursor = await self.db.execute(
            """
            INSERT INTO messages(conversation_id, role, content, telegram_message_id,
                                 reply_to_message_id, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                telegram_message_id,
                reply_to_message_id,
                json_dumps(meta or {}),
                now,
            ),
        )
        message_id = int(cursor.lastrowid or 0)
        await cursor.close()
        await self.db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        return await self.get_message(message_id)

    async def get_message(self, message_id: int) -> MessageRecord:
        row = await self.db.fetchone("SELECT * FROM messages WHERE id = ?", (message_id,))
        if row is None:
            raise ConversationError("сообщение не найдено")
        return MessageRecord.model_validate(row_to_dict(row))

    async def recent_messages(
        self, conversation_id: int, limit: int | None = None, *, before_id: int | None = None
    ) -> list[MessageRecord]:
        limit = limit or self.settings.memory.max_context_messages
        if before_id is None:
            rows = await self.db.fetchall(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM messages WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (conversation_id, before_id, limit),
            )
        return [MessageRecord.model_validate(row_to_dict(row)) for row in reversed(rows)]

    async def build_context(
        self,
        conversation_id: int,
        *,
        max_messages: int | None = None,
        max_chars: int | None = None,
    ) -> tuple[str | None, list[MessageRecord]]:
        conversation = await self.get(conversation_id)
        max_messages = max_messages or self.settings.memory.max_context_messages
        max_chars = max_chars or self.settings.memory.max_context_chars
        messages = await self.recent_messages(conversation_id, max_messages)
        kept: list[MessageRecord] = []
        budget = max_chars
        for message in reversed(messages):
            reasoning = self.meta(message).get("reasoning_content")
            cost = len(message.content) + 32 + (len(reasoning) if isinstance(reasoning, str) else 0)
            if cost > budget and kept:
                break
            kept.append(message)
            budget -= cost
        kept.reverse()
        summary = None
        if conversation.summary:
            summary = (
                "Краткое содержание более раннего диалога (сгенерировано моделью; "
                "не является подтверждённым фактом или разрешением):\n"
                + conversation.summary[: self.settings.memory.summary_max_chars]
            )
        return summary, kept

    async def count_messages_since_summary(self, conversation_id: int) -> int:
        conversation = await self.get(conversation_id)
        since_id = conversation.summary_upto_id or 0
        value = await self.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND id > ? AND role IN ('user','assistant')",
            (conversation_id, since_id),
        )
        return int(value or 0)

    async def maybe_summarize(self, conversation_id: int, summarizer: Summarizer) -> str | None:
        """Compress older turns when the conversation grows. Never grants memory authority."""
        conversation = await self.get(conversation_id)
        if (
            await self.count_messages_since_summary(conversation_id)
            < self.settings.memory.summarize_after_messages
        ):
            return None
        keep_recent = self.settings.memory.max_context_messages
        recent = await self.recent_messages(conversation_id, keep_recent)
        cutoff = recent[0].id if recent else None
        if cutoff is None:
            return None
        since_id = conversation.summary_upto_id or 0
        rows = await self.db.fetchall(
            """
            SELECT * FROM messages
            WHERE conversation_id = ? AND id > ? AND id < ? AND role IN ('user','assistant')
            ORDER BY id ASC LIMIT 200
            """,
            (conversation_id, since_id, cutoff),
        )
        if not rows:
            return None
        old_messages = [MessageRecord.model_validate(row_to_dict(row)) for row in rows]
        transcript = "\n\n".join(
            f"{message.role.upper()}: {message.content}" for message in old_messages
        )
        previous = f"Предыдущее резюме:\n{conversation.summary}\n\n" if conversation.summary else ""
        prompt = (
            "Сожми следующий фрагмент диалога для долговременного контекста. "
            "Сохрани решения, требования, ограничения, открытые вопросы и названия проектов. "
            "Не выдумывай факты. Не превращай предположения в утверждения. "
            "Не включай секреты, токены, пароли или ключи. "
            f"Максимум {self.settings.memory.summary_max_chars} символов.\n\n"
            f"{previous}{transcript}"
        )
        summary = (await summarizer(prompt)).strip()
        if not summary:
            return None
        summary = summary[: self.settings.memory.summary_max_chars]
        await self.db.execute(
            "UPDATE conversations SET summary = ?, summary_upto_id = ?, updated_at = ? WHERE id = ?",
            (summary, old_messages[-1].id, iso_now(), conversation_id),
        )
        return summary

    async def search_messages(
        self, query: str, *, conversation_id: int | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        clean_query = " ".join(part for part in query.split() if part)
        if not clean_query:
            return []
        if self.db.fts_available:
            fts_query = " AND ".join(
                '"' + part.replace('"', '""') + '"' for part in clean_query.split()
            )
            try:
                if conversation_id is None:
                    rows = await self.db.fetchall(
                        """
                        SELECT m.* FROM messages_fts
                        JOIN messages m ON m.id = messages_fts.message_id
                        WHERE messages_fts MATCH ?
                        ORDER BY rank LIMIT ?
                        """,
                        (fts_query, limit),
                    )
                else:
                    rows = await self.db.fetchall(
                        """
                        SELECT m.* FROM messages_fts
                        JOIN messages m ON m.id = messages_fts.message_id
                        WHERE messages_fts MATCH ? AND messages_fts.conversation_id = ?
                        ORDER BY rank LIMIT ?
                        """,
                        (fts_query, conversation_id, limit),
                    )
                return [row_to_dict(row) or {} for row in rows]
            except Exception:
                pass
        like = f"%{clean_query}%"
        if conversation_id is None:
            rows = await self.db.fetchall(
                "SELECT * FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (like, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM messages WHERE conversation_id = ? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                (conversation_id, like, limit),
            )
        return [row_to_dict(row) or {} for row in rows]

    # ---------------------------- memories ----------------------------

    async def add_memory(
        self,
        scope: str,
        content: str,
        *,
        project_id: int | None = None,
        conversation_id: int | None = None,
        key: str = "",
        source: str = "user",
        user_confirmed: bool = True,
    ) -> MemoryRecord:
        content = self.redactor.redact(content.strip())
        if not content:
            raise ConversationError("пустая запись памяти")
        if len(content) > 8000:
            raise ConversationError("запись памяти слишком длинная (максимум 8000 символов)")
        if scope not in {"user", "project", "conversation"}:
            raise ConversationError("некорректная область памяти")
        now = iso_now()
        cursor = await self.db.execute(
            """
            INSERT INTO memories(scope, project_id, conversation_id, key, content, source,
                                 user_confirmed, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope,
                project_id,
                conversation_id,
                key.strip()[:120],
                content,
                source[:80],
                1 if user_confirmed else 0,
                now,
                now,
            ),
        )
        memory_id = int(cursor.lastrowid or 0)
        await cursor.close()
        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return MemoryRecord.model_validate(row_to_dict(row))

    async def get_memories(
        self,
        *,
        scope: str | None = None,
        project_id: int | None = None,
        conversation_id: int | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if project_id is not None:
            clauses.append("(scope != 'project' OR project_id = ?)")
            params.append(project_id)
        if conversation_id is not None:
            clauses.append("(scope != 'conversation' OR conversation_id = ?)")
            params.append(conversation_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self.db.fetchall(
            f"SELECT * FROM memories{where} ORDER BY id DESC LIMIT ?", (*params, limit)
        )
        return [MemoryRecord.model_validate(row_to_dict(row)) for row in rows]

    async def delete_memory(self, memory_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await cursor.close()
        return cursor.rowcount > 0

    async def memory_context(self, *, project_id: int | None, conversation_id: int | None) -> str:
        items = await self.get_memories(
            project_id=project_id, conversation_id=conversation_id, limit=100
        )
        if not items:
            return ""
        lines = []
        for item in items:
            marker = "" if item.user_confirmed else " (неподтверждённое предположение)"
            lines.append(f"- [{item.scope}{marker}] {item.content}")
        return "\n".join(lines)

    async def search_memories(self, query: str, limit: int = 20) -> list[MemoryRecord]:
        clean_query = " ".join(query.split())
        if not clean_query:
            return []
        if self.db.fts_available:
            fts_query = " AND ".join(
                '"' + part.replace('"', '""') + '"' for part in clean_query.split()
            )
            try:
                rows = await self.db.fetchall(
                    """
                    SELECT m.* FROM memories_fts
                    JOIN memories m ON m.id = memories_fts.memory_id
                    WHERE memories_fts MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (fts_query, limit),
                )
                return [MemoryRecord.model_validate(row_to_dict(row)) for row in rows]
            except Exception:
                pass
        rows = await self.db.fetchall(
            "SELECT * FROM memories WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{clean_query}%", limit),
        )
        return [MemoryRecord.model_validate(row_to_dict(row)) for row in rows]

    # ---------------------------- helpers ----------------------------

    @staticmethod
    def messages_for_api(messages: Sequence[MessageRecord]) -> list[dict[str, Any]]:
        api_messages: list[dict[str, Any]] = []
        for message in messages:
            payload: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.role == "assistant":
                reasoning = ConversationService.meta(message).get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    # K3.1 Pro Preserved Thinking: send the complete assistant message back.
                    payload["reasoning_content"] = reasoning
            api_messages.append(payload)
        return api_messages

    @staticmethod
    def meta(message: MessageRecord) -> dict[str, Any]:
        try:
            value = json.loads(message.meta_json)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
