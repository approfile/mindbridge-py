"""Bound credential request bodies and expensive capability/index operations."""
import threading
from fastapi.responses import JSONResponse


class ModelProfileMiddleware:
    def __init__(self, app):
        self.app = app
        self.slots = threading.BoundedSemaphore(2)

    async def __call__(self, scope, receive, send):
        if (scope['type'] != 'http' or not scope.get('path', '').startswith('/api/model-profile')
                or scope.get('method') not in {'POST', 'PUT'}):
            return await self.app(scope, receive, send)
        if not self.slots.acquire(blocking=False):
            return await JSONResponse({'detail': '模型测试正在进行，请稍后重试。'}, status_code=429,
                                      headers={'Retry-After': '5'})(scope, receive, send)
        try:
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > 16384:
                    return await JSONResponse({'detail': '模型配置请求过大。'}, status_code=413)(scope, receive, send)
                if not message.get('more_body', False):
                    break
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()

            await self.app(scope, replay, send)
        finally:
            self.slots.release()
