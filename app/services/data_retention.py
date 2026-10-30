"""Logical content erasure, preserving risk/outbox metadata and active work.

The CLI defaults to preview. No scheduler or startup deletion is implicit.
"""
from datetime import datetime, timedelta
import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import (AgentRunTrace, ChatMessage, ChatSession, DataAccessAudit,
                                 PsychologicalReport, RiskCase, SessionMemory, ToolJob,
                                 ToolAuditRecord, DeadLetterRecord, CaseNote, AlertRecord, PrivacyErasureMarker)
from app.services.privacy_views import EXPIRED, technical_steps


def protected_session(db, session_id):
    report_ids=db.query(PsychologicalReport.id).filter_by(session_id=session_id)
    return (db.query(RiskCase.id).filter(RiskCase.report_id.in_(report_ids), RiskCase.status!='CLOSED').first() is not None
            or db.query(ToolJob.id).filter(ToolJob.report_id.in_(report_ids),ToolJob.status.in_(['PENDING','RUNNING'])).first() is not None)


def content_expired(db, session, settings=None, now=None):
    settings=settings or get_settings();now=now or datetime.utcnow()
    return (session is not None and session.updated_at < now-timedelta(days=settings.privacy_content_retention_days)
            and not protected_session(db,session.id))


class DataRetentionService:
    def __init__(self, db, settings, *, cache=None):self.db=db;self.settings=settings;self.cache=cache

    def run(self, *, apply=False, now=None, batch_size=100):
        try:return self._run(apply=apply,now=now,batch_size=batch_size)
        except BaseException:self.db.rollback();raise

    def _run(self, *, apply=False, now=None, batch_size=100):
        now=now or datetime.utcnow();db=self.db;counts={'traces':0,'sessions':0,'protectedSessions':0,'accessAudits':0}
        trace_cutoff=now-timedelta(days=self.settings.privacy_trace_retention_days)
        cursor=0
        while True:
            query=db.query(AgentRunTrace).filter(AgentRunTrace.id>cursor,AgentRunTrace.created_at<trace_cutoff).order_by(AgentRunTrace.id).limit(batch_size)
            rows=(query.with_for_update() if apply else query).all()
            if not rows:break
            for trace in rows:
                if db.query(PrivacyErasureMarker.id).filter_by(resource='trace',resource_id=trace.id).first():continue
                counts['traces']+=1
                if apply:
                    trace.original_input=trace.sanitized_input=trace.memory_brief=EXPIRED
                    trace.agent_steps_json=json.dumps(technical_steps(json.loads(trace.agent_steps_json)),ensure_ascii=False)
                    trace.retrieved_knowledge_json=trace.response_messages_json='[]';trace.assessment_json='{}'
                    db.add(PrivacyErasureMarker(resource='trace',resource_id=trace.id))
            cursor=rows[-1].id
        cutoff=now-timedelta(days=self.settings.privacy_content_retention_days);cursor=0
        while True:
            query=db.query(ChatSession).filter(ChatSession.id>cursor,ChatSession.updated_at<cutoff).order_by(ChatSession.id).limit(batch_size)
            rows=(query.with_for_update() if apply else query).all()
            if not rows:break
            for session in rows:
                if protected_session(db,session.id):counts['protectedSessions']+=1;continue
                if db.query(PrivacyErasureMarker.id).filter_by(resource='session',resource_id=session.id).first():continue
                counts['sessions']+=1
                if not apply:continue
                if self.cache is None:
                    from app.services.memory import RedisShortTermMemoryStore
                    self.cache=RedisShortTermMemoryStore(self.settings)
                self.cache.erase_session_cache(session.public_id)
                db.add(PrivacyErasureMarker(resource='session',resource_id=session.id))
                session.title=EXPIRED
                for message in db.query(ChatMessage).filter_by(session_id=session.id):message.content=EXPIRED
                memory=db.query(SessionMemory).filter_by(session_id=session.id).first()
                if memory:memory.state_json='{}';memory.last_message_id=0
                reports=db.query(PsychologicalReport).filter_by(session_id=session.id).all()
                for report in reports:
                    report.content=report.summary=EXPIRED
                    for case in db.query(RiskCase).filter_by(report_id=report.id):
                        case.summary=case.handoff_summary=EXPIRED
                        for note in db.query(CaseNote).filter_by(case_id=case.id):note.note=EXPIRED
                    for job in db.query(ToolJob).filter_by(report_id=report.id):job.last_error=''
                    for audit in db.query(ToolAuditRecord).filter_by(report_id=report.id):audit.reason=EXPIRED;audit.payload='{}'
                    for dead in db.query(DeadLetterRecord).filter_by(report_id=report.id):dead.reason=EXPIRED;dead.payload='{}'
                    for alert in db.query(AlertRecord).filter_by(report_id=report.id):alert.message=EXPIRED;alert.recipient=EXPIRED
            cursor=rows[-1].id
        audit_cutoff=now-timedelta(days=self.settings.privacy_access_audit_retention_days)
        old_audits=db.query(DataAccessAudit).filter(DataAccessAudit.created_at<audit_cutoff)
        counts['accessAudits']=old_audits.count()
        if apply:
            old_audits.delete(synchronize_session=False)
            db.add(DataAccessAudit(action='RETENTION_APPLY',resource='retention',status='ALLOWED',row_count=sum(counts[k] for k in ['traces','sessions','accessAudits'])))
            db.commit()
        else:db.rollback()
        return {'applied':apply,'policyVersion':'sensitive-data-v1','counts':counts,
                'note':'Logical SQL content erasure plus session/agent cache removal; independent exports/backups require their own lifecycle. No active case or pending/running tool content erased.'}
