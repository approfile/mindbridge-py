"""Offline verification of the fixes described in README section 13.

This is a dependency-light regression check for environments where the full
requirements cannot be installed (no network / no FastAPI). It installs minimal
stub modules for the missing imports and then exercises the *real* code paths:

  1. ReportService admin methods being real class attributes
  2. EventDrivenCoordinator claim write-back (claimed_by / CLAIMED status)
  3. ToolPolicyRegistry risk scoping + ToolGovernanceService audit write
  4. AgentTask.claim() merge semantics
  5. Full 60-case RAG evaluation on the BM25 + local reranker path

For the complete suite use `python -m app.harness.runner` and
`python -m unittest discover -s tests` after installing requirements.txt.

Run: python scripts/verify_offline.py
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Must be set before app.core.database is imported: it builds the engine at
# import time. SQLite keeps the offline verification self-contained, and the
# scratch directory lives inside the repository tree so the run needs no
# elevated filesystem access outside the checkout.
_TEMP_DIR = ROOT / ".verify-tmp"
_TEMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = "sqlite:///%s" % (_TEMP_DIR / "verify.sqlite3").as_posix()
os.environ["AI_PROVIDER"] = "mock"
os.environ["KNOWLEDGE_VECTOR_ENABLED"] = "false"
os.environ["KNOWLEDGE_VECTOR_REQUIRED"] = "false"
os.environ["TOOL_QUEUE_ENABLED"] = "false"
os.environ["ALERT_EMAIL_DELIVERY_MODE"] = "log"
os.environ["EXCEL_PATH"] = (_TEMP_DIR / "ledger.xlsx").as_posix()


def install_stubs() -> None:
    def module(name: str, **attrs) -> types.ModuleType:
        stub = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(stub, key, value)
        sys.modules[name] = stub
        return stub

    class _Redis:
        @staticmethod
        def from_url(*args, **kwargs):
            raise RuntimeError("redis stub: no server in offline verification")

    redis_module = module("redis", Redis=_Redis)
    redis_module.__version__ = "stub"
    module("pypdf", PdfReader=object)

    class _MySqlError(Exception):
        pass

    pymysql_module = module(
        "pymysql",
        paramstyle="pyformat",
        threadsafety=1,
        apilevel="2.0",
        __version__="stub",
        MySQLError=_MySqlError,
        Error=_MySqlError,
        Warning=_MySqlError,
        InterfaceError=_MySqlError,
        DatabaseError=_MySqlError,
        OperationalError=_MySqlError,
        ProgrammingError=_MySqlError,
    )
    pymysql_module.err = module(
        "pymysql.err",
        MySQLError=_MySqlError,
        Warning=_MySqlError,
        Error=_MySqlError,
        InterfaceError=_MySqlError,
        DatabaseError=_MySqlError,
        OperationalError=_MySqlError,
        ProgrammingError=_MySqlError,
    )
    module("chromadb", __version__="stub")
    module("mcp", __version__="stub")
    module("uvicorn", run=lambda *a, **k: None)

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    # FastAPI is only needed so that app.core.security and the route module can
    # be imported; none of the verification paths call a real HTTP endpoint.
    fastapi_module = module(
        "fastapi",
        Depends=lambda dependency=None, **kwargs: dependency,
        HTTPException=_HTTPException,
        Request=type("Request", (), {}),
        UploadFile=type("UploadFile", (), {}),
        File=lambda *a, **k: None,
        APIRouter=type("APIRouter", (), {"__init__": lambda self, *a, **k: None}),
        FastAPI=type("FastAPI", (), {"__init__": lambda self, *a, **k: None}),
        __version__="stub",
    )
    fastapi_module.status = SimpleNamespace(
        HTTP_401_UNAUTHORIZED=401,
        HTTP_403_FORBIDDEN=403,
        HTTP_404_NOT_FOUND=404,
        HTTP_500_INTERNAL_SERVER_ERROR=500,
    )

    responses_module = module(
        "fastapi.responses",
        StreamingResponse=type("StreamingResponse", (), {}),
    )
    staticfiles_module = module(
        "fastapi.staticfiles",
        StaticFiles=type("StaticFiles", (), {"__init__": lambda self, *a, **k: None}),
    )
    fastapi_module.responses = responses_module
    fastapi_module.staticfiles = staticfiles_module

    # memory.py imports redis lazily via import_module, so the stub above is enough.
    assert redis_module is sys.modules["redis"]


PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        print("  [PASS] %s" % label)
    else:
        FAILED.append(label)
        print("  [FAIL] %s %s" % (label, detail))


def verify_report_service() -> None:
    print("\n1) ReportService admin methods are real class attributes")
    from app.services.report import ReportService

    for name in ("agent_run_traces", "tool_audits", "conversation"):
        check(
            "ReportService.%s is a method" % name,
            callable(getattr(ReportService, name, None)),
            "(would raise AttributeError -> HTTP 500)",
        )
    check(
        "ReportService no longer leaks module-level duplicates",
        not any(
            isinstance(value, types.FunctionType) and value.__name__ in {"agent_run_traces", "tool_audits", "conversation"}
            for value in vars(sys.modules["app.services.report"]).values()
        ),
    )


def verify_claim_write_back() -> None:
    print("\n2) EventDrivenCoordinator writes the claim back to the blackboard")
    from app.agents.coordinator import EventDrivenCoordinator
    from app.agents.events import (
        AgentArtifact,
        AgentEventType,
        AgentTask,
        AgentTurnResult,
        CollaborationBlackboard,
        TaskPriority,
        TaskStatus,
    )
    from app.agents.registry import AgentCapability, AgentDecision, AgentProfile, AgentRegistry

    class DemoAgent:
        def __init__(self, name, capability, confidence):
            self.profile = AgentProfile(
                name=name,
                capabilities=frozenset({capability}),
                system_prompt="%s prompt" % name,
                memory_policy="private",
                model_profile=name,
            )
            self.confidence = confidence

        def decide(self, task, board):
            return AgentDecision(True, self.confidence, "%s claims" % self.profile.name)

        def act(self, task, board):
            return AgentTurnResult(
                artifacts=(
                    AgentArtifact(
                        id="%s:artifact" % self.profile.name,
                        owner=self.profile.name,
                        kind="demo",
                        payload={"agent": self.profile.name},
                        task_id=task.id,
                    ),
                )
            )

    settings = SimpleNamespace(
        agent_max_rounds=1,
        agent_max_claims_per_round=2,
        agent_max_claims_per_agent=1,
        agent_final_acceptance_min_confidence=0.6,
    )
    coordinator_agent = SimpleNamespace(
        name="CoordinatorAgent",
        root_task=lambda board: AgentTask(
            id="task:root",
            title="Resolve user turn",
            description=board.user_input,
            priority=TaskPriority.NORMAL,
            metadata={"kind": "root"},
        ),
        remember_acceptance=lambda artifact_id, reason: None,
    )
    registry = AgentRegistry([
        DemoAgent("AgentA", AgentCapability.UNDERSTANDING, 0.8),
        DemoAgent("AgentB", AgentCapability.SAFETY, 0.7),
    ])
    board = CollaborationBlackboard(turn_id="t1", user_input="hello", model_input="hello")

    result = EventDrivenCoordinator(registry, coordinator_agent, settings).run(board)

    claim_events = [event for event in result.events if event.type == AgentEventType.TASK_CLAIMED]
    check("two TASK_CLAIMED events emitted", len(claim_events) == 2, "got %d" % len(claim_events))
    for event in claim_events:
        task = result.tasks[event.task_id]
        check(
            "task %s claimed_by contains %s" % (event.task_id, event.actor),
            event.actor in task.claimed_by,
            "claimed_by=%r" % (task.claimed_by,),
        )
        check(
            "metadata.claimedBy matches for %s" % event.actor,
            event.metadata.get("claimedBy") == [event.actor],
            "metadata=%r" % (event.metadata,),
        )
        check(
            "task %s ends CLOSED" % event.task_id,
            task.status == TaskStatus.CLOSED,
            "status=%s" % task.status,
        )

    check(
        "no task is stuck in CLAIMED",
        all(task.status != TaskStatus.CLAIMED for task in result.tasks.values()),
    )


def verify_tool_governance() -> None:
    print("\n3) ToolPolicyRegistry risk scoping and audit writes")
    from app.core.database import Base
    from app.core.enums import EmotionLabel, IntentType, RiskLevel, ToolJobKind
    from app.services.tool_governance import ToolGovernanceService, ToolPolicyRegistry
    from app.services.tool_queue import ToolQueueService

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        from app.models.entities import ChatSession, PsychologicalReport, ToolAuditRecord, ToolJob, UserAccount

        user = UserAccount(username="student", display_name="Demo", password_hash="x", roles_csv="ROLE_USER")
        db.add(user)
        db.commit()
        session = ChatSession(public_id="s1", user_id=user.id, title="t")
        db.add(session)
        db.commit()

        def make_report(risk):
            report = PsychologicalReport(
                user_id=user.id,
                session_id=session.id,
                content="x",
                intent=IntentType.RISK.value,
                emotion=EmotionLabel.HIGH_RISK.value,
                emotion_score=4.0,
                risk_level=risk,
                confidence=0.9,
                summary="s",
            )
            db.add(report)
            db.commit()
            db.refresh(report)
            return report

        low = make_report(RiskLevel.LOW.value)
        high = make_report(RiskLevel.HIGH.value)

        allowed, reason, policy = ToolPolicyRegistry.authorize(ToolJobKind.ALERT_SEND.value, low)
        check("ALERT_SEND blocked for LOW risk", not allowed, reason)
        check("blocking reason is explicit", "不允许处理风险等级" in reason, reason)
        check("policy scope is HIGH only", policy.allowed_risks == (RiskLevel.HIGH.value,), "%r" % (policy.allowed_risks,))

        allowed_high, _, _ = ToolPolicyRegistry.authorize(ToolJobKind.ALERT_SEND.value, high)
        check("ALERT_SEND allowed for HIGH risk", allowed_high)

        jobs = ToolQueueService(db, SimpleNamespace(tool_queue_max_attempts=3)).enqueue_report(high.id, high.risk_level)
        check("HIGH risk enqueues 3 jobs", len(jobs) == 3, "got %d" % len(jobs))
        alert_job = next(job for job in jobs if job.kind == ToolJobKind.ALERT_SEND.value)
        case_job = next(job for job in jobs if job.kind == ToolJobKind.CASE_CREATE.value)
        check("alert job depends on case creation", alert_job.depends_on_job_id == case_job.id)

        excel_job = next(job for job in jobs if job.kind == ToolJobKind.EXCEL_REPORT.value)
        governance = ToolGovernanceService(db)
        record = governance.start_job(excel_job, high)
        check("start_job writes an audit row", record.id is not None and record.allowed)
        check("audit row is marked AUTHORIZED", record.status == "AUTHORIZED", record.status)
        governance.finish(record, "SUCCESS", "工具执行成功")
        stored = db.query(ToolAuditRecord).filter(ToolAuditRecord.job_id == excel_job.id).all()
        check("audit row persisted", len(stored) == 1, "rows=%d" % len(stored))
        check("audit row finished SUCCESS", stored and stored[0].status == "SUCCESS")
    finally:
        db.close()


def verify_claim_merge() -> None:
    print("\n4) AgentTask.claim() merge semantics")
    from app.agents.events import AgentTask, TaskStatus

    task = AgentTask(id="t", title="T")
    once = task.claim("AgentA")
    twice = once.claim("AgentB")
    check("original is untouched", task.claimed_by == () and task.status == TaskStatus.OPEN)
    check("first claim recorded", once.claimed_by == ("AgentA",) and once.status == TaskStatus.CLAIMED)
    check("second claim appends", twice.claimed_by == ("AgentA", "AgentB"), "%r" % (twice.claimed_by,))
    check("re-claim by same agent is idempotent", once.claim("AgentA").claimed_by == ("AgentA",))


def verify_rag_metrics() -> None:
    """Run the real RAG evaluation over the real knowledge base.

    The vector store is disabled (there is no network for embeddings), so this
    measures the documented fallback path: local BM25 + hybrid_score reranker.
    """
    print("\n5) RAG evaluation (BM25 + local reranker degradation path)")
    import json

    from app.core.bootstrap import seed_data
    from app.core.config import get_settings
    from app.core.database import Base, SessionLocal, engine
    from app.models.entities import KnowledgeChunk
    from app.rag_eval.runner import evaluate_case
    from app.services.knowledge import KnowledgeService

    settings = get_settings()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_data(db)
        chunk_count = db.query(KnowledgeChunk).count()
        check("built-in knowledge base ingested", chunk_count > 0, "chunks=%d" % chunk_count)

        service = KnowledgeService(db, settings)
        check("vector store is disabled for this run", service.vector_store.can_embed is False)

        dataset_path = ROOT / settings.rag_eval_dataset
        cases = json.loads(dataset_path.read_text(encoding="utf-8"))
        results = [evaluate_case(service, case, settings.knowledge_top_k) for case in cases]
        total = max(1, len(results))
        hits = [item for item in results if item["hit"]]
        metrics = {
            "totalCases": len(results),
            "topK": settings.knowledge_top_k,
            "hitRate": len(hits) / total,
            "recallAtK": sum(item["recallAtK"] for item in results) / total,
            "precisionAtK": sum(item["precisionAtK"] for item in results) / total,
            "mrr": sum(item["reciprocalRank"] for item in results) / total,
            "ndcgAtK": sum(item["ndcgAtK"] for item in results) / total,
            "averageFirstRelevantRank": sum(item["firstRelevantRank"] for item in hits) / max(1, len(hits)),
        }
        print("     knowledge chunks : %d" % chunk_count)
        for key in ("totalCases", "topK", "hitRate", "recallAtK", "precisionAtK", "mrr", "ndcgAtK", "averageFirstRelevantRank"):
            print("     %-24s : %s" % (key, round(metrics[key], 4)))

        check("hitRate meets the harness threshold (>= 0.95)", metrics["hitRate"] >= 0.95, metrics["hitRate"])
        check("recallAtK meets the harness threshold (>= 0.95)", metrics["recallAtK"] >= 0.95, metrics["recallAtK"])
        check("mrr meets the harness threshold (>= 0.75)", metrics["mrr"] >= 0.75, metrics["mrr"])
        check("ndcgAtK meets the harness threshold (>= 0.75)", metrics["ndcgAtK"] >= 0.75, metrics["ndcgAtK"])

        misses = [item["id"] for item in results if not item["hit"]]
        if misses:
            print("     cases with no relevant hit (%d): %s" % (len(misses), ", ".join(misses)))
        check(
            "reported misses match the hit rate",
            abs((len(results) - len(misses)) / total - metrics["hitRate"]) < 1e-9,
        )
    finally:
        db.close()


def main() -> int:
    install_stubs()
    print("=" * 78)
    print("Offline verification of MindBridge fixes (README section 13)")
    print("=" * 78)
    verify_report_service()
    verify_claim_write_back()
    verify_tool_governance()
    verify_claim_merge()
    verify_rag_metrics()
    print("\n" + "=" * 78)
    print("passed=%d failed=%d" % (len(PASSED), len(FAILED)))
    for label in FAILED:
        print("  FAILED: %s" % label)
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
