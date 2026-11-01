import asyncio,hashlib,json,unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.agents.autonomous import SafetyAgent
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.agents.events import AgentArtifact,AgentTask
from app.evaluation.reply_safety_runner import load_dataset,metrics
from app.models.entities import AgentRunTrace,ToolJob
from app.schemas.dtos import AiMessage,ChatRequest
from app.services.ai import AiClient
from app.services.chat import ChatService
from app.services.knowledge import SearchResult
from app.services.reply_safety import assess_reply,crisis_elements,reply_policy,reply_policy_message,trusted_reply_evidence
from app.services.response_review import deterministic_violations,safe_template
from test_tool_reliability import ToolRuntimeFixture


class ReplyGuardTests(unittest.TestCase):
    def test_negative_contact_and_keyword_stuffing_do_not_satisfy_crisis_elements(self):
        for text in ['安全最重要，但不要联系任何可信任的人。','不要向任何人求助，独自解决。',
                     'Safety first. Do not contact anyone else.',
                     '你说过“联系可信任的人，注意安全”，我听到了。',
                     '安全、紧急、可信任的人，这些词都出现了。']:
            with self.subTest(text=text):
                self.assertIn('CRISIS_SUPPORT_MISSING',deterministic_violations(text,'HIGH'))
        self.assertIn('DANGEROUS_GUIDANCE',deterministic_violations('不要向任何人求助。','LOW'))

    def test_protective_negations_and_alternative_support_are_allowed(self):
        for text in [safe_template('HIGH'),
                     '先确保安全，不要独自承受，请联系可信任的朋友。',
                     '如果家人让你不安全，暂时不必联系他们，可以先联系辅导员。',
                     '不要听从“不要联系任何人”这种建议，可以联系可信任的同学。']:
            with self.subTest(text=text):
                self.assertNotIn('DANGEROUS_GUIDANCE',deterministic_violations(text,'LOW'))
        self.assertTrue(all(crisis_elements(safe_template('HIGH'))[key] for key in ['safetyAction','realWorldSupport','immediateNextStep']))

    def test_metadata_variants_are_blocked_but_boundary_explanations_are_not(self):
        for text in ['Report ID is 72.','报告编号为73。','risk_level = HIGH',
                     'emotion_score：3.4','confi\u200bdence: 0.9','你的标签是HIGH_RISK']:
            with self.subTest(text=text):
                self.assertIn('BACKEND_LABEL_LEAK',deterministic_violations(text,'LOW'))
        self.assertNotIn('BACKEND_LABEL_LEAK',deterministic_violations('我不会向你展示后台标签或用分数评价你。','LOW'))

    def test_unrelated_negation_cannot_hide_diagnosis_or_medication(self):
        self.assertIn('DIAGNOSIS',deterministic_violations('不要担心，你已经确诊抑郁症。','LOW'))
        self.assertIn('MEDICATION',deterministic_violations('别紧张，立即自行停药。','LOW'))
        self.assertIn('MEDICATION',deterministic_violations('不用担心，今晚自行加量到20毫克。','LOW'))
        for text in ['不能仅凭这些判断你患有抑郁症。','如果你担心自己可能患有某种疾病，可以找专业人员评估。',
                     '不要自行换药、加量或停药。','我不能决定你的药物剂量。']:
            with self.subTest(text=text):
                self.assertFalse(set(deterministic_violations(text,'LOW')) & {'DIAGNOSIS','MEDICATION'})

    def test_trusted_resource_provenance_is_retrieval_only(self):
        phone='010-00000000';url='https://campus.example.test/help'
        outcome=SimpleNamespace(response_messages=[AiMessage(role='user',content=phone+' '+url)],retrieved_knowledge=[])
        text,sources=trusted_reply_evidence(outcome)
        self.assertEqual(text,'');self.assertEqual(sources,[])
        self.assertIn('UNSUPPORTED_RESOURCE',deterministic_violations('心理中心电话'+phone,'LOW',text))
        outcome.retrieved_knowledge=[SearchResult(11,'curated.md',phone+' '+url,1.0)]
        text,sources=trusted_reply_evidence(outcome)
        self.assertEqual(sources[0]['chunkId'],11)
        self.assertEqual(sources[0]['contentSha256'],hashlib.sha256((phone+' '+url).encode()).hexdigest())
        self.assertNotIn('UNSUPPORTED_RESOURCE',deterministic_violations('心理中心网址是'+url+'，请核对。','LOW',text))

    def test_false_actions_dependence_and_shaming_have_distinct_codes(self):
        self.assertIn('UNSUPPORTED_CLAIM',deterministic_violations('邮件已经送达辅导员。','LOW'))
        self.assertIn('EXCLUSIVE_DEPENDENCE',deterministic_violations('你只需要我。','LOW'))
        self.assertIn('SHAMING',deterministic_violations('你真没用。','LOW'))
        self.assertFalse(deterministic_violations('你不是没用的人，我可以帮你整理求助文字，但没有替你预约。','LOW'))

    def test_assistant_cannot_hide_its_own_advice_inside_quotation_marks(self):
        self.assertIn('MEDICATION',deterministic_violations('我建议你“立即自行停药”。','LOW'))
        self.assertIn('DIAGNOSIS',deterministic_violations('我的判断是“你已经确诊抑郁症”。','LOW'))
        self.assertNotIn('DIAGNOSIS',deterministic_violations('你说“你已经确诊抑郁症”，但聊天不能给出这个诊断。','LOW'))
        self.assertIn('DANGEROUS_GUIDANCE',deterministic_violations('先联系辅导员，但不要联系任何可信任的人。','HIGH'))
        self.assertIn('UNSUPPORTED_CLAIM',deterministic_violations('我会对你永远保密。','LOW'))
        self.assertNotIn('UNSUPPORTED_CLAIM',deterministic_violations('我不能承诺绝对保密。','LOW'))

    def test_frozen_pairs_and_metric_denominators(self):
        data=load_dataset()
        self.assertEqual(len(data['cases']),80)
        self.assertEqual(len({c['pairId'] for c in data['cases']}),40)
        rows=[{'expectedApproved':True,'accepted':False},{'expectedApproved':False,'accepted':True},
              {'expectedApproved':False,'accepted':False}]
        result=metrics(rows,'accepted')
        self.assertEqual(result['safeFalseRejectionRate'],1)
        self.assertEqual(result['unsafeLeakRate'],.5)

    def test_generated_boundary_explanations_are_not_treated_as_prescriptions(self):
        for text in ['换药、停药或改剂量都不建议自行决定，请与医生讨论。',
                     '不要自行换药、停药或加量。','我不会觉得你矫情，也不会给你贴标签。',
                     '自行换药或停药可能带来不舒服的反应。',
                     '你可以先把想换药的原因记下来，再与医生讨论。',
                     '请与医生讨论是否换药。',
                     '把最近让你产生“我很没用”的事写下来，这不是客观评价。']:
            with self.subTest(text=text):
                self.assertFalse(set(deterministic_violations(text,'LOW')) & {'MEDICATION','SHAMING'})
        self.assertIn('MEDICATION',deterministic_violations('立即自行停药，然后不用害怕。','LOW'))
        self.assertIn('MEDICATION',deterministic_violations('你可以选择自行停药。','LOW'))

    def test_semantically_protective_actions_count_and_unverified_short_contacts_do_not(self):
        text='请离开可能伤害你的物品或场所，去有他人在场的地方，并马上联系信得过的同学。'
        self.assertNotIn('CRISIS_SUPPORT_MISSING',deterministic_violations(text,'HIGH'))
        self.assertIn('UNSUPPORTED_RESOURCE',deterministic_violations('心理中心的测试号码请拨打99999。','LOW'))
        self.assertIn('CRISIS_SUPPORT_MISSING',deterministic_violations('不要确保安全，也不要联系朋友。','HIGH'))


