from __future__ import annotations

import json
import logging
import threading
import time
import uuid
import asyncio
from collections import deque
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import RiskLevel, ToolJobKind, ToolJobStatus, ToolStatus
from app.models.entities import AgentRunTrace, DeadLetterRecord, ExcelRecord, PsychologicalReport, ToolAuditRecord, ToolJob
from app.services.tool_governance import ToolAuthorizationError, ToolGovernanceService, ToolPolicyRegistry
from app.services.tool_execution import ToolExecutionService
from app.services.observability import bind_ids, correlation, log_event, observation_scope, read_correlation, span


logger = logging.getLogger(__name__)


class ToolQueueService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def enqueue_report(self, report_id: int, risk_level: str | None = None, *, commit: bool = True) -> list[ToolJob]:
        report = self.db.query(PsychologicalReport).filter_by(id=report_id).with_for_update().one_or_none()
        if report is None:
            raise RuntimeError(f"report {report_id} not found")
        # Use the persisted assessment, not a caller-provided risk label.
        risk_level = report.risk_level
        excel_job = self._find_or_create(ToolJobKind.EXCEL_REPORT.value, report_id)
        jobs = [excel_job]
        case_job = None
        if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
            case_job = self._find_or_create(ToolJobKind.CASE_CREATE.value, report_id)
            jobs.append(case_job)
        if risk_level == RiskLevel.HIGH.value:
            alert_job = self._find_or_create(ToolJobKind.ALERT_SEND.value, report_id, case_job.id if case_job else None)
            jobs.append(alert_job)
        if commit:
            self.db.commit()
        return jobs

    def _find_or_create(self, kind: str, report_id: int, depends_on_job_id: int | None = None) -> ToolJob:
        keyed = self.db.query(ToolJob).filter_by(idempotency_key=f"{report_id}:{kind}").first()
        if keyed is not None:
            return keyed
        existing = (
            self.db.query(ToolJob)
            .filter(ToolJob.report_id == report_id, ToolJob.kind == kind)
            .order_by(ToolJob.id.desc())
            .first()
        )
        if existing is not None:
            return existing
        job = ToolJob(
            report_id=report_id,
            kind=kind,
            status=ToolJobStatus.PENDING.value,
            attempts=0,
            max_attempts=self.settings.tool_queue_max_attempts,
            depends_on_job_id=depends_on_job_id,
            run_after=datetime.utcnow(),
            last_error="",
            idempotency_key=f"{report_id}:{kind}",
            correlation_json=json.dumps(correlation()),
        )
        try:
            with self.db.begin_nested():
                self.db.add(job)
                self.db.flush()
        except IntegrityError:
            job = self.db.query(ToolJob).filter_by(idempotency_key=f"{report_id}:{kind}").one()
        return job


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit = max(0, limit_per_minute)
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        if self.limit <= 0:
            return True, 0.0
        now_ts = time.monotonic()
        with self.lock:
            while self.events and now_ts - self.events[0] >= 60.0:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(now_ts)
                return True, 0.0
            retry_after = max(1.0, 60.0 - (now_ts - self.events[0]))
            return False, retry_after


class ToolDependencyError(RuntimeError):
    pass


class ToolQueueWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop_event = threading.Event()
        self.dispatcher: threading.Thread | None = None
        excel_workers = max(1, settings.tool_queue_excel_workers)
        email_workers = max(1, settings.tool_queue_email_workers)
        self.excel_executor = ThreadPoolExecutor(max_workers=excel_workers, thread_name_prefix="mindbridge-excel")
        self.email_executor = ThreadPoolExecutor(max_workers=email_workers, thread_name_prefix="mindbridge-email")
        self.excel_slots = threading.BoundedSemaphore(excel_workers)
        self.email_slots = threading.BoundedSemaphore(email_workers)
        self.email_limiter = RateLimiter(settings.alert_email_rate_limit_per_minute)

    def start(self) -> None:
        if self.dispatcher is not None:
            return
        # Both transports consume the durable outbox. False selects MCP; it no
        # longer disables durability or makes delivery depend on an SSE client.
        self._recover_running_jobs()
        self.dispatcher = threading.Thread(target=self._loop, name="mindbridge-tool-dispatcher", daemon=True)
        self.dispatcher.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.dispatcher is not None:
            self.dispatcher.join(timeout=5)
        self.excel_executor.shutdown(wait=False, cancel_futures=True)
        self.email_executor.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._recover_running_jobs()
                self._dispatch_once()
            except Exception:
                logger.exception("Tool queue dispatch failed")
            self.stop_event.wait(self.settings.tool_queue_poll_interval_seconds)

    def _claim_job(self, job_id: int) -> str | None:
        db = SessionLocal()
        try:
            token = uuid.uuid4().hex
            now = datetime.utcnow()
            updated = db.query(ToolJob).filter(
                ToolJob.id == job_id, ToolJob.status == ToolJobStatus.PENDING.value,
                ToolJob.run_after <= now,
            ).update({
                ToolJob.status: ToolJobStatus.RUNNING.value,
                ToolJob.claim_token: token,
                ToolJob.lease_expires_at: now + timedelta(seconds=max(1.0, self.settings.tool_queue_lease_seconds)),
                ToolJob.updated_at: now,
            }, synchronize_session=False)
            db.commit()
            return token if updated == 1 else None
        finally:
            db.close()

    def _dispatch_once(self) -> None:
        db = SessionLocal()
        try:
            rows = db.query(ToolJob.id, ToolJob.kind).filter(
                ToolJob.status == ToolJobStatus.PENDING.value, ToolJob.run_after <= datetime.utcnow(),
            ).order_by(ToolJob.created_at.asc()).limit(self.settings.tool_queue_batch_size).all()
        finally:
            db.close()
        for job_id, kind in rows:
            excel = kind in {ToolJobKind.EXCEL_REPORT.value, ToolJobKind.CASE_CREATE.value}
            slot = self.excel_slots if excel else self.email_slots
            if not slot.acquire(blocking=False):
                continue
            try:
                token = self._claim_job(job_id)
                if token is None:
                    slot.release()
                    continue
                executor = self.excel_executor if excel else self.email_executor
                future = executor.submit(self._run_job, job_id, token)
                future.add_done_callback(lambda future, slot=slot: slot.release())
            except Exception:
                slot.release()
                raise

    def _owned_job(self, db, job_id, token):
        query = db.query(ToolJob).filter_by(id=job_id, status=ToolJobStatus.RUNNING.value)
        if token is not None:
            query = query.filter_by(claim_token=token)
        return query.with_for_update().populate_existing().one_or_none()

    def _run_job(self, job_id: int, token: str | None = None) -> None:
        token = token or self._claim_job(job_id)
        if token is None:
            return
        db = SessionLocal()
        observation_stack = ExitStack()
        worker_span = None
        try:
            job = self._owned_job(db, job_id, token)
            if job is None:
                return
            observation_stack.enter_context(observation_scope(ids=read_correlation(job.correlation_json), reuse=False))
            bind_ids(job_id=job.id, report_id=job.report_id)
            trace = db.query(AgentRunTrace.id).filter_by(report_id=job.report_id).first()
            if trace:
                bind_ids(trace_id=trace.id)
            worker_span = observation_stack.enter_context(span('tool.worker', tool=job.kind))
            log_event('tool.job.claimed', tool=job.kind, attempt=job.attempts + 1,
                      job_age_ms=max(0., (datetime.utcnow() - job.created_at).total_seconds() * 1000),
                      eligible_wait_ms=max(0., (datetime.utcnow() - job.run_after).total_seconds() * 1000))
            # A lost transport reply must not replay an already committed success.
            succeeded = db.query(ToolAuditRecord.id).filter_by(
                job_id=job.id, tool_name=job.kind, status="SUCCESS", allowed=True,
            ).first()
            if succeeded:
                self._finish_job(db, job)
                return
            if job.lease_expires_at is None or job.lease_expires_at <= datetime.utcnow():
                self._requeue(db, job, "执行前租约已过期，等待重新认领", 1.0)
                return
            report = db.get(PsychologicalReport, job.report_id)
            allowed, _, _ = ToolPolicyRegistry.authorize(job.kind, report)
            if not allowed:
                job.attempts += 1
                db.commit()
                ToolExecutionService(db, self.settings).execute(
                    job.kind, report_id=job.report_id, job_id=job.id, claim_token=token,
                )
                return
            if job.attempts >= job.max_attempts:
                raise ToolDependencyError("重启恢复的任务已达到最大尝试次数")
            if not self._dependency_ready(db, job):
                self._requeue(db, job, self._dependency_wait_reason(job), 1.0)
                return
            if job.kind in {ToolJobKind.RISK_ALERT.value, ToolJobKind.ALERT_SEND.value}:
                permitted, retry_after = self.email_limiter.allow()
                if not permitted:
                    self._requeue(db, job, "邮件预警限流中，稍后重试", retry_after)
                    return
            job.attempts += 1
            db.commit()
            self._execute(db, job)
            job = self._owned_job(db, job_id, token)
            if job is not None:
                self._finish_job(db, job)
        except Exception as exc:
            if worker_span:
                worker_span.set(status='error', error_type=type(exc).__name__)
            db.rollback()
            try:
                self._fail_or_dead_letter(db, job_id, exc, token)
            except Exception:
                logger.exception("Failed to record tool job failure")
        finally:
            observation_stack.close()
            db.close()

    def _finish_job(self, db, job):
        job.status = ToolJobStatus.SUCCESS.value
        job.last_error = ""
        job.claim_token = None
        job.lease_expires_at = None
        job.updated_at = datetime.utcnow()
        db.commit()
        log_event('tool.job.finished', tool=job.kind, status=job.status, attempt=job.attempts)

    def _execute(self, db: Session, job: ToolJob) -> None:
        if self.settings.tool_queue_enabled:
            ToolExecutionService(db, self.settings).execute(
                job.kind, report_id=job.report_id, job_id=job.id, claim_token=job.claim_token,
            )
        else:
            from app.services.mcp_client import MindBridgeMcpToolClient

            # End the read transaction before the child acquires execution locks.
            kind, report_id, job_id, token = job.kind, job.report_id, job.id, job.claim_token
            db.rollback()
            asyncio.run(MindBridgeMcpToolClient(self.settings).execute_job(kind, report_id, job_id, token))

    def _dependency_ready(self, db: Session, job: ToolJob) -> bool:
        if job.kind not in {ToolJobKind.RISK_ALERT.value, ToolJobKind.ALERT_SEND.value}:
            return True
        if job.depends_on_job_id:
            dependency = db.get(ToolJob, job.depends_on_job_id)
            if dependency is None or dependency.report_id != job.report_id:
                raise ToolDependencyError("依赖任务不存在或属于其他报告")
            if dependency.status == ToolJobStatus.DEAD.value:
                raise ToolDependencyError("依赖任务已进入死信，终止下游任务")
            return dependency.status == ToolJobStatus.SUCCESS.value
        if job.kind == ToolJobKind.ALERT_SEND.value:
            from app.models.entities import RiskCase

            if db.query(RiskCase).filter_by(report_id=job.report_id).first():
                return True
            raise ToolDependencyError("预警任务缺少个案创建依赖")
        if db.query(ExcelRecord).filter_by(report_id=job.report_id, status=ToolStatus.SUCCESS.value).first():
            return True
        raise ToolDependencyError("旧预警任务缺少成功的 Excel 台账")

    def _dependency_wait_reason(self, job: ToolJob) -> str:
        return "等待风险个案创建成功后再发送预警" if job.kind == ToolJobKind.ALERT_SEND.value else "等待 Excel 台账写入成功后再发送预警"

    def _requeue(self, db: Session, job: ToolJob, reason: str, delay_seconds: float) -> None:
        job.status = ToolJobStatus.PENDING.value
        job.last_error = reason
        job.run_after = datetime.utcnow() + timedelta(seconds=max(1.0, delay_seconds))
        job.claim_token = None
        job.lease_expires_at = None
        job.updated_at = datetime.utcnow()
        db.commit()
        log_event('tool.job.requeued', tool=job.kind, status=job.status, attempt=job.attempts)

    def _fail_or_dead_letter(self, db: Session, job_id: int, exc: Exception, token: str | None = None) -> None:
        job = self._owned_job(db, job_id, token)
        if job is None:
            return
        message = f"{type(exc).__name__}: {exc}"
        audits = db.query(ToolAuditRecord).filter_by(job_id=job.id, tool_name=job.kind).order_by(ToolAuditRecord.id.desc()).all()
        if any(audit.status == "SUCCESS" and audit.allowed for audit in audits):
            self._finish_job(db, job)
            return
        audit = next((row for row in audits if (
            json.loads(row.payload or "{}").get("claimToken") == job.claim_token
            and json.loads(row.payload or "{}").get("attempts") == job.attempts
        )), None)
        governance = ToolGovernanceService(db)
        if audit is None:
            # A transport failure can happen before the MCP child creates its
            # execution audit. It still needs a durable failed-attempt record.
            audit = governance.start(job.kind, db.get(PsychologicalReport, job.report_id), job=job,
                                     source="queue" if self.settings.tool_queue_enabled else "mcp-dispatch")
        blocked = isinstance(exc, ToolAuthorizationError) or not audit.allowed
        if audit.status == "AUTHORIZED":
            governance.finish(audit, "BLOCKED" if blocked else "FAILED", message)
        job = self._owned_job(db, job_id, token)
        if job is None:
            return
        job.last_error = message
        job.updated_at = datetime.utcnow()
        job.claim_token = None
        job.lease_expires_at = None
        permanent = blocked or isinstance(exc, ToolDependencyError)
        if permanent or job.attempts >= job.max_attempts:
            job.status = ToolJobStatus.DEAD.value
            db.add(DeadLetterRecord(
                job_id=job.id, report_id=job.report_id, kind=job.kind, reason=message,
                payload=json.dumps({"reportId": job.report_id, "kind": job.kind, "attempts": job.attempts}, ensure_ascii=False),
            ))
        else:
            job.status = ToolJobStatus.PENDING.value
            job.run_after = datetime.utcnow() + timedelta(seconds=self.settings.tool_queue_retry_delay_seconds * max(1, job.attempts))
        db.commit()
        log_event('tool.job.failed', tool=job.kind, status=job.status,
                  attempt=job.attempts, error_type=type(exc).__name__)

    def _recover_running_jobs(self) -> None:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            candidates = db.query(ToolJob.id).filter(
                ToolJob.status == ToolJobStatus.RUNNING.value,
                or_(ToolJob.lease_expires_at.is_(None), ToolJob.lease_expires_at <= now),
            ).all()
            db.rollback()
            for (job_id,) in candidates:
                updated = db.query(ToolJob).filter(
                    ToolJob.id == job_id, ToolJob.status == ToolJobStatus.RUNNING.value,
                    or_(ToolJob.lease_expires_at.is_(None), ToolJob.lease_expires_at <= now),
                ).update({
                    ToolJob.status: ToolJobStatus.PENDING.value,
                    ToolJob.last_error: "租约过期后恢复未完成任务",
                    ToolJob.claim_token: None, ToolJob.lease_expires_at: None,
                    ToolJob.run_after: now, ToolJob.updated_at: now,
                }, synchronize_session=False)
                if updated:
                    audits = db.query(ToolAuditRecord).filter_by(job_id=job_id, status="AUTHORIZED").all()
                    for audit in audits:
                        audit.status = "INTERRUPTED"
                        audit.reason = "执行租约过期，任务等待恢复"
                        audit.updated_at = now
                db.commit()
        finally:
            db.close()


_worker: ToolQueueWorker | None = None


def get_tool_queue_worker(settings: Settings) -> ToolQueueWorker:
    global _worker
    if _worker is None or _worker.stop_event.is_set():
        _worker = ToolQueueWorker(settings)
    return _worker
