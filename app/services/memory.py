
from __future__ import annotations

import json
import logging
from datetime import datetime
from importlib import import_module
from typing import Protocol

from app.core.config import Settings
from app.models.entities import ChatMessage
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer
from app.core.privacy_crypto import encrypt_text, decrypt_text, PREFIX


logger = logging.getLogger(__name__)


class MemoryCompactionSettings(Protocol):
    memory_compaction_enabled: bool
    memory_compaction_recent_messages: int
    memory_summary_max_chars: int


class RedisShortTermMemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.client = self._connect()

    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        if self.client is None:
            return []
        try:
            return self._read(session_public_id, self.settings.redis_memory_max_messages)
        except Exception as exc:
            logger.warning("Redis memory read unavailable: %s", exc)
            return []

    def messages_from_rows(self, rows: list[ChatMessage]) -> list[AiMessage]:
        return [self._message_from_row(row) for row in rows]

    def append(self, session_public_id: str, role: str, content: str, message_id: int | None = None) -> None:
        if self.client is None:
            return
        key = self._key(session_public_id)
        payload = self._serialize(role, content)
        try:
            pipe = self.client.pipeline()
            pipe.rpush(key, payload)
            pipe.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            pipe.expire(key, self.settings.redis_memory_ttl_seconds)
            if message_id is not None:
                pipe.set(key + ":version", message_id, ex=self.settings.redis_memory_ttl_seconds)
            pipe.execute()
        except Exception as exc:
            logger.warning("Redis memory append unavailable: %s", exc)

    def replace(self, session_public_id: str, messages: list[AiMessage], last_message_id: int | None = None) -> None:
        if self.client is None:
            return
        key = self._key(session_public_id)
        pipe = self.client.pipeline()
        pipe.delete(key)
        pipe.delete(key + ":version")
        if messages:
            pipe.rpush(key, *[self._serialize(message.role, message.content) for message in messages])
            pipe.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            pipe.expire(key, self.settings.redis_memory_ttl_seconds)
            if last_message_id is not None:
                pipe.set(key + ":version", last_message_id, ex=self.settings.redis_memory_ttl_seconds)
        try:
            pipe.execute()
        except Exception as exc:
            logger.warning("Redis memory replace unavailable: %s", exc)

    def _read(self, session_public_id: str, limit: int) -> list[AiMessage]:
        raw_items = self.client.lrange(self._key(session_public_id), -limit, -1)
        return self._decode(raw_items)

    def load_snapshot(self, session_public_id: str) -> tuple[list[AiMessage], int | None]:
        if self.client is None:
            return [], None
        try:
            key = self._key(session_public_id)
            pipe = self.client.pipeline()
            pipe.lrange(key, -self.settings.redis_memory_max_messages, -1)
            pipe.get(key + ":version")
            messages, version = pipe.execute()
            return self._decode(messages), int(version) if version is not None else None
        except Exception as exc:
            logger.warning("Redis memory snapshot unavailable: %s", exc)
            return [], None

    def _decode(self, raw_items) -> list[AiMessage]:
        messages = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = str(data.get("role", "")).lower()
            content = str(data.get("content", ""))
            if not content.startswith(PREFIX):
                # Legacy cache is not an authoritative plaintext fallback.
                # DB-backed session memory repairs it after authentication.
                continue
            content = decrypt_text(content, getattr(self,'settings',None))
            if role and content:
                messages.append(AiMessage(role=role, content=self.privacy.sanitize(content)))
        return messages

    def _connect(self):
        try:
            redis_module = import_module("redis")
        except ModuleNotFoundError:
            logger.warning("Redis memory disabled: redis package not installed")
            return None
        try:
            client = redis_module.Redis.from_url(
                self.settings.redis_url,
                decode_responses=True,
                socket_timeout=self.settings.redis_socket_timeout_seconds,
                socket_connect_timeout=self.settings.redis_socket_timeout_seconds,
            )
        except Exception as exc:
            logger.warning("Redis memory disabled: %s", exc)
            return None
        try:
            client.ping()
        except Exception as exc:
            logger.warning("Redis memory disabled: %s", exc)
            return None
        return client

    def _message_from_row(self, row: ChatMessage) -> AiMessage:
        return AiMessage(role=row.role.lower(), content=self.privacy.sanitize(row.content))

    def _serialize(self, role: str, content: str) -> str:
        return json.dumps(
            {
                "role": role.lower(),
                "content": encrypt_text(self.privacy.sanitize(content),getattr(self,'settings',None)),
                "createdAt": datetime.utcnow().isoformat(),
            },
            ensure_ascii=False,
        )

    def _key(self, session_public_id: str) -> str:
        return f"mindbridge:short-term-memory:{session_public_id}"

    def upgrade_legacy_cache(self):
        """CAS replace legacy list entries; preserve existing TTL/version.

        Called once during quiescent deployment, not every request. WATCH
        prevents overwriting another process's concurrent list append.
        """
        if self.client is None:return {'available':False,'lists':0}
        count=0
        for key in self.client.scan_iter(match='mindbridge:short-term-memory:*',count=100):
            if self.client.type(key)!='list':continue
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key);values=pipe.lrange(key,0,-1);ttl=pipe.pttl(key);updated=[];changed=False
                    for value in values:
                        data=json.loads(value);content=str(data.get('content',''))
                        if not content.startswith(PREFIX):
                            data['content']=encrypt_text(self.privacy.sanitize(content),self.settings);changed=True
                        else:decrypt_text(content,self.settings)
                        updated.append(json.dumps(data,ensure_ascii=False))
                    if not changed:pipe.unwatch();continue
                    if ttl==0 or ttl==-2:pipe.unwatch();continue
                    pipe.multi();pipe.delete(key)
                    if updated:
                        pipe.rpush(key,*updated);pipe.pexpire(key,ttl if ttl>0 else self.settings.redis_memory_ttl_seconds*1000)
                    pipe.execute();count+=1
                except Exception as exc:
                    # Fail deployment rather than claim a plaintext cache was
                    # secured. Retry safely after a concurrent modification.
                    raise RuntimeError('Private cache migration incomplete') from None
        return {'available':True,'lists':count}

    def erase_session_cache(self, public_id):
        if self.client is None:
            raise RuntimeError('Redis unavailable: content erasure aborted to prevent stale-cache resurrection')
        base=self._key(public_id)
        keys=[base,base+':version']
        for key in self.client.scan_iter(match=self._key('agent:*:'+public_id)+'*',count=100):
            keys.append(key)
        self.client.delete(*keys)


