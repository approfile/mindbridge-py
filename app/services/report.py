from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import AlertRecord, AgentRunTrace, CaseNote, ChatMessage, ChatSession, DeadLetterRecord, ExcelRecord, PsychologicalReport, RiskCase, ToolAuditRecord, ToolJob, UserAccount
from app.services.observability import read_correlation
from app.services.confidence import report_confidence_info
from app.schemas.dtos import AgentRunTraceResponse, CaseNoteResponse, ConversationMessageResponse, ConversationResponse, DeadLetterResponse, ReportResponse, RiskCaseResponse, ToolAuditResponse, ToolJobResponse, ToolRecordResponse
from app.services.privacy_views import RESTRICTED, ORIGINAL_HIDDEN, EXPIRED, sanitized_value, technical_steps
from app.services.data_retention import content_expired
from app.core.config import get_settings
from datetime import datetime, timedelta


class ReportService:
    def __init__(self, db: Session, *, sensitive=False):
        self.db = db
        self.sensitive = sensitive

    def latest_reports(self, user_id: int | None = None) -> list[ReportResponse]:
        query = self.db.query(PsychologicalReport).order_by(PsychologicalReport.created_at.desc())
        if user_id is not None:
            query = query.filter(PsychologicalReport.user_id == user_id)
        return [self._report_response(item, owned=user_id is not None) for item in query.limit(100).all()]

    def report(self, report_id):
        row=self.db.get(PsychologicalReport,report_id)
        if row is None:raise ValueError('Report not found')
        return self._report_response(row)

    def excel_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(ExcelRecord).order_by(ExcelRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(id=row.id, reportId=row.report_id, status=row.status, message='台账任务：'+row.status, createdAt=row.created_at, filePath='[文件路径已隐藏]')
            for row in rows
        ]

    def alert_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(AlertRecord).order_by(AlertRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(
                id=row.id,
                reportId=row.report_id,
                status=row.status,
                message='预警任务：'+row.status,
                createdAt=row.created_at,
                channel=row.channel,
                recipient='[收件人已隐藏]',
            )
            for row in rows
        ]

    def risk_cases(self, case_id=None) -> list[RiskCaseResponse]:
        query=self.db.query(RiskCase)
        if case_id is not None:query=query.filter_by(id=case_id)
        rows = query.order_by(RiskCase.updated_at.desc()).limit(100).all()
        return [
            RiskCaseResponse(
                id=row.id,
                reportId=row.report_id,
                riskLevel=row.risk_level,
                status=row.status,
                owner='已分配' if row.owner!='unassigned' else '未分配',
                summary=sanitized_value(row.summary) if self.sensitive and not self._case_expired(row) else RESTRICTED,
                handoffSummary=sanitized_value(row.handoff_summary) if self.sensitive and not self._case_expired(row) else RESTRICTED,
                acknowledgedBy='已确认' if row.acknowledged_by else None,
                acknowledgedAt=row.acknowledged_at,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def case_notes(self, case_id: int) -> list[CaseNoteResponse]:
        case=self.db.get(RiskCase,case_id)
        if case is None:return []
        reveal=self.sensitive and not self._case_expired(case)
        rows = self.db.query(CaseNote).filter(CaseNote.case_id == case_id).order_by(CaseNote.created_at.asc()).all()
        return [
            CaseNoteResponse(id=row.id, caseId=row.case_id, actor='[操作者已隐藏]', note=sanitized_value(row.note) if reveal else RESTRICTED, createdAt=row.created_at)
            for row in rows
        ]

    def tool_jobs(self) -> list[ToolJobResponse]:
        rows = self.db.query(ToolJob).order_by(ToolJob.created_at.desc()).limit(100).all()
        return [
            ToolJobResponse(
                requestId=read_correlation(row.correlation_json).get('request_id'),
                runId=read_correlation(row.correlation_json).get('run_id'),
                id=row.id,
                reportId=row.report_id,
                kind=row.kind,
                status=row.status,
                attempts=row.attempts,
                maxAttempts=row.max_attempts,
                dependsOnJobId=row.depends_on_job_id,
                runAfter=row.run_after,
                lastError='任务异常，详情受权限保护。' if row.last_error else '',
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def dead_letters(self) -> list[DeadLetterResponse]:
        rows = self.db.query(DeadLetterRecord).order_by(DeadLetterRecord.created_at.desc()).limit(100).all()
        return [
            DeadLetterResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                kind=row.kind,
                reason='任务已进入死信，正文已隐藏。',
                payload='{}',
                createdAt=row.created_at,
            )
            for row in rows
        ]

    def agent_run_traces(self, trace_id: int | None = None) -> list[AgentRunTraceResponse]:
        query = self.db.query(AgentRunTrace)
        if trace_id is not None:
            query = query.filter_by(id=trace_id)
        rows = query.order_by(AgentRunTrace.created_at.desc()).limit(100).all()
        responses = []
        for row in rows:
            user = self.db.get(UserAccount, row.user_id)
            session = self.db.get(ChatSession, row.session_id)
            steps = _loads(row.agent_steps_json, [])
            observation = next((step['data'] for step in reversed(steps)
                                if step.get('kind') == 'observability' and isinstance(step.get('data'), dict)), {})
            ids = observation.get('correlation', {})
            expired=row.created_at<datetime.utcnow()-timedelta(days=get_settings().privacy_trace_retention_days)
            reveal=self.sensitive and not expired
            responses.append(
                AgentRunTraceResponse(
                    requestId=ids.get('request_id'),
                    runId=ids.get('run_id'),
                    observability=observation,
                    id=row.id,
                    sessionId=session.public_id if session else "",
                    reportId=row.report_id,
                    username=f'student-{row.user_id}',
                    intent=row.intent,
                    riskLevel=row.risk_level,
                    originalInput=ORIGINAL_HIDDEN,
                    sanitizedInput=sanitized_value(row.sanitized_input) if reveal else EXPIRED if expired else RESTRICTED,
                    memoryBrief=sanitized_value(row.memory_brief) if reveal else EXPIRED if expired else RESTRICTED,
                    agentSteps=sanitized_value(steps) if reveal else technical_steps(steps),
                    retrievedKnowledge=sanitized_value(_loads(row.retrieved_knowledge_json, [])) if reveal else [],
                    responseMessages=sanitized_value(_loads(row.response_messages_json, [])) if reveal else [],
                    assessment=sanitized_value(_loads(row.assessment_json, {})) if reveal else {},
                    createdAt=row.created_at,
                )
            )
        return responses

    def tool_audits(self) -> list[ToolAuditResponse]:
        rows = self.db.query(ToolAuditRecord).order_by(ToolAuditRecord.created_at.desc()).limit(100).all()
        return [
            ToolAuditResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                toolName=row.tool_name,
                policy=row.policy,
                allowed=row.allowed,
                status=row.status,
                reason='工具操作：'+row.status,
                payload={'reportId':row.report_id,'jobId':row.job_id},
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def conversation(self, public_id: str) -> ConversationResponse:
        session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id).first()
        if session is None:
            raise ValueError("Session not found")
        rows = self.db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.asc()).all()
        expired=content_expired(self.db,session)
        return ConversationResponse(
            sessionId=session.public_id,
            title=sanitized_value(session.title) if self.sensitive and not expired else '受控会话档案',
            messages=[ConversationMessageResponse(role=row.role, content=sanitized_value(row.content) if self.sensitive and not expired else EXPIRED if expired else RESTRICTED, createdAt=row.created_at) for row in rows],
        )

    def _report_response(self, report: PsychologicalReport, *, owned=False) -> ReportResponse:
        user = self.db.get(UserAccount, report.user_id)
        session = self.db.get(ChatSession, report.session_id)
        expired=content_expired(self.db,session)
        reveal=(owned or self.sensitive) and not expired
        return ReportResponse(
            id=report.id,
            sessionId=session.public_id if session else "",
            username=sanitized_value(user.username) if owned and user else f'student-{report.user_id}',
            displayName=sanitized_value(user.display_name) if owned and user else f'学生#{report.user_id}',
            content=sanitized_value(report.content) if reveal else EXPIRED if expired else RESTRICTED,
            intent=report.intent,
            emotion=report.emotion,
            emotionScore=report.emotion_score,
            riskLevel=report.risk_level,
            confidence=report.confidence,
            confidenceInfo=report_confidence_info(report),
            summary=sanitized_value(report.summary) if reveal else EXPIRED if expired else RESTRICTED,
            createdAt=report.created_at,
        )

    def _case_expired(self, case):
        report=self.db.get(PsychologicalReport,case.report_id)
        if report is None:return True
        return content_expired(self.db,self.db.get(ChatSession,report.session_id))


def _loads(raw: str, default):
    import json

    try:
        return json.loads(raw or "")
    except Exception:
        return default

