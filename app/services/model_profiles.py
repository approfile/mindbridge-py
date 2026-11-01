"""Account-owned credentials and immutable, per-turn model configuration.

Only deployment administrators can configure endpoint URLs. Profile JSON never
leaves the server; all public representations are explicit allowlists.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.models.entities import UserModelProfile
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


class ProfileError(ValueError):
    pass


class ProfileConflict(ProfileError):
    pass


class ConnectionSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connection: str = Field(max_length=64)
    model: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_./:@+\-]+$')
    # Omitted/null keeps the key only for the same connection. Empty clears it.
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(default=0, ge=0)
    mode: Literal['api', 'selfhost']
    chat: ConnectionSelection
    embedding: ConnectionSelection
    embedding_dimensions: int = Field(default=1024, ge=32, le=4096)


@dataclass(frozen=True)
class ModelConnection:
    id: str
    label: str
    kind: str
    operation: str
    base_url: str
    model: str
    revision: str = 'catalog-v1'


def connections(settings):
    items = [
        ModelConnection('deepseek', 'DeepSeek', 'api', 'chat', 'https://api.deepseek.com/v1', 'deepseek-flash'),
        ModelConnection('dashscope-chat', '阿里云 DashScope', 'api', 'chat',
                        'https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen-plus'),
        ModelConnection('dashscope-embedding', '阿里云 DashScope', 'api', 'embedding',
                        'https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3.7-text-embedding'),
    ]
    for operation in ('chat', 'embedding'):
        address = getattr(settings, f'selfhost_{operation}_base_url').rstrip('/')
        if address:
            parsed = urlsplit(address)
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment):
                raise ProfileError('管理员配置的自部署服务地址不合法。')
            items.append(ModelConnection(f'selfhost-{operation}', '自部署服务', 'selfhost', operation,
                         address, getattr(settings, f'selfhost_{operation}_model'), settings.selfhost_model_revision))
    return {item.id: item for item in items}


def catalog(settings):
    return [{'id': c.id, 'label': c.label, 'kind': c.kind, 'operation': c.operation,
             'defaultModel': c.model} for c in connections(settings).values()]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot_metadata(settings):
    return {'id': settings.model_profile_id or 'deployment-default', 'revision': settings.model_profile_revision,
            'mode': settings.model_runtime_mode,
            'chatModel': settings.openai_model if settings.ai_provider == 'openai' else settings.ollama_model,
            'embeddingModel': settings.openai_embedding_model,
            'embeddingBinding': settings.embedding_index_scope or 'deployment-default'}


class ModelProfileService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def _row(self, user_id):
        return self.db.query(UserModelProfile).filter_by(user_id=user_id).populate_existing().first()

    def public(self, user_id):
        row = self._row(user_id)
        if row is None or json.loads(row.config_json)['mode'] == 'deployment_default':
            return {'mode': 'deployment_default', 'revision': row.revision if row else 0, 'chat': None, 'embedding': None,
                    'capabilities': None}
        data = json.loads(row.config_json)
        return {'mode': data['mode'], 'revision': row.revision,
                'chat': self._public_selection(data['chat']), 'embedding': self._public_selection(data['embedding']),
                'embeddingDimensions': data['embedding_dimensions'], 'capabilities': data.get('capabilities'),
                'updatedAt': row.updated_at.isoformat()}

    @staticmethod
    def _public_selection(selection):
        return {'connection': selection['connection'], 'model': selection['model'],
                'hasKey': bool(selection.get('key')), 'keyMask': '已保存（加密）' if selection.get('key') else None}

    def candidate(self, user_id, request: ProfileInput):
        row = self._row(user_id)
        revision = row.revision if row else 0
        if revision != request.expected_revision:
            raise ProfileConflict('模型配置已变化，请刷新后重试。')
        previous = json.loads(row.config_json) if row else {}
        configured = connections(self.settings)
        data = {'mode': request.mode, 'embedding_dimensions': request.embedding_dimensions}
        for operation in ('chat', 'embedding'):
            selection = getattr(request, operation)
            connection = configured.get(selection.connection)
            if not connection or connection.operation != operation:
                raise ProfileError('连接不可用，请选择管理员已配置的服务。')
            if request.mode == 'selfhost' and connection.kind != 'selfhost':
                raise ProfileError('完整自部署模式的生成和 embedding 必须都使用自部署服务。')
            old = previous.get(operation, {})
            key = (selection.api_key.get_secret_value().strip() if selection.api_key is not None else
                   old.get('key', '') if old.get('connection') == connection.id else '')
            if connection.kind == 'selfhost':
                if key:
                    raise ProfileError('自部署服务使用管理员凭据，无需填写个人 Key。')
                key = ''
            elif not key or re.search(r'[\x00-\x20\x7f]', key):
                raise ProfileError('请填写该服务的有效 API Key。')
            data[operation] = {'connection': connection.id, 'model': selection.model, 'key': key,
                               'binding': fingerprint([connection.id, connection.base_url, connection.revision,
                                                       selection.model, fingerprint(key)])}
        # No transaction or DB connection is held during capability/model calls.
        self.db.commit()
        return data

    def settings_for(self, user_id, data, revision):
        settings = self.settings.model_copy(deep=True)
        if data['mode'] == 'deployment_default':
            settings.model_profile_id = f'user-{user_id}'
            settings.model_profile_revision = revision
            from app.services.vector_store import bind_embedding_namespace
            return bind_embedding_namespace(settings)
        configured = connections(settings)
        for operation in ('chat', 'embedding'):
            selected = data[operation]
            connection = configured.get(selected['connection'])
            if not connection or selected['binding'] != fingerprint([
                    connection.id, connection.base_url, connection.revision, selected['model'], fingerprint(selected['key'])]):
                raise ProfileError('服务配置已变更，请重新测试并保存模型配置。')
            key = settings.selfhost_api_key if connection.kind == 'selfhost' else selected['key']
            if operation == 'chat':
                settings.ai_provider = 'openai'
                settings.openai_base_url = connection.base_url
                settings.openai_api_key = key
                settings.openai_model = selected['model']
                settings.openai_thinking_mode = 'auto'
                settings.openai_chat_template_no_thinking = connection.kind == 'selfhost'
            else:
                settings.openai_embedding_base_url = connection.base_url
                settings.openai_embedding_api_key = key
                settings.openai_embedding_model = selected['model']
                settings.embedding_allow_no_key = connection.kind == 'selfhost'
                settings.embedding_query_instruction = ('Given a student question, retrieve relevant campus support passages.'
                                                        if connection.kind == 'selfhost' else '')
        # All roles, including rewrite/summary/reviewer, inherit this snapshot.
        for name in type(settings).model_fields:
            if name.startswith('agent_model_'):
                setattr(settings, name, '')
        settings.knowledge_vector_enabled = settings.knowledge_vector_required = True
        settings.model_profile_id = f'user-{user_id}'
        settings.model_profile_revision = revision
        settings.model_runtime_mode = data['mode']
        settings.embedding_dimensions = data['embedding_dimensions']
        settings.embedding_index_scope = fingerprint([
            'public-campus-kb-v1', user_id, data['embedding']['binding'], settings.embedding_dimensions,
            settings.embedding_query_instruction, settings.knowledge_chunk_size, settings.knowledge_chunk_overlap])
        return settings

    def resolve(self, user_id):
        row = self._row(user_id)
        if row:
            return self.settings_for(user_id, json.loads(row.config_json), row.revision)
        from app.services.vector_store import bind_embedding_namespace
        return bind_embedding_namespace(self.settings)

    def test(self, user_id, request):
        data = self.candidate(user_id, request)
        settings = self.settings_for(user_id, data, request.expected_revision + 1)
        return data, settings, probe_capabilities(settings)

    def save(self, user_id, request):
        data, settings, capabilities = self.test(user_id, request)
        # Build and validate a separate, corpus-bound index before activation.
        from app.services.knowledge import KnowledgeService
        try:
            service = KnowledgeService(self.db, settings)
            indexed = service.rebuild_vector_index()
            if service.vector_store.count() != indexed:
                raise ProfileError('知识库索引未完整建立，原配置仍有效。')
            if KnowledgeService(self.db, settings).settings.embedding_corpus_version != service.settings.embedding_corpus_version:
                raise ProfileError('知识库正在更新，请重新保存配置；原配置仍有效。')
        except ProfileError:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            raise ProfileError('知识库索引建立失败，原配置仍有效。') from None
        data['capabilities'] = {**capabilities, 'indexedChunks': indexed}
        payload = json.dumps(data, ensure_ascii=False)
        try:
            if request.expected_revision == 0:
                self.db.add(UserModelProfile(user_id=user_id, revision=1, config_json=payload))
            else:
                table = UserModelProfile.__table__
                result = self.db.execute(update(table).where(
                    table.c.user_id == user_id, table.c.revision == request.expected_revision
                ).values(revision=request.expected_revision + 1, config_json=payload, updated_at=datetime.utcnow()))
                if result.rowcount != 1:
                    raise ProfileConflict('模型配置已变化，请刷新后重试。')
            self.db.commit()
        except (IntegrityError, ProfileConflict):
            self.db.rollback()
            raise ProfileConflict('模型配置已变化，请刷新后重试。') from None
        return self.public(user_id)

    def reset(self, user_id, expected_revision):
        # Keep a key-free tombstone so revision numbers cannot be reused (ABA).
        # In-flight saves using an old revision cannot resurrect revoked keys.
        table = UserModelProfile.__table__
        result = self.db.execute(update(table).where(table.c.user_id == user_id, table.c.revision == expected_revision)
                                 .values(revision=expected_revision + 1, config_json='{"mode":"deployment_default"}',
                                         updated_at=datetime.utcnow()))
        if result.rowcount != 1:
            self.db.rollback()
            raise ProfileConflict('模型配置已变化，请刷新后重试。')
        self.db.commit()
        return self.public(user_id)


def probe_capabilities(settings):
    from app.services.turn_budget import TurnControl, TurnStopped, turn_scope
    try:
        with turn_scope(TurnControl(60, 15)):
            return _probe_capabilities(settings)
    except TurnStopped:
        raise ProfileError('连接测试超时；原配置未改变。') from None


def _probe_capabilities(settings):
    """Functional probes use synthetic input only; upstream bodies never escape."""
    messages = [AiMessage(role='system', content='Return a JSON object only: {"ok":true}. Do not think aloud.'),
                AiMessage(role='user', content='Connectivity test. Reply with the JSON object.')]
    try:
        ai = AiClient(settings.model_copy(update={'ai_max_tokens': 128}))
        ordinary = ai.complete(messages)
        structured = ai.complete(messages, json_mode=True)
        if json.loads(structured) != {'ok': True}:
            raise ValueError('Invalid JSON capability')

        async def asynchronous():
            reviewed = await ai.complete_async(messages)
            chunks = [chunk async for chunk in ai.stream(messages)]
            return reviewed, ''.join(chunks)

        reviewed, streamed = asyncio.run(asynchronous())
        if not all(isinstance(value, str) and value.strip() and '<think' not in value.lower()
                   and '</think' not in value.lower() for value in (ordinary, structured, reviewed, streamed)):
            raise ValueError('Incompatible content')
        if json.loads(reviewed) != {'ok': True}:
            raise ValueError('Invalid reviewer JSON')
        from app.services.vector_store import ChromaKnowledgeStore
        store = ChromaKnowledgeStore(settings)
        vectors = store.embed_texts(['Synthetic campus support connectivity test.'])
        if len(vectors[0]) != settings.embedding_dimensions:
            raise ValueError('Embedding dimensions mismatch')
        maximum = None
        if settings.openai_chat_template_no_thinking:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.get(f'{settings.openai_base_url}/models',
                                      headers={'Authorization': f'Bearer {settings.openai_api_key}'})
                response.raise_for_status()
                models = response.json().get('data', [])
                model = next(item for item in models if item.get('id') == settings.openai_model)
                maximum = model.get('max_model_len')
                if not isinstance(maximum, int) or maximum < 8192:
                    raise ValueError('Selfhost requires a declared context >=8192')
        return {'nonStreaming': True, 'streaming': True, 'json': True, 'reviewJson': True,
                'thinkingCompatible': True, 'embeddingDimensions': len(vectors[0]),
                'declaredContextTokens': maximum, 'contextStatus': 'server_declared' if maximum else 'unknown',
                'testedAt': datetime.now(timezone.utc).isoformat(),
                'notice': '连接协议测试不代表业务效果评测或完整上下文压力测试。'}
    except Exception:
        raise ProfileError('连接能力测试失败。请检查模型名、独立的 embedding Key、维度和服务状态；原配置未改变。') from None
