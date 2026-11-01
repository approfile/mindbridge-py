from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Query
from fastapi.responses import StreamingResponse
from contextlib import aclosing
from sqlalchemy.orm import Session

from app.agents.factory import agent_framework_status
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_user, require_admin
from app.models.entities import UserAccount
from app.schemas.dtos import KnowledgeIngestRequest, KnowledgeIngestResponse, ChatRequest, authority
from app.services.chat import ChatService
from app.services.knowledge import KnowledgeService
from app.services.model_assets import finetuned_model_status
from app.services.report import ReportService
from app.services.skills import MindBridgeSkillLibrary
from app.services.turn_budget import current_turn
from app.services.turn_execution import TurnRejected
from app.services.observability import log_event
from app.services.data_access import admin_read, ReadAccess, exposed, write_audit
from app.models.entities import DataAccessAudit
from fastapi import Request

router = APIRouter()


@router.get('/api/admin/model-usage-summary')
def model_usage_summary(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)],
                        hours: Annotated[int, Query(ge=1, le=720)] = 24,
                        scope: Literal['online_chat', 'knowledge_index', 'http_model_operation', 'all'] = 'online_chat'):
    from app.services.usage_reporting import usage_report
    return exposed(access, usage_report(db, hours, scope))


@router.get('/api/admin/model-usage')
def model_usage_runs(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)],
                     limit: Annotated[int, Query(ge=1, le=100)] = 20,
                     scope: Literal['online_chat', 'knowledge_index', 'http_model_operation', 'all'] = 'online_chat'):
    from app.services.usage_reporting import list_usage_runs
    return exposed(access, list_usage_runs(db, limit, scope))


@router.get('/api/admin/model-usage/{run_id}')
def model_usage_detail(run_id: str, access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    from app.services.usage_reporting import usage_run_detail
    result = usage_run_detail(db, run_id)
    if result is None:
        raise HTTPException(404, '用量记录不存在。')
    return exposed(access, result)


@router.get("/actuator/health")
def health():
    return {"status": "UP"}


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }


class ChatStreamingResponse(StreamingResponse):
    async def stream_response(self, send):
        # Starlette cancels a send on disconnect/backpressure. Close the
        # suspended producer in its own task/context, rather than relying on
        # async-generator GC, so its owned work always receives finalization.
        async with aclosing(self.body_iterator):
            await super().stream_response(send)


@router.post("/api/chat/stream")
def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    user_id = user.id
    control = current_turn()
    if control and control.lease:
        try:
            control.lease.bind_user(user_id, request.sessionId)
        except TurnRejected as exc:
            log_event('turn.rejected', reason_code=exc.code)
            raise HTTPException(exc.status, '当前会话正在处理或请求较多，请稍后重试。', headers={'Retry-After': '1'})
    service = ChatService(db, get_settings())
    # Auth owns this request Session. No connection/ORM object is held across
    # the streamed response or passed into the dedicated planning worker.
    db.close()
    return ChatStreamingResponse(service.stream_chat(user_id, request), media_type="text/event-stream")


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai" else "mock"
    framework = agent_framework_status(settings)
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": framework,
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "CoordinatorAgent", "status": "READY", "description": "维护任务板、预算、安全门槛、冲突仲裁和最终采纳"},
            {"name": "UnderstandingAgent", "status": "READY", "description": "独立理解用户输入，发布 intent artifact"},
            {"name": "SafetyAgent", "status": "READY", "description": "独立风险评估、SAFETY_OVERRIDE 和候选回复安全审查"},
            {"name": "ContextAgent", "status": "READY", "description": "独立记忆视图、RAG 检索和 skill 上下文聚合"},
            {"name": "ResponseAgent", "status": "READY", "description": "根据黑板 artifact 发布候选回复方案"},
        ],
        "skills": MindBridgeSkillLibrary.status_items(),
        "runtimeHarness": {
            "name": "MindBridgeAgentHarness",
            "status": "READY",
            "description": "统一管理单轮 Agent run 的输入脱敏、上下文注入、风险报告、工具计划和 trace 输出",
        },
        "loop": {
            "type": "event-driven-multi-agent",
            "maxSteps": EventDrivenAgentRuntimeService.max_steps,
            "scheduler": "claim-based-actor-runtime",
        },
        "collaboration": {
            "scheduler": "claim-based",
            "state": "append-only-blackboard",
            "messageBus": "per-agent inbox over shared mailbox",
            "fixedWorkflow": False,
            "agentIsolation": {
                "prompt": "per-agent system prompt",
                "memory": "per-agent private Redis key",
                "model": "per-agent model profile",
                "tools": "per-agent tool permissions",
            },
        },
    }


