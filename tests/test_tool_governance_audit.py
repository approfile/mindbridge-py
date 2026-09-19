"""Regression test for the tool execution policy gate and audit trail.

`ToolPolicyRegistry` / `ToolGovernanceService` existed but were never called by
the queue worker, so `tool_audit_records` stayed permanently empty and
`GET /api/admin/tool-audits` always returned nothing. The worker now authorizes
every job against the risk policy and records the outcome.

The suite is self-contained: it provisions its own database directory and forces
the provider to `mock`, so it does not depend on the caller's environment.

Note on the database: `app/services/tool_queue.py` resolves its sessions with a
module-level `from app.core.database import SessionLocal`, which captures the
object bound at import time. A test therefore must NOT rebind
`app.core.database.SessionLocal` and expect the worker to follow - the worker
keeps its own reference. Keeping everything on one file-backed SQLite database
means every import site already agrees, including the worker's own threads.
"""

import os
import tempfile
import unittest
import uuid
from pathlib import Path

# The worker and the test must talk to the same file. Prefer the system temp
# directory and fall back to a git-ignored directory inside the repository,
# because some locked-down environments deny writes to the system temp dir.
try:
    _TEMP_DIR = Path(tempfile.mkdtemp(prefix="mindbridge-tool-governance-")).resolve()
except OSError:
    _TEMP_DIR = Path(__file__).resolve().parents[1] / ".verify-tmp" / f"tool-governance-{os.getpid()}"
(_TEMP_DIR / "data").mkdir(parents=True, exist_ok=True)

# A clean checkout has no target/ directory (it is git-ignored) and SQLite cannot
# create a database file inside a directory that does not exist.
os.environ["DATABASE_URL"] = "sqlite:///%s" % (_TEMP_DIR / "governance.sqlite3").as_posix()
os.environ["AI_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_VECTOR_ENABLED"] = "false"
os.environ["KNOWLEDGE_VECTOR_REQUIRED"] = "false"
os.environ["TOOL_QUEUE_ENABLED"] = "false"
os.environ["ALERT_EMAIL_DELIVERY_MODE"] = "log"
os.environ["EXCEL_PATH"] = (_TEMP_DIR / "data" / "ledger.xlsx").as_posix()

from app.core.config import get_settings  # noqa: E402

# Other test modules may have imported the app first, so the settings cache can
# already hold a different DATABASE_URL. Clear it before the engine is built.
get_settings.cache_clear()

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.enums import EmotionLabel, IntentType, RiskLevel, ToolJobKind, ToolJobStatus  # noqa: E402
from app.models.entities import ChatSession, PsychologicalReport, ToolAuditRecord, ToolJob, UserAccount  # noqa: E402
from app.services.tool_governance import ToolPolicyRegistry  # noqa: E402
from app.services.tool_queue import ToolQueueService, ToolQueueWorker  # noqa: E402


class ToolGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        get_settings.cache_clear()
        Base.metadata.create_all(bind=engine)

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        user = UserAccount(username="student", display_name="Demo Student", password_hash="x", roles_csv="ROLE_USER")
        self.db.add(user)
        self.db.commit()
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title="governance")
        self.db.add(session)
        self.db.commit()
        self.report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content="我不想活了，想结束生命。",
            intent=IntentType.RISK.value,
            emotion=EmotionLabel.HIGH_RISK.value,
            emotion_score=4.0,
            risk_level=RiskLevel.HIGH.value,
            confidence=0.95,
            summary="governance high risk case",
        )
        self.db.add(self.report)
        self.db.commit()
        self.db.refresh(self.report)

    def tearDown(self):
        self.db.close()

    def test_policy_registry_blocks_jobs_outside_their_risk_scope(self):
        low_report = PsychologicalReport(
            user_id=self.report.user_id,
            session_id=self.report.session_id,
            content="普通聊天",
            intent=IntentType.CHAT.value,
            emotion=EmotionLabel.NORMAL.value,
            emotion_score=0.0,
            risk_level=RiskLevel.LOW.value,
            confidence=0.66,
            summary="low risk",
        )
        self.db.add(low_report)
        self.db.commit()
        alert_job = ToolJob(report_id=low_report.id, kind=ToolJobKind.ALERT_SEND.value, status=ToolJobStatus.PENDING.value)

        allowed, reason, policy = ToolPolicyRegistry.authorize(alert_job.kind, low_report)

        self.assertFalse(allowed)
        self.assertIn("不允许处理风险等级", reason)
        self.assertEqual(policy.allowed_risks, (RiskLevel.HIGH.value,))

    def test_worker_writes_an_audit_record_for_each_executed_job(self):
        jobs = ToolQueueService(self.db, get_settings()).enqueue_report(self.report.id, self.report.risk_level)
        self.assertEqual(len(jobs), 3)
        job = next(item for item in jobs if item.kind == ToolJobKind.EXCEL_REPORT.value)

        worker = ToolQueueWorker(get_settings())
        try:
            # Mark the job RUNNING the way the dispatcher does, then let the
            # worker execute it through its own session.
            prepared = SessionLocal()
            prepared.query(ToolJob).filter(ToolJob.id == job.id).update({ToolJob.status: ToolJobStatus.RUNNING.value})
            prepared.commit()
            prepared.close()

            worker._run_job(job.id)
        finally:
            worker.stop()

        verify = SessionLocal()
        try:
            refreshed = verify.get(ToolJob, job.id)
            self.assertIsNotNone(refreshed, "worker could not see the job; check that both use the same database")
            self.assertEqual(refreshed.status, ToolJobStatus.SUCCESS.value)
            audits = verify.query(ToolAuditRecord).filter(ToolAuditRecord.job_id == job.id).all()
            self.assertEqual(len(audits), 1)
            self.assertTrue(audits[0].allowed)
            self.assertEqual(audits[0].tool_name, ToolJobKind.EXCEL_REPORT.value)
            self.assertEqual(audits[0].status, "SUCCESS")
            self.assertIn("EXCEL_REPORT", audits[0].policy)
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