def compact_history_for_prompt(
    history: list[AiMessage],
    settings: MemoryCompactionSettings,
    current_input: str = "",
) -> tuple[list[AiMessage], str]:
    """Return bounded prompt history plus a student-safe memory brief.

    The summary is deterministic and avoids diagnostic labels. It is intended
    for prompt context and auditability, not for student-facing display.
    """

    sanitized = [AiMessage(role=item.role, content=PrivacySanitizer().sanitize(item.content)) for item in history]
    if not sanitized:
        return [], "无相关历史记忆。"

    recent_count = max(2, int(getattr(settings, "memory_compaction_recent_messages", 8)))
    max_chars = max(120, int(getattr(settings, "memory_summary_max_chars", 500)))
    brief = summarize_history_for_memory(sanitized, current_input, max_chars)

    if not getattr(settings, "memory_compaction_enabled", True) or len(sanitized) <= recent_count:
        return sanitized, brief

    recent = sanitized[-recent_count:]
    summary_message = AiMessage(
        role="system",
        content=(
            "历史摘要（仅供 MindBridge 内部上下文使用；不要向学生展示；"
            "不要据此输出诊断、风险等级或后台标签）：\n" + brief
        ),
    )
    return [summary_message, *recent], brief


def summarize_history_for_memory(history: list[AiMessage], current_input: str = "", max_chars: int = 500) -> str:
    privacy = PrivacySanitizer()
    user_points = []
    assistant_points = []
    for message in history:
        content = " ".join(privacy.sanitize(message.content).split())
        if not content:
            continue
        if message.role == "user":
            user_points.append(content)
        elif message.role == "assistant":
            assistant_points.append(content)

    parts = []
    if user_points:
        parts.append("学生近期关注：" + "；".join(_clip(item, 80) for item in user_points[-4:]))
    if assistant_points:
        parts.append("已给过的支持：" + "；".join(_clip(item, 70) for item in assistant_points[-3:]))
    if current_input:
        parts.append("本轮输入关注：" + _clip(privacy.sanitize(current_input), 80))
    if not parts:
        return "无相关历史记忆。"
    return _clip("\n".join(parts), max_chars)


def _clip(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."