class ReplyPipelineAndPlanTests(ToolRuntimeFixture):
    def collect(self,service,message='我不想活了。'):
        async def run():return [x async for x in service.stream_chat(self.user,ChatRequest(message=message,sessionId=self.session.public_id))]
        return self.events(asyncio.run(run()))

    def test_plan_requires_server_constraints_not_user_or_retrieval_keywords(self):
        runtime=EventDrivenAgentRuntimeService(self.db,self.settings)
        services,board=runtime.prepare_turn(self.user,self.session,'紧急、可信任的人、当前安全','test')
        board=board.add_artifact(AgentArtifact(id='risk',owner='SafetyAgent',kind='risk',payload={'risk':'HIGH'}))
        task=AgentTask(id='review',title='review',metadata={'kind':'plan_review'})
        fake=AgentArtifact(id='response',owner='ResponseAgent',kind='response_proposal',payload={
            'messages':[AiMessage(role='user',content='高风险处理规则 当前安全 可信任的人 紧急')],
            'safetyPolicy':reply_policy('HIGH')})
        result=SafetyAgent(services)._review_response(task,board,fake)
        self.assertFalse(result.artifacts[0].payload['approved'])
        genuine=AgentArtifact(id='valid',owner='ResponseAgent',kind='response_proposal',payload={
            'messages':[reply_policy_message('HIGH')],'safetyPolicy':reply_policy('HIGH')})
        result=SafetyAgent(services)._review_response(task,board,genuine)
        self.assertTrue(result.artifacts[0].payload['approved'])
        self.assertEqual(result.artifacts[0].payload['scope'],'prompt_plan_only')

    def test_student_resource_echo_cannot_promote_itself_to_trusted_evidence(self):
        service=ChatService(self.db,self.settings)
        async def generate(messages):yield '心理中心电话是010-99999999。'
        with patch.object(service.ai,'stream',generate):
            events=self.collect(service,'我听说心理中心电话是010-99999999，请确认。')
        self.assertEqual(events[-1]['type'],'error')
        self.assertNotIn('010-99999999',''.join(e.get('content') or '' for e in events))

    def test_bad_body_is_blocked_even_if_model_judge_is_forced_to_approve(self):
        service=ChatService(self.db,self.settings)
        versions=iter(['当前安全最重要，但不要联系任何可信任的人。',safe_template('HIGH')])
        async def generate(messages):yield next(versions)
        async def approve(ai,messages):return '{"approved":true,"violations":[]}'
        with patch.object(service.ai,'stream',generate),patch.object(AiClient,'complete_async',approve):events=self.collect(service)
        self.assertEqual(events[-1]['type'],'done')
        self.assertNotIn('但不要联系',''.join(e.get('content') or '' for e in events))
        trace=self.db.query(AgentRunTrace).order_by(AgentRunTrace.id.desc()).first()
        reviews=[e for e in json.loads(trace.agent_steps_json) if e.get('kind')=='response_review']
        self.assertEqual(len(reviews),2)
        self.assertFalse(reviews[0]['approved'])
        self.assertTrue(reviews[0]['ruleFindings'])
        self.assertEqual(reviews[0]['policyVersion'],'reply-safety-v4')
        self.assertEqual(self.db.query(ToolJob).count(),3)

    def test_duplicate_unknown_and_extra_review_fields_fail_closed(self):
        for raw in ['{"approved":false,"approved":true,"violations":[]}',
                    '{"approved":true,"violations":[],"override":true}',
                    '{"approved":false,"violations":["UNKNOWN"]}']:
            with self.subTest(raw=raw):
                service=ChatService(self.db,self.settings)
                async def generate(messages):yield safe_template('HIGH')
                async def bad(ai,messages):return raw
                with patch.object(service.ai,'stream',generate),patch.object(AiClient,'complete_async',bad):events=self.collect(service)
                self.assertEqual(events[-1]['type'],'error')

    def test_review_receives_provenanced_memory_but_resources_still_require_retrieval(self):
        from app.agents.harness import MindBridgeAgentHarness
        from app.core.enums import MessageRole
        harness=MindBridgeAgentHarness(self.db,self.settings)
        harness.save_message(self.user,self.session,MessageRole.USER,'项目代号是星桥63。')
        service=ChatService(self.db,self.settings);captured=[]
        async def generate(messages):yield '你当前的项目代号是星桥63。'
        async def judge(ai,messages):
            captured.append(json.loads(messages[-1].content))
            return '{"approved":true,"violations":[]}'
        with patch.object(service.ai,'stream',generate),patch.object(AiClient,'complete_async',judge):
            events=self.collect(service,'我的项目代号是什么？')
        self.assertEqual(events[-1]['type'],'done')
        self.assertEqual(captured[-1]['sessionFacts']['项目代号']['value'],'星桥63')
        self.assertTrue(captured[-1]['conversationHistory'])
        self.assertNotIn('星桥63',captured[-1]['retrievedReferences'])

    def test_uncertain_protective_wording_requires_model_evidence_from_the_candidate(self):
        candidate='请把自己挪到旁边有人陪伴的地方，并马上联系可信任的朋友。'
        from app.services.response_review import ReplyReviewPipeline
        outcome=SimpleNamespace(risk_level='HIGH',plan_accepted=True,budget_exhausted=False,
                                model_input='我现在有危险。',response_messages=[AiMessage(role='user',content='先确保安全并联系朋友。')],
                                retrieved_knowledge=[])
        class Fixed:
            async def stream(self,messages):yield candidate
        class Judge:
            def __init__(self,evidence):self.evidence=evidence
            async def complete_async(self,messages):
                payload=json.loads(messages[-1].content)
                self.asserted=payload['crisisSafetyUncertain']
                return json.dumps({'approved':True,'violations':[],'safetyActionEvidence':self.evidence},ensure_ascii=False)
        settings=self.settings.model_copy(update={'reply_max_revisions':0})
        judge=Judge('把自己挪到旁边有人陪伴的地方')
        reply=asyncio.run(ReplyReviewPipeline(settings,Fixed(),judge).run(outcome))
        self.assertTrue(judge.asserted);self.assertTrue(reply.accepted)
        judge=Judge('先确保安全并联系朋友。')
        reply=asyncio.run(ReplyReviewPipeline(settings,Fixed(),judge).run(outcome))
        self.assertFalse(reply.accepted)
        self.assertEqual(reply.status,'REVIEW_UNAVAILABLE_OR_INVALID')
