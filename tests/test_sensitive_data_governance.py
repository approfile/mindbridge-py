import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.api.routes import router
from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.core.database import Base, get_db
from app.core.privacy_crypto import PREFIX, PrivacyStorageError, cipher, encrypt_text, decrypt_text
from app.core.privacy_migration import upgrade_sensitive_storage, sensitive_columns
from app.core.security import hash_password
from app.models.entities import (UserAccount, ChatSession, ChatMessage, PsychologicalReport, AgentRunTrace,
                                 DataAccessAudit, ToolAuditRecord, DeadLetterRecord, SessionMemory, RiskCase, ToolJob)
from app.schemas.dtos import ChatRequest
from app.services.data_retention import DataRetentionService
from app.services.memory import RedisShortTermMemoryStore
from app.services.observability import ObservationMiddleware
from app.services.privacy_views import RESTRICTED, ORIGINAL_HIDDEN, EXPIRED
from app.services.report import ReportService
from app.services.tools import ToolOrchestrationService
from test_tool_reliability import ToolRuntimeFixture

SENTINEL='SYNTHETIC_PRIVATE_PATIENT_ALPHA'
PRIVATE=f'{SENTINEL} 电话13800138000 邮箱fixture@example.test 身份证110101199003071234'


class EncryptionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=Settings(_env_file=None,privacy_key_file=str(Path(temp.name)/'private.key'))
        patched=patch('app.core.privacy_crypto.get_settings',return_value=self.settings);patched.start();self.addCleanup(patched.stop)
        cipher(self.settings)

    def test_authenticated_randomized_encryption_and_fake_prefix_is_ordinary_input(self):
        first=encrypt_text(PRIVATE);second=encrypt_text(PRIVATE)
        self.assertNotEqual(first,second);self.assertNotIn(SENTINEL,first)
        self.assertEqual(decrypt_text(first),PRIVATE)
        fake=PREFIX+'caller-controlled';self.assertEqual(decrypt_text(encrypt_text(fake)),fake)
        self.assertEqual(Path(self.settings.privacy_key_file).stat().st_mode & 0o777,0o600)

    def test_tampering_plaintext_wrong_and_missing_keys_fail_closed(self):
        value=encrypt_text(PRIVATE)
        with self.assertRaises(PrivacyStorageError):decrypt_text(value[:-4]+'abcd')
        with self.assertRaises(PrivacyStorageError):decrypt_text(PRIVATE)
        key=Path(self.settings.privacy_key_file);key.unlink()
        with self.assertRaises(PrivacyStorageError):decrypt_text(value)
        with self.assertRaises(PrivacyStorageError):encrypt_text(PRIVATE)
        self.assertFalse(key.exists())
        from cryptography.fernet import Fernet
        key.write_bytes(Fernet.generate_key());key.chmod(0o600)
        with self.assertRaises(PrivacyStorageError):decrypt_text(value)

    def test_insecure_key_permissions_and_symlink_are_rejected(self):
        encrypt_text(PRIVATE);key=Path(self.settings.privacy_key_file);key.chmod(0o644)
        with self.assertRaises(PrivacyStorageError):encrypt_text(PRIVATE)
        key.chmod(0o600);target=key.with_name('target.key');key.rename(target);key.symlink_to(target)
        with self.assertRaises(PrivacyStorageError):encrypt_text(PRIVATE)

    def legacy_database(self):
        engine=create_engine('sqlite://');self.addCleanup(engine.dispose);Base.metadata.create_all(engine)
        with engine.begin() as c:
            c.execute(text("INSERT INTO user_accounts(id,username,display_name,password_hash,roles_csv,created_at) VALUES(1,'fixture','Fixture','unused','ROLE_USER',CURRENT_TIMESTAMP)"))
            c.execute(text("INSERT INTO chat_sessions(id,public_id,title,user_id,created_at,updated_at) VALUES(1,'legacy',:value,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),{'value':PRIVATE})
            c.execute(text("INSERT INTO chat_messages(id,user_id,session_id,role,content,created_at) VALUES(1,1,1,'USER',:value,CURRENT_TIMESTAMP)"),{'value':PRIVATE})
        return engine

    def test_legacy_migration_preserves_plaintext_semantics_and_is_idempotent(self):
        engine=self.legacy_database();first=upgrade_sensitive_storage(engine,self.settings)
        self.assertEqual(first['encryptedValues'],2)
        with engine.connect() as c:
            value=c.execute(text('SELECT content FROM chat_messages')).scalar_one()
            self.assertTrue(value.startswith(PREFIX));self.assertNotIn(SENTINEL,value)
        with Session(engine) as db:self.assertEqual(db.get(ChatMessage,1).content,PRIVATE)
        self.assertTrue(upgrade_sensitive_storage(engine,self.settings)['alreadyComplete'])
        with engine.connect() as c:self.assertEqual(c.execute(text('SELECT content FROM chat_messages')).scalar_one(),value)

    def test_migration_never_replaces_lost_or_wrong_database_key(self):
        engine=self.legacy_database();upgrade_sensitive_storage(engine,self.settings)
        key=Path(self.settings.privacy_key_file);key.unlink()
        with self.assertRaises(PrivacyStorageError):upgrade_sensitive_storage(engine,self.settings)
        self.assertFalse(key.exists())
        from cryptography.fernet import Fernet
        key.write_bytes(Fernet.generate_key());key.chmod(0o600)
        with self.assertRaises(PrivacyStorageError):upgrade_sensitive_storage(engine,self.settings)


class AccessTests(ToolRuntimeFixture):
    def setUp(self):
        super().setUp()
        self.user.password_hash=hash_password('fixture')
        admin=self.db.query(UserAccount).filter_by(username='admin').one();admin.password_hash=hash_password('fixture')
        for name,roles in [('reader',{'ROLE_ADMIN','ROLE_SENSITIVE_READER'}),('auditor',{'ROLE_ADMIN','ROLE_PRIVACY_AUDITOR'})]:
            user=UserAccount(username=name,display_name=name,password_hash=hash_password('fixture'));user.roles=roles;self.db.add(user)
        self.db.commit()
        outcome=MindBridgeAgentHarness(self.db,self.settings).run(self.user,ChatRequest(message=PRIVATE+' 我今晚想伤害自己。',sessionId=self.session.public_id))
        self.report_id=outcome.report_id;self.trace_id=outcome.trace_id
        report=self.db.get(PsychologicalReport,self.report_id);report.summary=PRIVATE
        self.case=ToolOrchestrationService(self.db,self.settings).create_case(report)
        self.db.add(ToolAuditRecord(report_id=report.id,tool_name='EXCEL_REPORT',policy='EXCEL_REPORT',allowed=True,status='SUCCESS',reason=PRIVATE,payload=json.dumps({'private':PRIVATE})))
        self.db.add(DeadLetterRecord(report_id=report.id,kind='EXCEL_REPORT',reason=PRIVATE,payload=PRIVATE));self.db.commit()
        app=FastAPI();app.include_router(router);app.add_middleware(ObservationMiddleware)
        def db():
            with self.sessions() as session:yield session
        app.dependency_overrides[get_db]=db
        settings=patch('app.api.routes.get_settings',return_value=self.settings);settings.start();self.addCleanup(settings.stop)
        self.client=TestClient(app,raise_server_exceptions=False);self.addCleanup(self.client.close)

    def get(self,path,actor='admin',purpose=None):
        headers={'X-Access-Purpose':purpose} if purpose else {}
        return self.client.get(path,auth=(actor,'fixture'),headers=headers)

    def test_default_all_read_paths_hide_body_and_identifiers(self):
        for path in ['/reports','/reports/'+str(self.report_id),'/agent-traces','/agent-traces/'+str(self.trace_id),
                     '/conversations/'+self.session.public_id,'/cases','/cases/'+str(self.case.id),'/cases/'+str(self.case.id)+'/notes',
                     '/tool-jobs','/dead-letters','/tool-audits','/excel-records','/alerts']:
            with self.subTest(path=path):
                response=self.get('/api/admin'+path);self.assertEqual(response.status_code,200,response.text)
                for secret in [SENTINEL,'13800138000','fixture@example.test','110101199003071234']:
                    self.assertNotIn(secret,response.text)
        data=self.get('/api/admin/agent-traces/'+str(self.trace_id)).json()
        self.assertEqual(data['originalInput'],ORIGINAL_HIDDEN);self.assertEqual(data['responseMessages'],[])
        self.assertTrue(data['agentSteps'])

    def test_reader_needs_explicit_detail_request_valid_purpose_and_extra_role(self):
        path='/api/admin/agent-traces/'+str(self.trace_id)+'?includeSensitive=true'
        self.assertEqual(self.get(path,purpose='case_followup').status_code,403)
        self.assertEqual(self.get(path,actor='reader').status_code,400)
        self.assertEqual(self.get(path,actor='reader',purpose=SENTINEL+'13800138000').status_code,400)
        response=self.get(path,actor='reader',purpose='incident_review');self.assertEqual(response.status_code,200,response.text)
        data=response.json();self.assertEqual(data['originalInput'],ORIGINAL_HIDDEN)
        self.assertIn(SENTINEL,data['sanitizedInput']);self.assertNotIn('13800138000',response.text)
        self.assertNotIn('fixture@example.test',response.text);self.assertNotIn('110101199003071234',response.text)

    def test_no_bulk_sensitive_reader_and_unknown_raw_query_cannot_bypass(self):
        for path in ['/api/admin/agent-traces','/api/admin/reports','/api/admin/cases']:
            self.assertEqual(self.get(path+'?includeSensitive=true',actor='reader',purpose='case_followup').status_code,400)
            self.assertNotIn(SENTINEL,self.get(path+'?raw=true',actor='reader').text)

    def test_read_audit_captures_allowed_denied_unknown_and_anonymous_without_body(self):
        self.get('/api/admin/reports')
        self.get('/api/admin/reports',actor='student')
        self.client.get('/api/admin/reports')
        self.get('/api/admin/reports/99999')
        self.get('/api/admin/reports/'+str(self.report_id)+'?includeSensitive=true',actor='reader',purpose='case_followup')
        self.db.expire_all();rows=self.db.query(DataAccessAudit).all()
        self.assertTrue({'ALLOWED','DENIED','AUTH_DENIED','NOT_FOUND'}.issubset({r.status for r in rows}))
        allowed=next(r for r in rows if r.purpose=='case_followup')
        self.assertEqual(allowed.row_count,1);self.assertTrue(allowed.request_id)
        payload=' '.join(str(r.__dict__) for r in rows)
        self.assertNotIn(SENTINEL,payload);self.assertNotIn('13800138000',payload)

    def test_only_auditor_can_query_read_audits_and_audit_storage_failure_is_closed(self):
        self.assertEqual(self.get('/api/admin/data-access-audits').status_code,403)
        response=self.get('/api/admin/data-access-audits',actor='auditor');self.assertEqual(response.status_code,200)
        self.assertNotIn(SENTINEL,response.text)
        with patch('app.services.data_access.write_audit',side_effect=RuntimeError('audit unavailable')):
            response=self.get('/api/admin/reports')
            self.assertEqual(response.status_code,500);self.assertNotIn(SENTINEL,response.text)

    def test_owner_can_only_read_own_sanitized_report(self):
        response=self.get('/api/reports/me',actor='student');self.assertEqual(response.status_code,200)
        self.assertIn(SENTINEL,response.text);self.assertNotIn('13800138000',response.text)
        self.assertEqual({r['id'] for r in response.json()},{self.report_id})
        self.assertEqual(self.get('/api/admin/reports',actor='student').status_code,403)

    def test_sql_free_text_columns_are_ciphertext_and_cannot_be_used_as_lookup_keys(self):
        with self.engine.connect() as c:
            for table,names in sensitive_columns().items():
                for name in names:
                    for value in c.execute(text(f'SELECT {name} FROM {table} WHERE {name} IS NOT NULL')).scalars():
                        self.assertTrue(value.startswith(PREFIX),(table,name));self.assertNotIn(SENTINEL,value)
        with self.assertRaises(PrivacyStorageError):self.db.query(PsychologicalReport).filter(PsychologicalReport.content==PRIVATE).all()

    def test_account_display_name_is_not_in_outbound_model_messages(self):
        self.user.display_name='SYNTHETIC_REAL_ACCOUNT_NAME';self.db.commit()
        for message in ['解释Python字典。','我今晚想伤害自己。']:
            with self.subTest(message=message):
                outcome=MindBridgeAgentHarness(self.db,self.settings).run(self.user,ChatRequest(message=message))
                self.assertTrue(outcome.response_messages)
                text='\n'.join(m.content for m in outcome.response_messages)
                self.assertNotIn('SYNTHETIC_REAL_ACCOUNT_NAME',text)
                self.assertIn('学生显示名：学生',text)

    def test_identifier_redaction_cannot_consume_danger_after_a_name_or_address_label(self):
        for message in ['姓名：张三我今晚想伤害自己。','家庭住址：学校北门我今晚想伤害自己。']:
            with self.subTest(message=message):
                outcome=MindBridgeAgentHarness(self.db,self.settings).run(self.user,ChatRequest(message=message))
                self.assertEqual(outcome.risk_level,'HIGH')
                self.assertEqual(self.db.query(ToolJob).filter_by(report_id=outcome.report_id).count(),3)

    def test_expired_trace_detail_stays_hidden_even_for_reader(self):
        self.db.get(AgentRunTrace,self.trace_id).created_at=datetime.utcnow()-timedelta(days=31);self.db.commit()
        response=self.get('/api/admin/agent-traces/'+str(self.trace_id)+'?includeSensitive=true',actor='reader',purpose='case_followup')
        self.assertEqual(response.status_code,200);self.assertNotIn(SENTINEL,response.text)
        self.assertEqual(response.json()['sanitizedInput'],EXPIRED)

    def test_notification_excludes_patient_body_and_export_is_minimized_private(self):
        from openpyxl import load_workbook
        service=ToolOrchestrationService(self.db,self.settings);report=self.db.get(PsychologicalReport,self.report_id)
        body=service._email_body(report,self.case)
        for value in [SENTINEL,'13800138000','fixture@example.test',self.user.display_name]:self.assertNotIn(value,body)
        service.write_excel(report);path=Path(self.settings.excel_path);self.assertEqual(path.stat().st_mode & 0o777,0o600)
        book=load_workbook(path);text_value=str(list(book.active.values));book.close()
        self.assertNotIn('13800138000',text_value);self.assertNotIn('fixture@example.test',text_value)


class RetentionTests(ToolRuntimeFixture):
    def prepare(self, *, protected=False, pending=False):
        outcome=MindBridgeAgentHarness(self.db,self.settings).run(self.user,ChatRequest(message=PRIVATE+' 最近有点压力。',sessionId=self.session.public_id))
        self.report_id=outcome.report_id;self.trace_id=outcome.trace_id
        self.session.updated_at=datetime.utcnow()-timedelta(days=120)
        self.db.get(AgentRunTrace,self.trace_id).created_at=datetime.utcnow()-timedelta(days=40)
        if not pending:
            for job in self.db.query(ToolJob):job.status='SUCCESS'
        if protected:
            self.db.add(RiskCase(report_id=self.report_id,risk_level='MEDIUM',status='ACKNOWLEDGED',owner='fixture',summary=PRIVATE,handoff_summary=PRIVATE))
        self.db.commit()
        class Cache:
            erased=[]
            def erase_session_cache(self,sid):self.erased.append(sid)
        self.cache=Cache()

    def test_preview_changes_nothing_and_apply_erases_content_but_preserves_metadata(self):
        self.prepare();before=self.db.get(PsychologicalReport,self.report_id).content
        service=DataRetentionService(self.db,self.settings,cache=self.cache)
        result=service.run();self.assertFalse(result['applied']);self.assertEqual(result['counts']['sessions'],1)
        self.assertEqual(self.db.get(PsychologicalReport,self.report_id).content,before);self.assertEqual(self.cache.erased,[])
        result=service.run(apply=True);self.db.expire_all()
        self.assertEqual(result['counts']['sessions'],1);self.assertEqual(self.cache.erased,['test-session'])
        report=self.db.get(PsychologicalReport,self.report_id);self.assertEqual(report.content,EXPIRED);self.assertEqual(report.risk_level,'LOW')
        self.assertEqual(self.db.get(AgentRunTrace,self.trace_id).original_input,EXPIRED)
        self.assertTrue(all(m.content==EXPIRED for m in self.db.query(ChatMessage)))
        self.assertEqual(self.db.query(SessionMemory).one().state_json,'{}')
        self.assertEqual(service.run(apply=True)['counts']['sessions'],0)

    def test_active_cases_and_pending_outbox_are_exempt_from_content_erasure(self):
        for mode in ['case','pending']:
            with self.subTest(mode=mode):
                # Separate fixture state per mode is unnecessary: either
                # protection must independently keep the same session intact.
                if mode=='case':self.prepare(protected=True)
                else:
                    self.db.query(RiskCase).delete()
                    self.db.query(ToolJob).update({ToolJob.status:'PENDING'});self.db.commit()
                result=DataRetentionService(self.db,self.settings,cache=self.cache).run(apply=True)
                self.assertEqual(result['counts']['protectedSessions'],1)
                self.assertIn(SENTINEL,self.db.get(PsychologicalReport,self.report_id).content)
                self.assertEqual(self.cache.erased,[])

    def test_cache_erasure_failure_rolls_back_sql_and_cannot_claim_success(self):
        self.prepare()
        class Broken:
            def erase_session_cache(self,sid):raise RuntimeError('cache unavailable')
        with self.assertRaises(RuntimeError):DataRetentionService(self.db,self.settings,cache=Broken()).run(apply=True)
        self.assertIn(SENTINEL,self.db.get(AgentRunTrace,self.trace_id).original_input)
        self.assertIn(SENTINEL,self.db.get(PsychologicalReport,self.report_id).content)
        self.assertEqual(self.db.query(DataAccessAudit).filter_by(action='RETENTION_APPLY').count(),0)

    def test_user_supplied_tombstone_text_cannot_skip_real_content_erasure(self):
        self.prepare()
        self.session.title=EXPIRED
        self.db.get(AgentRunTrace,self.trace_id).original_input=EXPIRED
        self.db.commit()
        service=DataRetentionService(self.db,self.settings,cache=self.cache)
        result=service.run(apply=True)
        self.assertEqual(result['counts']['sessions'],1);self.assertEqual(result['counts']['traces'],1)
        self.assertEqual(self.db.get(PsychologicalReport,self.report_id).content,EXPIRED)
        self.assertEqual(self.db.get(AgentRunTrace,self.trace_id).memory_brief,EXPIRED)
        again=service.run(apply=True)
        self.assertEqual(again['counts']['sessions'],0);self.assertEqual(again['counts']['traces'],0)

    def test_expired_session_cannot_rehydrate_old_memory_and_audit_retention_is_explicit(self):
        self.prepare()
        from app.services.session_memory import SessionMemoryService
        with self.assertRaises(ValueError):SessionMemoryService(self.db,self.settings).load(self.user,self.session)
        self.db.add(DataAccessAudit(action='READ',resource='reports',status='ALLOWED',created_at=datetime.utcnow()-timedelta(days=366)))
        self.db.commit();service=DataRetentionService(self.db,self.settings,cache=self.cache)
        self.assertEqual(service.run()['counts']['accessAudits'],1);self.assertEqual(self.db.query(DataAccessAudit).count(),1)
        service.run(apply=True);self.assertEqual(self.db.query(DataAccessAudit).filter_by(action='READ').count(),0)


class CacheEncryptionTests(unittest.TestCase):
    setUp=EncryptionTests.setUp
    def test_encrypted_cache_roundtrip_and_legacy_plaintext_is_not_fallback(self):
        store=RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        from app.services.privacy import PrivacySanitizer
        store.privacy=PrivacySanitizer()
        encoded=store._serialize('user',PRIVATE);self.assertNotIn(SENTINEL,encoded)
        decoded=store._decode([encoded]);self.assertIn(SENTINEL,decoded[0].content);self.assertNotIn('13800138000',decoded[0].content)
        self.assertEqual(store._decode([json.dumps({'role':'user','content':PRIVATE})]),[])
