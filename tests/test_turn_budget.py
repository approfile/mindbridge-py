import asyncio
from concurrent.futures import Future
import json
import threading
import time
import unittest
import base64
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import event

from app.api.routes import router
from app.core.config import Settings
from app.core.database import get_db
from app.core.security import current_user
from app.models.entities import AgentRunTrace, PsychologicalReport, ToolJob
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService
from app.services.observability import ObservationMiddleware
from app.services.turn_budget import TurnControl, TurnStopped, turn_scope, bounded_timeout
from app.services.turn_execution import TurnRuntime, TurnRejected, await_owned
from app.services.turn_middleware import TurnBudgetMiddleware
from test_tool_reliability import ToolRuntimeFixture


class TurnControlTests(unittest.TestCase):
    def test_remaining_budget_clamps_io_and_stop_is_not_silent_model_fallback(self):
        control = TurnControl(.1, .03)
        with turn_scope(control):
            self.assertLessEqual(bounded_timeout(60), .03)
            control.stop('TURN_TIMEOUT')
            with self.assertRaises(TurnStopped): bounded_timeout(60)

    def test_late_model_return_is_discarded_instead_of_reclassified_as_low(self):
        control = TurnControl(.02, .01)
        settings = Settings(_env_file=None, ai_provider='openai', openai_api_key='fixture', openai_base_url='https://example.test')
        def response(*args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], .01)
            time.sleep(.04)
            return httpx.Response(200, request=httpx.Request('POST','https://example.test'), json={
                'choices':[{'message':{'content':'{"emotion":"HIGH_RISK","emotionScore":4,"risk":"HIGH","confidence":0.1}'},'finish_reason':'stop'}]})
        with turn_scope(control), patch('app.services.ai.httpx.post', response), self.assertRaises(TurnStopped):
            PsychologicalAssessmentService(AiClient(settings)).assess('我需要聊聊最近的情况。')

    def test_global_user_and_session_admission_are_distinct(self):
        settings = Settings(_env_file=None, turn_max_inflight=3, turn_max_inflight_per_user=1)
        runtime = TurnRuntime(settings); self.addCleanup(runtime.close)
        first = runtime.admit(TurnControl(10,1)); first.bind_user(1,'s')
        second = runtime.admit(TurnControl(10,1))
        with self.assertRaises(TurnRejected) as raised: second.bind_user(1,'other')
        self.assertEqual(raised.exception.code,'USER_TURN_CAPACITY_EXCEEDED'); second.http_finished()
        runtime.user_limit=3
        third = runtime.admit(TurnControl(10,1))
        with self.assertRaises(TurnRejected) as raised: third.bind_user(1,'s')
        self.assertEqual(raised.exception.status,409); third.http_finished()
        first.http_finished(); self.assertEqual(runtime.stats()['active'],0)

    def test_cancelled_http_keeps_slot_until_worker_and_finalizer_finish(self):
        settings = Settings(_env_file=None, turn_max_inflight=1)
        runtime=TurnRuntime(settings); self.addCleanup(runtime.close)
        control=TurnControl(10,1); lease=runtime.admit(control); release=threading.Event()
        with turn_scope(control): lease.submit(lambda: release.wait(1))
        lease.finish(lambda: None); lease.http_finished(); control.stop('CLIENT_DISCONNECTED')
        with self.assertRaises(TurnRejected):runtime.admit(TurnControl(10,1))
        release.set(); lease.finished.result(1)
        for _ in range(100):
            if runtime.stats()['active']==0:break
            time.sleep(.005)
        self.assertEqual(runtime.stats()['active'],0)


