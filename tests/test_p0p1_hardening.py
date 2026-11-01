import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

import httpx

from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.core.enums import MessageRole
from app.models.entities import ChatMessage, SessionMemory
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.memory_facts import VERSION, fact_operations
from app.services.session_memory import SessionMemoryService
from app.services.query_rewrite import rewrite_query
from app.services.response_review import ReplyReviewPipeline, safe_template, deterministic_violations
from app.services.reply_grounding import candidate_sentences
from app.services.reply_safety import trusted_reply_evidence, crisis_elements
from app.services.knowledge import SearchResult
from app.services.turn_budget import TurnControl, TurnStopped, turn_scope
from app.services.ai import AiClient
from test_tool_reliability import ToolRuntimeFixture


class FactParsingTests(unittest.TestCase):
    def test_questions_and_reported_examples_are_not_assertions(self):
        for text in ['项目代号是什么？', '我的目标岗位是不是后端？', '项目代号是星河吗',
                     '请问项目名称为哪个', '如果项目代号是星河73，会怎样？',
                     '例如项目名称是示例。', '他说项目代号是别人的。',
                     '请翻译 My project codename is Other.', 'What is my project codename?',
                     '请问目标岗位是工程师', '项目名称是' + 'x' * 70]:
            with self.subTest(text=text):self.assertEqual(fact_operations(text), [])

    def test_assertions_preserve_internal_punctuation_and_normalize_sentence_end(self):
        for text,key,value in [('My project codename is Cedar58.','项目代号','Cedar58'),
                               ('我的项目代号是v1.2。','项目代号','v1.2'),
                               ('项目名称是.NET。','项目名称','.NET'),
                               ('称呼是何同学。','称呼','何同学'),
                               ('每天可用时间是1.5小时。','每天可用时间','1.5小时'),
                               ('请把项目代号改为星河73。','项目代号','星河73')]:
            with self.subTest(text=text):self.assertEqual(fact_operations(text),[(key,value)])

    def test_correction_forgetting_and_multiple_updates_follow_message_order(self):
        self.assertEqual(fact_operations('项目代号是甲，项目代号不是甲，而是乙，项目代号改为丙。'),
                         [('项目代号','甲'),('项目代号','乙'),('项目代号','丙')])
        self.assertEqual(fact_operations('请忘记project codename。'),[('项目代号',None)])


class MemoryAndRoutingHardeningTests(ToolRuntimeFixture):
    def save(self,text):
        MindBridgeAgentHarness(self.db,self.settings).save_message(self.user,self.session,MessageRole.USER,text)

    def snapshot(self):
        return SessionMemoryService(self.db,self.settings).load(self.user,self.session)

    def test_followup_questions_do_not_corrupt_persisted_facts(self):
        for statement,question,key,value in [('项目代号是银杏73。','我这次会话的项目代号是什么？','项目代号','银杏73'),
                                             ('目标岗位是后端Agent工程实习生。','我的目标岗位是什么？','目标岗位','后端Agent工程实习生'),
                                             ('My project codename is Cedar58.','What is my project codename?','项目代号','Cedar58')]:
            self.save(statement);source=self.snapshot().facts[key]['sourceMessageId']
            self.save(question);fact=self.snapshot().facts[key]
            self.assertEqual(fact,{'value':value,'sourceMessageId':source})

    def test_legacy_poison_with_valid_user_provenance_is_rebuilt(self):
        self.save('项目代号是银杏73。');original=self.snapshot().facts['项目代号']['sourceMessageId']
        self.save('项目代号是什么？')
        question=self.db.query(ChatMessage).order_by(ChatMessage.id.desc()).first()
        memory=self.db.query(SessionMemory).one()
        memory.state_json=json.dumps({'facts':{'项目代号':{'value':'什么','sourceMessageId':question.id}}})
        memory.last_message_id=question.id;self.db.commit()
        fixed=self.snapshot()
        self.assertEqual(fixed.facts['项目代号'],{'value':'银杏73','sourceMessageId':original})
        self.assertEqual(json.loads(self.db.query(SessionMemory).one().state_json)['extractorVersion'],VERSION)
        self.save('请忘记project codename。');self.assertNotIn('项目代号',self.snapshot().facts)
        self.assertNotIn('项目代号',self.snapshot().facts)

    def test_frozen_denial_consultation_examples_keep_low_risk_and_consult_route(self):
        tasks=json.loads((Path(__file__).resolve().parents[1]/'app/evaluation/data/agent-tasks-v1/tasks.json').read_text())['tasks']
        selected={'normal-05','normal-07','normal-10','evidence-08','recovery-02'}
        for task in tasks:
            if task['id'] not in selected:continue
            with self.subTest(task=task['id']):
                result=MindBridgeAgentHarness(self.db,self.settings).run(self.user,ChatRequest(message=task['turns'][0]['input'],sessionId=self.session.public_id))
                self.assertEqual(result.intent.value,'CONSULT');self.assertEqual(result.risk_level,'LOW')

    def test_denial_with_irritability_is_support_but_technical_and_academic_tasks_stay_chat(self):
        for text,intent in [('我没有自伤想法，但很烦躁，想找人聊聊。','CONSULT'),
                            ('我没有自伤想法，请解释Python字典。','CHAT'),
                            ('我的论文研究失眠的定义，请解释研究方法。','CHAT')]:
            with self.subTest(text=text):
                result=MindBridgeAgentHarness(self.db,self.settings).run(self.user,ChatRequest(message=text,sessionId=self.session.public_id))
                self.assertEqual(result.intent.value,intent);self.assertEqual(result.risk_level or 'LOW','LOW')