@router.get("/api/reports/me")
def my_reports(request: Request, user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    audit_id=write_audit(db,request,user.id,'STARTED',action='SELF_READ')
    try:
        rows=ReportService(db).latest_reports(user.id)
        audit=db.get(DataAccessAudit,audit_id);audit.status='ALLOWED';audit.row_count=len(rows);db.commit()
        return rows
    except Exception:
        db.rollback();audit=db.get(DataAccessAudit,audit_id);audit.status='FAILED';db.commit();raise


@router.get("/api/admin/reports")
def admin_reports(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db).latest_reports())


@router.get("/api/admin/excel-records")
def admin_excel(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).excel_records())


@router.get('/api/admin/reports/{report_id}')
def admin_report(report_id: int, access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    try:return exposed(access,ReportService(db,sensitive=access.sensitive).report(report_id))
    except ValueError:raise HTTPException(404,'Report not found') from None


@router.get('/api/admin/cases/{case_id}')
def admin_case(case_id: int, access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    rows=ReportService(db,sensitive=access.sensitive).risk_cases(case_id)
    if not rows:raise HTTPException(404,'Case not found')
    return exposed(access,rows[0])


@router.get('/api/admin/data-access-audits')
def data_access_audits(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    rows=db.query(DataAccessAudit).order_by(DataAccessAudit.id.desc()).limit(100).all()
    return exposed(access,[{'id':row.id,'actorId':row.actor_id,'requestId':row.request_id,'action':row.action,
                            'resource':row.resource,'resourceId':row.resource_id,'purpose':row.purpose,'policy':row.policy,'operatorUid':row.operator_uid,
                            'status':row.status,'rowCount':row.row_count,'createdAt':row.created_at} for row in rows])


@router.get("/api/admin/alerts")
def admin_alerts(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).alert_records())


@router.get("/api/admin/cases")
def admin_cases(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).risk_cases())


@router.get("/api/admin/cases/{case_id}/notes")
def admin_case_notes(case_id: int, access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).case_notes(case_id))


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).tool_jobs())


@router.get("/api/admin/dead-letters")
def admin_dead_letters(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).dead_letters())


@router.get("/api/admin/agent-traces")
def admin_agent_traces(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).agent_run_traces())


@router.get("/api/admin/agent-traces/{trace_id}")
def admin_agent_trace(trace_id: int, access: Annotated[ReadAccess, Depends(admin_read)],
                      db: Annotated[Session, Depends(get_db)]):
    rows = ReportService(db, sensitive=access.sensitive).agent_run_traces(trace_id)
    if not rows:
        raise HTTPException(404, 'Trace not found')
    return exposed(access, rows[0])


@router.get("/api/admin/tool-audits")
def admin_tool_audits(access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    return exposed(access, ReportService(db, sensitive=access.sensitive).tool_audits())


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(session_id: str, access: Annotated[ReadAccess, Depends(admin_read)], db: Annotated[Session, Depends(get_db)]):
    try:
        return exposed(access, ReportService(db, sensitive=access.sensitive).conversation(session_id))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/knowledge")
def ingest_knowledge(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    chunks = KnowledgeService(db, get_settings()).ingest(request.source, request.content)
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        indexed = KnowledgeService(db, get_settings()).rebuild_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"indexedChunks": indexed}


@router.post("/api/admin/knowledge/backup")
def backup_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        snapshot = KnowledgeService(db, get_settings()).backup_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"snapshot": snapshot}


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
):
    chunks = KnowledgeService(db, get_settings()).ingest_file(file.filename or "uploaded-file", await file.read())
    return KnowledgeIngestResponse(source=file.filename or "uploaded-file", chunks=chunks)
