from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent
from app.services.ai import split_text
from app.services.agent_models import AgentModelRegistry
from app.services.response_review import ReplyReviewPipeline, safe_template
from app.services.observability import observation_scope, span, log_event
from app.services.turn_budget import TurnControl, TurnStopped, checkpoint, current_turn, turn_scope, within_turn
from app.services.turn_execution import get_turn_runtime, await_owned, TurnRejected
from app.services.turn_work import OwnedTurnWork

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, db: Session, settings: Settings, runtime_factory=None):
        bound = db.get_bind()
        self.engine = bound if isinstance(bound, Engine) else bound.engine
        self.settings = settings.model_copy(deep=True)
        self.runtime_factory = runtime_factory
        self.ai = AgentModelRegistry(self.settings).client_for('ResponseAgent')

    async def stream_chat(self, user: UserAccount | int, request: ChatRequest):
        # Inspection of identity never lazy-loads an expired caller ORM object.
        user_id = user if isinstance(user, int) else inspect(user).identity[0]
        with observation_scope(ensure_run=True) as observation:
            inherited = current_turn()
            control = inherited or TurnControl(self.settings.turn_timeout_seconds,
                                                self.settings.turn_io_timeout_seconds, observation)
            with turn_scope(control):
                direct = control.lease is None
                lease = control.lease
                work = None
                try:
                    if lease is None:
                        lease = get_turn_runtime(self.settings).admit(control)
                    lease.bind_user(user_id, request.sessionId)
                    lease.finalization_required = True
                    work = OwnedTurnWork(self.engine, self.settings, user_id, request.model_copy(deep=True),
                                         control, observation, self.runtime_factory)
                    with span('turn.total'):
                        async with aclosing(self._stream_chat(work, lease, observation)) as stream:
                            async for block in stream:
                                yield block
                except TurnRejected as exc:
                    observation.stream_status = 'rejected_capacity'
                    log_event('turn.rejected', reason_code=exc.code)
                    yield sse('error', {'type': 'error', 'code': exc.code, 'message': '本轮正在处理或请求较多，请稍后重试。'})
                except TurnStopped:
                    observation.stream_status = control.status()
                    yield sse('error', {'type': 'error', 'code': control.reason, 'message': '本轮处理已停止，请稍后重试。'})
                except BaseException as exc:
                    if not control.reason:
                        control.stop('CLIENT_DISCONNECTED' if type(exc).__name__ in {'CancelledError', 'GeneratorExit'} else 'TURN_FAILED')
                    observation.stream_status = control.status()
                    raise
                finally:
                    if lease is not None:
                        if work is not None:
                            completion = lease.finish(work.finalize)
                            if not control.reason:
                                try:
                                    async with asyncio.timeout(self.settings.turn_cleanup_wait_seconds):
                                        await await_owned(completion)
                                except TimeoutError:
                                    log_event('turn.finalization_pending', status='background')
                                except Exception:
                                    logger.exception('Unable to persist final turn telemetry')
                            elif control.reason == 'CLIENT_DISCONNECTED' and self.settings.turn_cleanup_wait_seconds:
                                try:
                                    async with asyncio.timeout(self.settings.turn_cleanup_wait_seconds):
                                        await await_owned(completion)
                                except (TimeoutError, Exception):
                                    pass
                        if direct:
                            lease.http_finished()

    async def _stream_chat(self, work, lease, observation):
        observation.stream_status = 'planning'
        try:
            outcome = await within_turn(await_owned(lease.submit(work.plan)))
            checkpoint()
        except Exception:
            observation.stream_status = 'planning_failed'
            logger.exception('Unable to persist chat turn and tool outbox')
            yield sse('error', ChatStreamEvent(type='error', message='暂时无法处理这条消息，请稍后重试。').model_dump())
            return
        yield sse('meta', ChatStreamEvent(type='meta', sessionId=outcome.session.public_id,
                  requestId=observation.ids['request_id']).model_dump())
        try:
            checkpoint()
            with span('reply.total'):
                reply = await within_turn(ReplyReviewPipeline(self.settings, self.ai,
                    AgentModelRegistry(self.settings).client_for('SafetyAgent')).run(outcome))
            checkpoint()
            with span('persistence.reviewed_reply'):
                await within_turn(await_owned(lease.submit(lambda: work.persist_reply(reply))))
            checkpoint()
        except Exception:
            observation.stream_status = 'reply_persistence_failed'
            logger.exception('Unable to persist reviewed reply')
            yield sse('token', ChatStreamEvent(type='token', sessionId=outcome.session.public_id,
                                               content=safe_template(outcome.risk_level)).model_dump())
            yield sse('error', ChatStreamEvent(type='error', sessionId=outcome.session.public_id,
                                               message='本轮回复暂时未能完成，请稍后重试。').model_dump())
            return
        observation.stream_status = 'accepted' if reply.accepted else 'safe_fallback'
        with span('sse.emit'):
            for token in split_text(reply.text, 24):
                checkpoint()
                yield sse('token', ChatStreamEvent(type='token', sessionId=outcome.session.public_id, content=token).model_dump())
                await asyncio.sleep(0)
        checkpoint()
        if not reply.accepted:
            yield sse('error', ChatStreamEvent(type='error', sessionId=outcome.session.public_id,
                                               message='本轮回复暂时未能完成，已提供通用支持信息。').model_dump())
            return
        yield sse('done', ChatStreamEvent(type='done', sessionId=outcome.session.public_id).model_dump())


def sse(event: str, data: dict) -> str:
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n'