class QueryBudgetHardeningTests(unittest.TestCase):
    def test_valid_rewrite_beyond_sixty_characters_is_preserved(self):
        query='考试季作息与沟通支持。'*12
        result=rewrite_query(SimpleNamespace(complete=lambda messages:query),[],'原始需求')
        self.assertEqual(result.query,query);self.assertFalse(result.truncated);self.assertFalse(result.degraded)

    def test_overlong_model_output_falls_back_to_full_original(self):
        original='前面的背景。'*20+'真正问题是怎样与室友沟通，我没有危险想法。'
        result=rewrite_query(SimpleNamespace(complete=lambda messages:'x'*2000),[],original)
        self.assertEqual(result.query,original);self.assertEqual(result.reasonCode,'REWRITE_OUTPUT_TOO_LONG')
        self.assertTrue(result.degraded);self.assertFalse(result.truncated)

    def test_extreme_input_window_keeps_both_ends_and_declares_omission(self):
        def fail(messages):raise httpx.ReadTimeout('no response')
        original='当前情况：'+('中间背景'*500)+'最后需求：核对学校咨询渠道'
        result=rewrite_query(SimpleNamespace(complete=fail),[],original,limit=128)
        self.assertLessEqual(len(result.query),128);self.assertTrue(result.query.startswith('当前情况'))
        self.assertTrue(result.query.endswith('最后需求：核对学校咨询渠道'));self.assertTrue(result.truncated)
        self.assertEqual(result.selectionStrategy,'head_tail_window');self.assertEqual(result.reasonCode,'REWRITE_TIMEOUT')


def outcome(references=(),text='我最近考试压力很大。',risk='LOW'):
    return SimpleNamespace(risk_level=risk,plan_accepted=True,budget_exhausted=False,model_input=text,
                           response_messages=[AiMessage(role='user',content=text)],retrieved_knowledge=list(references),memory_facts={})


