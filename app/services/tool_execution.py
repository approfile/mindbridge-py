"""One authorization/audit boundary for queue and trusted local stdio tools."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import ToolJobKind, ToolJobStatus, ToolStatus
from app.models.entities import AgentRunTrace, PsychologicalReport, RiskCase, ToolJob, UserAccount
from app.services.tool_governance import ToolAuthorizationError, ToolGovernanceService, ToolPolicyRegistry
from app.services.tools import ToolOrchestrationService
from app.services.observability import bind_ids, observation_scope, read_correlation, span


class ToolExecutionError(RuntimeError):
    pass


class ToolExecutionService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def execute(
        self, tool_name: str, *, report_id: int | None = None, case_id: int | None = None,
        actor: str = "", note: str = "", job_id: int | None = None,
        claim_token: str | None = None, source: str = "queue",
    ) -> str:
        job = self.db.get(ToolJob, job_id) if job_id is not None else None
        carrier = read_correlation(job.correlation_json) if job else None
        with observation_scope(ids=carrier, reuse=job is None):
            bind_ids(job_id=job_id, report_id=report_id)
            if report_id is not None:
                trace = self.db.query(AgentRunTrace.id).filter_by(report_id=report_id).first()
                if trace:
                    bind_ids(trace_id=trace.id)
            with span('tool.execute', tool=tool_name):
                return self._execute(tool_name, report_id=report_id, case_id=case_id, actor=actor,
                                     note=note, job_id=job_id, claim_token=claim_token, source=source)

    def _execute(
        self, tool_name: str, *, report_id: int | None = None, case_id: int | None = None,
        actor: str = "", note: str = "", job_id: int | None = None,
        claim_token: str | None = None, source: str = "queue",
    ) -> str:
        case = self.db.get(RiskCase, case_id) if case_id is not None else None
        if report_id is None and case is not None:
            report_id = case.report_id
        report = self.db.get(PsychologicalReport, report_id) if report_id is not None else None
        principal = self.db.query(UserAccount).filter(UserAccount.username == actor).first() if actor else None
        job = self.db.get(ToolJob, job_id) if job_id is not None else None
        governance = ToolGovernanceService(self.db)
        audit = governance.start(tool_name, report, job=job, actor=principal, source=source,
                                 payload={"requestedReportId": report_id, "caseId": case_id})
        audit_id = audit.id
        if not audit.allowed:
            raise ToolAuthorizationError(audit.reason)

        # The initial audit is durable even if execution dies. Side effects in
        # SQL and the terminal audit share a later transaction/parent-row lock.
        self.db.rollback()
        try:
            if self.db.get_bind().dialect.name == "sqlite":
                self.db.execute(text("BEGIN IMMEDIATE"))
            report = self.db.query(PsychologicalReport).filter_by(id=report_id).with_for_update().one_or_none()
            principal = self.db.query(UserAccount).filter_by(username=actor).first() if actor else None
            allowed, reason, _ = ToolPolicyRegistry.authorize(tool_name, report, principal)
            if not allowed:
                raise ToolAuthorizationError(reason)
            if job_id is not None:
                job = self.db.query(ToolJob).filter_by(id=job_id).with_for_update().populate_existing().one_or_none()
                if (job is None or job.report_id != report_id or job.kind != tool_name
                        or job.status != ToolJobStatus.RUNNING.value or not claim_token
                        or job.claim_token != claim_token):
                    raise ToolAuthorizationError("任务未被当前 Worker 有效认领，或工具与任务不匹配")
                if job.lease_expires_at is None or job.lease_expires_at <= datetime.utcnow():
                    raise ToolExecutionError("执行租约已到期，等待重新认领")
            if case_id is not None:
                case = self.db.get(RiskCase, case_id, populate_existing=True)
                if case is None or case.report_id != report_id:
                    raise ToolAuthorizationError("个案与报告不匹配")
            tools = ToolOrchestrationService(self.db, self.settings, autocommit=False)
            with span('tool.side_effect', tool=tool_name):
                success, message = self._invoke(tools, tool_name, report, case, actor, note)
        except Exception as exc:
            self.db.rollback()
            audit = self.db.get(type(audit), audit_id)
            governance.finish(audit, "BLOCKED" if isinstance(exc, ToolAuthorizationError) else "FAILED", str(exc))
            raise

        audit = self.db.get(type(audit), audit_id)
        governance.finish(audit, "SUCCESS" if success else "FAILED", message)
        if not success:
            # Keep failed delivery records and the terminal audit committed.
            raise ToolExecutionError(message)
        return message

    def _invoke(self, tools, name, report, case, actor, note) -> tuple[bool, str]:
        if name == ToolJobKind.EXCEL_REPORT.value:
            record = tools.write_excel(report)
            return record.status == ToolStatus.SUCCESS.value, f"success: {record.file_path}"
        if name == ToolJobKind.CASE_CREATE.value:
            case = tools.create_case(report)
            return True, f"success: caseId={case.id}, reportId={case.report_id}, status={case.status}"
        if name in {ToolJobKind.ALERT_SEND.value, "CASE_ACK", "CASE_NOTE_ADD"}:
            case = case or self.db.query(RiskCase).filter_by(report_id=report.id).first()
            if case is None:
                raise ToolExecutionError("风险个案尚未创建")
            if name == "CASE_ACK":
                case = tools.acknowledge_case(case.id, actor, note)
                return True, f"success: caseId={case.id}, status={case.status}, acknowledgedBy={case.acknowledged_by}"
            if name == "CASE_NOTE_ADD":
                record = tools.add_case_note(case.id, actor, note)
                return True, f"success: noteId={record.id}, caseId={case.id}"
            record = tools.send_case_alert(case)
            success = record.status == ToolStatus.SUCCESS.value
            return success, f"{'success' if success else 'failed'}: caseId={case.id}, {record.message}"
        if name == ToolJobKind.RISK_ALERT.value:
            record = tools.notify(report)
            success = record.status == ToolStatus.SUCCESS.value
            return success, f"{'success' if success else 'failed'}: {record.message}"
        raise ToolAuthorizationError(f"未知工具：{name}")
