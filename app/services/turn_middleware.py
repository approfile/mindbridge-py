"""ASGI deadline watchdog: includes parsing/auth, planning and streamed sends."""
import asyncio
import json
import uuid

from app.services.observability import bind_ids, current_observation, log_event
from app.services.turn_budget import TurnControl, TurnStopped, turn_scope
from app.services.turn_execution import TurnRejected


class _BodyTooLarge(BaseException):
    pass


class TurnBudgetMiddleware:
    def __init__(self, app, settings, runtime):
        self.app = app; self.settings = settings; self.runtime = runtime

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/api/chat/stream' or scope.get('method') != 'POST':
            return await self.app(scope, receive, send)
        observation = current_observation()
        if observation and not observation.ids['run_id']:
            bind_ids(run_id=uuid.uuid4().hex)
        elapsed = observation.elapsed_ms() / 1000 if observation else 0.
        control = TurnControl(self.settings.turn_timeout_seconds, self.settings.turn_io_timeout_seconds,
                              observation, elapsed)
        with turn_scope(control):
            try:
                lease = self.runtime.admit(control)
            except TurnRejected as exc:
                if observation: observation.stream_status = 'rejected_capacity'
                log_event('turn.rejected', reason_code=exc.code)
                return await self._json(send, 429, exc.code, '当前请求较多，请稍后重试。')
            started = False; terminal = False; meta_sent = False; token_sent = False; received = 0
            disconnected = asyncio.Event()

            async def guarded_receive():
                nonlocal received
                if terminal:
                    return {'type': 'http.disconnect'}
                message = await receive()
                if message['type'] == 'http.request':
                    received += len(message.get('body', b''))
                    if received > self.settings.turn_max_request_bytes:
                        control.stop('REQUEST_TOO_LARGE')
                        raise _BodyTooLarge()
                if message['type'] == 'http.disconnect' and not control.terminal_sent:
                    control.stop('CLIENT_DISCONNECTED'); disconnected.set()
                return message

            async def guarded_send(message):
                nonlocal started, meta_sent, token_sent
                if terminal or control.reason:
                    return
                if message['type'] == 'http.response.start': started = True
                body = message.get('body', b'')
                if body.startswith(b'event: meta\n'): meta_sent = True
                if body.startswith(b'event: token\n'): token_sent = True
                await send(message)
                if body.startswith((b'event: done\n', b'event: error\n')):
                    control.terminal_sent = True

            task = asyncio.create_task(self.app(scope, guarded_receive, guarded_send), name='mindbridge-http-turn')
            lease.http_task = task
            task.add_done_callback(lease.http_finished)
            disconnect_wait = asyncio.create_task(disconnected.wait())
            try:
                done, _ = await asyncio.wait({task, disconnect_wait}, timeout=control.remaining(),
                                             return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    try:
                        result = task.result()
                        if not control.reason or control.reason == 'CLIENT_DISCONNECTED':
                            return result
                    except BaseException as exc:
                        if control.reason == 'CLIENT_DISCONNECTED':
                            return
                        if not (self._contains(exc, _BodyTooLarge) or self._contains(exc, TurnStopped)):
                            raise
                    if control.reason == 'REQUEST_TOO_LARGE':
                        terminal = True
                        return await self._json(send, 413, 'REQUEST_TOO_LARGE', '消息过长，请缩短后重试。')
                elif disconnect_wait in done:
                    terminal = True; task.cancel(); return
                if control.terminal_sent and not control.reason:
                    # The client already received done/error within budget.
                    # Slow telemetry cleanup retains capacity but must not
                    # retrospectively turn a completed response into timeout.
                    terminal = True; task.cancel()
                    try:
                        async with asyncio.timeout(.1):
                            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                    except (TimeoutError, OSError):
                        pass
                    return
                if not control.reason:
                    control.stop('TURN_TIMEOUT')
                terminal = True; task.cancel()
                log_event('turn.deadline_exceeded', reason_code=control.reason,
                          budget_seconds=self.settings.turn_timeout_seconds)
                # A slow/closed receiver must not make error delivery unbounded.
                try:
                    async with asyncio.timeout(.1):
                        if not started:
                            await self._json(send, 504, 'TURN_TIMEOUT', '本轮处理超时，请稍后重试。')
                        else:
                            if control.terminal_sent:
                                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                                return
                            ids = observation.ids if observation else {}
                            blocks = []
                            if not meta_sent and ids.get('session_id'):
                                blocks.append(self._sse('meta', {'type': 'meta', 'sessionId': ids['session_id'], 'requestId': ids.get('request_id')}))
                            if not token_sent and control.risk in {'HIGH', 'MEDIUM'}:
                                from app.services.response_review import safe_template
                                blocks.append(self._sse('token', {'type': 'token', 'sessionId': ids.get('session_id'), 'content': safe_template(control.risk)}))
                            blocks.append(self._sse('error', {'type': 'error', 'code': 'TURN_TIMEOUT', 'message': '本轮处理超时，已停止继续生成，请稍后重试。'}))
                            await send({'type': 'http.response.body', 'body': b''.join(blocks), 'more_body': False})
                except (TimeoutError, OSError):
                    pass
            except BaseException:
                if not control.reason: control.stop('CLIENT_DISCONNECTED')
                terminal = True; task.cancel(); raise
            finally:
                disconnect_wait.cancel()

    @staticmethod
    def _contains(error, kind):
        return isinstance(error, kind) or any(TurnBudgetMiddleware._contains(e, kind)
                                              for e in getattr(error, 'exceptions', ()))

    @staticmethod
    def _sse(event, data):
        return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode()

    @staticmethod
    async def _json(send, status, code, message):
        body = json.dumps({'detail': message, 'code': code}, ensure_ascii=False).encode()
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'application/json'), (b'retry-after', b'1')]})
        await send({'type': 'http.response.body', 'body': body, 'more_body': False})