class ReplyHardeningTests(unittest.TestCase):
    def settings(self,**updates):
        return Settings(_env_file=None,reply_max_revisions=0,model_retry_base_delay_seconds=0,**updates)

    def test_completed_action_variants_cannot_be_hidden_by_unrelated_negation(self):
        for text in ['不用担心，我已经帮你预约了心理咨询。','我没有拒绝帮助，而且已经通知了辅导员。',
                     '我没有拒绝帮助而且已经通知了辅导员。','I cannot diagnose and I have already called a counselor.',
                     '系统已为你提交个案。','通知已经发出。',"I've notified your counselor.",
                     'I have booked an appointment for you.','Your appointment has been confirmed.']:
            with self.subTest(text=text):self.assertIn('UNSUPPORTED_CLAIM',deterministic_violations(text,'LOW'))
        for text in ['我没有替你预约，可以帮你整理求助文字。','你已经联系了朋友，这是很好的开始。',
                     '我不能声称我已经通知了老师。',
                     "I have not contacted anyone on your behalf.",'如果预约成功后，可以记录时间。']:
            with self.subTest(text=text):self.assertNotIn('UNSUPPORTED_CLAIM',deterministic_violations(text,'LOW'))

    def test_high_risk_requires_concrete_action_and_fallback_is_actionable(self):
        self.assertIn('CRISIS_SUPPORT_MISSING',deterministic_violations('安全最重要，请联系辅导员。','HIGH'))
        for language in ['zh','en']:
            text=safe_template('HIGH',language)
            self.assertFalse(deterministic_violations(text,'HIGH'))
            self.assertTrue(crisis_elements(text)['safetyAction'])
            self.assertTrue(text.endswith(('？','?')))

    def test_partial_generation_is_discarded_before_retry_and_review(self):
        class Generator:
            calls=0
            async def stream(self,messages):
                self.calls+=1
                if self.calls==1:
                    yield 'UNREVIEWED_PARTIAL'
                    raise httpx.ReadError('controlled disconnect')
                yield '我们先一起梳理你的困扰。'
        class Reviewer:
            async def complete_async(self,messages):
                data=json.loads(messages[-1].content)
                if 'UNREVIEWED_PARTIAL' in data['candidate']:raise AssertionError('partial leaked into retry')
                return '{"approved":true,"violations":[]}'
        gen=Generator();reply=asyncio.run(ReplyReviewPipeline(self.settings(),gen,Reviewer()).run(outcome()))
        self.assertTrue(reply.accepted);self.assertEqual(gen.calls,2)
        self.assertNotIn('UNREVIEWED_PARTIAL',reply.text)
        self.assertEqual(next(e for e in reply.events if e['kind']=='model_retry')['status'],'scheduled')

    def test_transient_review_error_retries_same_candidate_without_regeneration(self):
        class Generator:
            calls=0
            async def stream(self,messages):self.calls+=1;yield '我们先一起梳理。'
        class Reviewer:
            calls=0
            async def complete_async(self,messages):
                self.calls+=1
                if self.calls==1:raise httpx.ReadTimeout('controlled')
                return '{"approved":true,"violations":[]}'
        gen=Generator();reviewer=Reviewer()
        reply=asyncio.run(ReplyReviewPipeline(self.settings(),gen,reviewer).run(outcome()))
        self.assertTrue(reply.accepted);self.assertEqual((gen.calls,reviewer.calls),(1,2))

    def test_actual_client_retries_clean_but_incomplete_sse_and_discards_partial_text(self):
        settings=self.settings(ai_provider='openai',openai_api_key='fixture',openai_base_url='https://example.test')
        requests=[]
        def handler(request):
            body=json.loads(request.content);requests.append(body)
            if not body['stream']:
                return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"approved":true,"violations":[]}'}}]})
            if len(requests)==1:
                return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"PARTIAL_MUST_NOT_ESCAPE"}}]}\n\n')
            return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"我们先一起梳理。"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        original=httpx.AsyncClient
        with patch('app.services.ai.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
            reply=asyncio.run(ReplyReviewPipeline(settings,AiClient(settings),AiClient(settings)).run(outcome()))
        self.assertTrue(reply.accepted);self.assertNotIn('PARTIAL',reply.text)
        self.assertEqual([x['stream'] for x in requests],[True,True,False])

    def test_repeated_transient_failure_is_capped(self):
        class Generator:
            calls=0
            async def stream(self,messages):self.calls+=1;raise httpx.ReadTimeout('controlled');yield ''
        gen=Generator();reply=asyncio.run(ReplyReviewPipeline(self.settings(),gen,object()).run(outcome()))
        self.assertEqual(gen.calls,2);self.assertFalse(reply.accepted)
        self.assertEqual([x['status'] for x in reply.events if x['kind']=='model_retry'],['scheduled','exhausted'])

    def test_explicit_filter_or_tool_termination_is_not_retried_as_network_eof(self):
        settings=self.settings(ai_provider='openai',openai_api_key='fixture',openai_base_url='https://example.test')
        for reason in ['content_filter','tool_calls']:
            requests=[]
            def handler(request):
                requests.append(request)
                return httpx.Response(200,text='data: '+json.dumps({'choices':[{'delta':{'content':'PARTIAL'},'finish_reason':reason}]})+'\n\ndata: [DONE]\n\n')
            original=httpx.AsyncClient
            with patch('app.services.ai.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw)):
                reply=asyncio.run(ReplyReviewPipeline(settings,AiClient(settings),AiClient(settings)).run(outcome()))
            self.assertFalse(reply.accepted);self.assertEqual(len(requests),1);self.assertNotIn('PARTIAL',reply.text)

    def test_billing_auth_bad_request_invalid_json_and_truncation_are_not_retried(self):
        for error in [httpx.HTTPStatusError('controlled',request=httpx.Request('POST','https://example.test'),response=httpx.Response(code))
                      for code in [400,401,402,403,422]]+[RuntimeError('Model output truncated by token budget')]:
            class Generator:
                calls=0
                async def stream(self,messages):self.calls+=1;raise error;yield ''
            gen=Generator();reply=asyncio.run(ReplyReviewPipeline(self.settings(),gen,object()).run(outcome(risk='HIGH')))
            self.assertFalse(reply.accepted);self.assertEqual(gen.calls,1)
        class Gen:
            async def stream(self,messages):yield '我们先一起梳理。'
        class Invalid:
            calls=0
            async def complete_async(self,messages):self.calls+=1;return '{"approved":"true","violations":[]}'
        judge=Invalid();reply=asyncio.run(ReplyReviewPipeline(self.settings(),Gen(),judge).run(outcome()))
        self.assertFalse(reply.accepted);self.assertEqual(judge.calls,1)

    def test_retry_after_cannot_extend_local_reply_deadline(self):
        class Generator:
            calls=0
            async def stream(self,messages):
                self.calls+=1
                raise httpx.HTTPStatusError('controlled',request=httpx.Request('POST','https://example.test'),
                                           response=httpx.Response(429,headers={'Retry-After':'60'}))
                yield ''
        gen=Generator();begin=time.monotonic()
        reply=asyncio.run(ReplyReviewPipeline(self.settings(reply_timeout_seconds=.1),gen,object()).run(outcome()))
        self.assertLess(time.monotonic()-begin,.3);self.assertEqual(gen.calls,1)
        self.assertEqual(next(e for e in reply.events if e['kind']=='model_retry')['status'],'budget_insufficient')

    def test_stopped_and_cancelled_turns_never_start_another_attempt(self):
        class Generator:
            calls=0
            async def stream(self,messages):self.calls+=1;raise asyncio.CancelledError();yield ''
        gen=Generator()
        with self.assertRaises(asyncio.CancelledError):asyncio.run(ReplyReviewPipeline(self.settings(),gen,object()).run(outcome()))
        self.assertEqual(gen.calls,1)
        control=TurnControl(1,1);control.stop('CLIENT_DISCONNECTED');gen=Generator()
        with turn_scope(control),self.assertRaises(TurnStopped):
            asyncio.run(ReplyReviewPipeline(self.settings(),gen,object()).run(outcome()))
        self.assertEqual(gen.calls,0)


class GroundingHardeningTests(unittest.TestCase):
    def test_clarification_identifies_fact_sentence_and_revision_receives_actionable_feedback(self):
        owner = self
        class Generator:
            calls = 0
            async def stream(self, messages):
                self.calls += 1
                if self.calls == 1:
                    yield '我愿意听你说。这个方法能缓解你的焦虑。'
                else:
                    feedback = messages[-1].content
                    owner.assertIn('FACT_MISCLASSIFIED_AS_SUPPORT', feedback)
                    owner.assertIn('这个方法能缓解你的焦虑。', feedback)
                    owner.assertIn('删除或修正', feedback)
                    yield '我愿意听你说。'
        class Judge:
            calls = 0
            async def complete_async(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return '{"approved":true,"violations":[],"evidenceChecks":[{"sentenceId":1,"kind":"support"},{"sentenceId":2,"kind":"support"}]}'
                if self.calls == 2:
                    feedback = messages[-1].content
                    owner.assertIn('"id": 2', feedback)
                    owner.assertIn('这个方法能缓解你的焦虑。', feedback)
                    return '{"approved":false,"violations":["UNSUPPORTED_CLAIM"]}'
                return '{"approved":true,"violations":[],"evidenceChecks":[{"sentenceId":1,"kind":"support"}]}'
        gen, judge = Generator(), Judge()
        result = asyncio.run(ReplyReviewPipeline(Settings(_env_file=None), gen, judge).run(
            outcome([SearchResult(1, 'support.md', '请联系可信任的人。', 1)])))
        self.assertTrue(result.accepted)
        self.assertEqual((gen.calls, judge.calls), (2, 3))
        self.assertEqual(len([e for e in result.events if e['kind'] == 'grounding_clarification']), 1)

    def test_one_clarification_can_bind_existing_numeric_advice_without_regenerating(self):
        class Generator:
            calls=0
            async def stream(self,messages):self.calls+=1;yield '可以先用10分钟开始。'
        class Judge:
            calls=0
            async def complete_async(self,messages):
                self.calls+=1
                check={'sentenceId':1,'kind':'support'} if self.calls==1 else {
                    'sentenceId':1,'kind':'retrieved_fact','referenceId':'R1','sourceQuote':'10分钟也算开始'}
                return json.dumps({'approved':True,'violations':[],'evidenceChecks':[check]})
        gen=Generator();judge=Judge();s=Settings(_env_file=None,reply_max_revisions=0)
        result=asyncio.run(ReplyReviewPipeline(s,gen,judge).run(outcome([SearchResult(1,'study.md','低状态时10分钟也算开始。',1)])))
        self.assertTrue(result.accepted);self.assertEqual((gen.calls,judge.calls),(1,2))
        self.assertEqual(len([e for e in result.events if e['kind']=='grounding_clarification']),1)

    def test_clarification_never_overrides_rejection_or_uses_an_unbounded_loop(self):
        class Generator:
            async def stream(self,messages):yield '大脑执行功能会下降。'
        class Judge:
            calls=0
            async def complete_async(self,messages):
                self.calls+=1
                if self.calls==1:return '{"approved":true,"violations":[],"evidenceChecks":[{"sentenceId":1,"kind":"support"}]}'
                return '{"approved":false,"violations":["UNSUPPORTED_CLAIM"]}'
        judge=Judge();s=Settings(_env_file=None,reply_max_revisions=0)
        result=asyncio.run(ReplyReviewPipeline(s,Generator(),judge).run(outcome([SearchResult(1,'study.md','请先做一小步。',1)])))
        self.assertFalse(result.accepted);self.assertEqual(judge.calls,2)

    def run_reply(self,checks,*,max_revisions=0):
        candidate='听起来你很难受。可以先固定起床时间。'
        class Generator:
            async def stream(self,messages):yield candidate
        class Reviewer:
            async def complete_async(self,messages):
                data=json.loads(messages[-1].content)
                self.data=data
                return json.dumps({'approved':True,'violations':[],**({'evidenceChecks':checks} if checks is not None else {})})
        reviewer=Reviewer();settings=Settings(_env_file=None,reply_max_revisions=max_revisions)
        result=asyncio.run(ReplyReviewPipeline(settings,Generator(),reviewer).run(outcome([SearchResult(1,'sleep.md','建议固定起床时间，避免作息漂移。',1)])))
        return result,reviewer

    def test_provenance_bound_grounding_accepts_empathy_and_supported_advice(self):
        result,judge=self.run_reply([{'sentenceId':1,'kind':'support'},
                                    {'sentenceId':2,'kind':'retrieved_fact','referenceId':'R1','sourceQuote':'固定起床时间'}])
        self.assertTrue(result.accepted);self.assertTrue(judge.data['requiresGrounding'])
        review=next(e for e in result.events if e['kind']=='response_review')
        self.assertEqual(len(review['groundingChecks']),2)
        self.assertNotIn('固定起床时间',json.dumps(review['groundingChecks'],ensure_ascii=False))

    def test_bare_approval_missing_sentence_wrong_source_and_invented_quote_fail_closed(self):
        for checks in [None,[],[{'sentenceId':1,'kind':'support'}],
                       [{'sentenceId':1,'kind':'support'},{'sentenceId':1,'kind':'support'}],
                       [{'sentenceId':1,'kind':'support'},{'sentenceId':2,'kind':'retrieved_fact','referenceId':'CURRENT','sourceQuote':'考试'}],
                       [{'sentenceId':1,'kind':'support'},{'sentenceId':2,'kind':'retrieved_fact','referenceId':'R1','sourceQuote':'本校24小时开放'}]]:
            with self.subTest(checks=checks):self.assertFalse(self.run_reply(checks)[0].accepted)

    def test_only_included_reference_bytes_receive_provenance_credit(self):
        raw='来源正文'*3000
        text,sources=trusted_reply_evidence(outcome([SearchResult(1,'long.md',raw,1),SearchResult(2,'excluded.md','MUST_NOT_CREDIT',1)]))
        self.assertLessEqual(len(text),8000);self.assertEqual(len(sources),1)
        self.assertEqual(sources[0]['contentSha256'],hashlib.sha256(text.encode()).hexdigest())
        self.assertNotIn('MUST_NOT_CREDIT',text)

    def test_biomedical_numeric_and_causal_assertions_cannot_use_empathy_escape_hatch(self):
        from app.services.reply_grounding import EvidenceCheck,verify_grounding
        for candidate in ['低落时大脑执行功能像被调低了。','建议花15分钟处理这件事。',
                          '这个方法能缓解你的焦虑。','本校心理中心全天开放。']:
            with self.subTest(candidate=candidate):
                checks=[EvidenceCheck(sentenceId=1,kind='support')]
                approved,_,reason=verify_grounding(checks,candidate_sentences(candidate),[],[])
                self.assertFalse(approved);self.assertEqual(reason,'FACT_MISCLASSIFIED_AS_SUPPORT')
