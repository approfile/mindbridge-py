import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient


class ModelTransportTests(unittest.TestCase):
    def test_auto_thinking_is_provider_scoped_and_can_be_overridden(self):
        settings = Settings(_env_file=None, openai_base_url='https://api.deepseek.com/v1')
        self.assertEqual(AiClient(settings)._openai_options(), {'thinking': {'type': 'disabled'}})
        settings.openai_base_url = 'https://example.test/api'
        self.assertEqual(AiClient(settings)._openai_options(), {})
        settings.openai_thinking_mode = 'enabled'
        self.assertEqual(AiClient(settings)._openai_options(), {'thinking': {'type': 'enabled'}})

    def test_real_review_transport_requests_json_and_rejects_truncation(self):
        settings = Settings(_env_file=None, ai_provider='openai', openai_base_url='https://api.deepseek.com', openai_api_key='test')
        payloads = []
        finish = ['stop']
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'finish_reason': finish[0], 'message': {'content': '{"approved":true,"violations":[]}'}}]})
        original_client = httpx.AsyncClient
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch('app.services.ai.httpx.AsyncClient', client):
            answer = asyncio.run(AiClient(settings).complete_async([AiMessage(role='system', content='JSON review')]))
            self.assertTrue(json.loads(answer)['approved'])
            self.assertEqual(payloads[0]['response_format'], {'type': 'json_object'})
            self.assertEqual(payloads[0]['thinking'], {'type': 'disabled'})
            finish[0] = 'length'
            with self.assertRaisesRegex(RuntimeError, 'truncated'):
                asyncio.run(AiClient(settings).complete_async([AiMessage(role='system', content='JSON review')]))

    def test_stream_truncation_is_fail_closed_before_reply_can_be_accepted(self):
        settings = Settings(_env_file=None, ai_provider='openai', openai_base_url='https://api.deepseek.com', openai_api_key='test')
        def handler(request):
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                                  'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n')
        original_client = httpx.AsyncClient
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler), **kwargs)
        async def collect():
            return [part async for part in AiClient(settings).stream([AiMessage(role='user', content='test')])]
        with patch('app.services.ai.httpx.AsyncClient', client), self.assertRaisesRegex(RuntimeError, 'truncated'):
            asyncio.run(collect())