class ConcurrentHttpTests(ToolRuntimeFixture):
    def setUp(self):
        super().setUp(); self.runtimes=[]; self.uid=self.user.id

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
            for _ in range(200):
                if runtime.stats()['active']==0:break
                time.sleep(.01)

    def app(self, **updates):
        settings=self.settings.model_copy(update=updates); runtime=TurnRuntime(settings);self.runtimes.append(runtime)
        app=FastAPI();app.include_router(router)
        @app.middleware('http')
        async def cache_headers(request, call_next):
            return await call_next(request)
        def db():
            with self.sessions() as session:yield session
        app.dependency_overrides[get_db]=db
        app.dependency_overrides[current_user]=lambda:SimpleNamespace(id=self.uid,roles={'ROLE_USER'})
        app.add_middleware(TurnBudgetMiddleware,settings=settings,runtime=runtime)
        app.add_middleware(ObservationMiddleware)
        return app,settings,runtime

    async def idle(self, runtime):
        for _ in range(300):
            if runtime.stats()['active']==0:return
            await asyncio.sleep(.01)
        self.fail('tracked turn work did not finish')

    def delayed_complete(self, started, seconds=.2):
        original=AiClient.complete
        def complete(ai,messages,**kwargs):
            if 'SLOW' in messages[-1].content and messages[0].content.startswith('你负责分析校园心理健康消息'):
                started.set();time.sleep(seconds)
            return original(ai,messages,**kwargs)
        return complete

    def test_slow_planning_does_not_block_health_or_another_chat(self):
        app,settings,runtime=self.app(turn_timeout_seconds=2);started=threading.Event()
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings),patch.object(AiClient,'complete',self.delayed_complete(started,.3)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    slow=asyncio.create_task(client.post('/api/chat/stream',json={'message':'SLOW 解释Python字典。'}))
                    while not started.is_set():await asyncio.sleep(.001)
                    begin=time.perf_counter();health=await client.get('/actuator/health')
                    self.assertEqual(health.status_code,200);self.assertLess(time.perf_counter()-begin,.15)
                    fast=await client.post('/api/chat/stream',json={'message':'解释Python列表。'})
                    self.assertIn('event: done',fast.text);self.assertFalse(slow.done())
                    self.assertIn('event: done',(await slow).text)
            await self.idle(runtime)
        asyncio.run(run())

    def test_whole_deadline_returns_promptly_and_rejects_more_work_until_cleanup(self):
        app,settings,runtime=self.app(turn_timeout_seconds=.06,turn_max_inflight=1);started=threading.Event()
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings),patch.object(AiClient,'complete',self.delayed_complete(started,.25)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    begin=time.perf_counter();response=await client.post('/api/chat/stream',json={'message':'SLOW 解释Python字典。'})
                    self.assertLess(time.perf_counter()-begin,.18)
                    self.assertIn('TURN_TIMEOUT',response.text);self.assertNotIn('event: done',response.text)
                    busy=await client.post('/api/chat/stream',json={'message':'解释Python列表。'})
                    self.assertEqual(busy.status_code,429);self.assertEqual(busy.headers['retry-after'],'1')
                    await self.idle(runtime)
                    # This request verifies released capacity, not whether the
                    # full mock pipeline fits the earlier 60ms outage budget.
                    settings.turn_timeout_seconds=2
                    fast=await client.post('/api/chat/stream',json={'message':'解释Python列表。'})
                    self.assertIn('event: done',fast.text)
            await self.idle(runtime)
        asyncio.run(run())

    def test_same_session_is_exclusive_without_rejecting_other_sessions(self):
        app,settings,runtime=self.app(turn_timeout_seconds=2,turn_max_inflight_per_user=3);started=threading.Event()
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings),patch.object(AiClient,'complete',self.delayed_complete(started,.25)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    slow=asyncio.create_task(client.post('/api/chat/stream',json={'message':'SLOW 解释Python字典。','sessionId':self.session.public_id}))
                    while not started.is_set():await asyncio.sleep(.001)
                    conflict=await client.post('/api/chat/stream',json={'message':'第二轮','sessionId':self.session.public_id})
                    self.assertEqual(conflict.status_code,409)
                    fast=await client.post('/api/chat/stream',json={'message':'解释Python列表。'})
                    self.assertIn('event: done',fast.text);await slow
            await self.idle(runtime)
        asyncio.run(run())

    def test_expired_high_risk_planning_still_commits_three_durable_jobs(self):
        from app.agents.autonomous import ContextAgent
        app,settings,runtime=self.app(turn_timeout_seconds=.06);original=ContextAgent._rewrite_query
        def delayed(agent,*args):time.sleep(.18);return original(agent,*args)
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings),patch.object(ContextAgent,'_rewrite_query',delayed):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    response=await client.post('/api/chat/stream',json={'message':'我今晚想伤害自己。'})
                    self.assertIn('TURN_TIMEOUT',response.text);self.assertNotIn('event: done',response.text)
                    await self.idle(runtime)
        asyncio.run(run());self.db.expire_all()
        report=self.db.query(PsychologicalReport).one();self.assertEqual(report.risk_level,'HIGH')
        jobs=self.db.query(ToolJob).filter_by(report_id=report.id).all();self.assertEqual(len(jobs),3)
        trace=self.db.query(AgentRunTrace).one();steps=json.loads(trace.agent_steps_json)
        self.assertTrue(any(e.get('kind')=='turn_control' and e['reasonCode']=='TURN_TIMEOUT' for e in steps))
        self.assertFalse(any(e.get('kind')=='response_review' for e in steps));self.drain(jobs)
        self.assertTrue(all(j.status=='SUCCESS' for j in self.rows(ToolJob)))

    def test_authentication_delay_is_inside_http_budget(self):
        app,settings,runtime=self.app(turn_timeout_seconds=.04)
        def auth():time.sleep(.12);return SimpleNamespace(id=self.uid,roles={'ROLE_USER'})
        app.dependency_overrides[current_user]=auth
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    begin=time.perf_counter();response=await client.post('/api/chat/stream',json={'message':'解释Python。'})
                    self.assertLess(time.perf_counter()-begin,.1);self.assertEqual(response.status_code,504)
            await self.idle(runtime)
        asyncio.run(run())

    def test_request_size_is_rejected_before_planning(self):
        app,settings,runtime=self.app(turn_max_request_bytes=1024)
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    response=await client.post('/api/chat/stream',content=json.dumps({'message':'x'*2000}),headers={'Content-Type':'application/json'})
                    self.assertEqual(response.status_code,413)
            await self.idle(runtime)
        asyncio.run(run());self.assertEqual(self.db.query(AgentRunTrace).count(),0)

    def test_actual_auth_worker_keeps_capacity_and_owns_its_sql_session(self):
        app,settings,runtime=self.app(turn_timeout_seconds=.04,turn_max_inflight=1)
        app.dependency_overrides.pop(current_user)
        def verify(*args):time.sleep(.15);return True
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings),patch('app.core.security.verify_password',verify):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    response=await client.post('/api/chat/stream',auth=('student','fixture'),json={'message':'解释Python。'})
                    self.assertEqual(response.status_code,504)
                    self.assertEqual(runtime.stats()['active'],1)
                    busy=await client.post('/api/chat/stream',auth=('student','fixture'),json={'message':'解释Python。'})
                    self.assertEqual(busy.status_code,429)
                    await self.idle(runtime)
        asyncio.run(run());self.assertEqual(self.db.query(AgentRunTrace).count(),0)

    def test_done_before_deadline_is_not_reclassified_by_slow_finalization(self):
        from app.services.turn_work import OwnedTurnWork
        app,settings,runtime=self.app(turn_timeout_seconds=1)
        original=OwnedTurnWork.finalize
        finalizer_started=threading.Event();release_finalizer=threading.Event()
        def delayed(work):
            finalizer_started.set()
            if not release_finalizer.wait(5):
                raise AssertionError('test did not release finalizer')
            return original(work)
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings),patch.object(OwnedTurnWork,'finalize',delayed):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    try:
                        response=await client.post('/api/chat/stream',json={'message':'解释Python。'})
                        self.assertTrue(finalizer_started.is_set())
                        self.assertIn('event: done',response.text);self.assertNotIn('TURN_TIMEOUT',response.text)
                        self.assertEqual(runtime.stats()['active'],1)
                    finally:
                        release_finalizer.set()
                    await self.idle(runtime)
        asyncio.run(run())
        steps=json.loads(self.db.query(AgentRunTrace).one().agent_steps_json)
        self.assertEqual(next(s['reasonCode'] for s in steps if s.get('kind')=='turn_control'),'COMPLETED')

    def asgi_scope(self):
        return {'type':'http','asgi':{'version':'3.0','spec_version':'2.0'},'http_version':'1.1',
                'method':'POST','scheme':'http','path':'/api/chat/stream','raw_path':b'/api/chat/stream',
                'query_string':b'','root_path':'','headers':[(b'content-type',b'application/json')],
                'client':('127.0.0.1',1234),'server':('test',80)}

    def test_backpressure_is_inside_deadline_and_eof_is_not_done(self):
        app,settings,runtime=self.app(turn_timeout_seconds=.15)
        async def run():
            received=False;messages=[];never=asyncio.Event()
            async def receive():
                nonlocal received
                if not received:
                    received=True;return {'type':'http.request','body':json.dumps({'message':'解释Python。'}).encode(),'more_body':False}
                await never.wait()
            async def send(message):
                if message.get('body',b'').startswith(b'event: token\n'):
                    await never.wait()
                messages.append(message)
            with patch('app.api.routes.get_settings',return_value=settings):
                begin=time.perf_counter();await app(self.asgi_scope(),receive,send)
                self.assertLess(time.perf_counter()-begin,.3)
            body=b''.join(m.get('body',b'') for m in messages)
            self.assertIn(b'TURN_TIMEOUT',body);self.assertNotIn(b'event: done',body)
            await self.idle(runtime)
        asyncio.run(run())

    def test_real_disconnect_during_planning_keeps_high_outbox(self):
        from app.agents.autonomous import ContextAgent
        app,settings,runtime=self.app(turn_timeout_seconds=2);started=threading.Event();original=ContextAgent._rewrite_query
        def delayed(agent,*args):started.set();time.sleep(.2);return original(agent,*args)
        async def run():
            received=False;disconnect=asyncio.Event();messages=[]
            async def receive():
                nonlocal received
                if not received:
                    received=True;return {'type':'http.request','body':json.dumps({'message':'我今晚想伤害自己。'}).encode(),'more_body':False}
                await disconnect.wait();return {'type':'http.disconnect'}
            async def send(message):messages.append(message)
            with patch('app.api.routes.get_settings',return_value=settings),patch.object(ContextAgent,'_rewrite_query',delayed):
                request=asyncio.create_task(app(self.asgi_scope(),receive,send))
                while not started.is_set():await asyncio.sleep(.001)
                disconnect.set();await request
                self.assertGreater(runtime.stats()['active'],0)
                await self.idle(runtime)
            self.assertNotIn(b'event: done',b''.join(m.get('body',b'') for m in messages))
        asyncio.run(run());self.db.expire_all()
        self.assertEqual(self.db.query(PsychologicalReport).one().risk_level,'HIGH')
        self.assertEqual(self.db.query(ToolJob).count(),3)

    def test_worker_sessions_are_created_and_used_off_event_loop(self):
        app,settings,runtime=self.app();threads=[];main_thread=threading.get_ident()
        def execute(state):threads.append(threading.get_ident())
        from app.services.turn_work import OwnedTurnWork
        original=OwnedTurnWork.__init__
        def initialize(work,*args,**kwargs):
            original(work,*args,**kwargs)
            event.listen(work.sessions,'do_orm_execute',execute)
        async def run():
            with patch('app.api.routes.get_settings',return_value=settings),patch.object(OwnedTurnWork,'__init__',initialize):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    response=await client.post('/api/chat/stream',json={'message':'解释Python字典。'})
                    self.assertIn('event: done',response.text)
            await self.idle(runtime)
        asyncio.run(run());self.assertTrue(threads);self.assertNotIn(main_thread,threads)
