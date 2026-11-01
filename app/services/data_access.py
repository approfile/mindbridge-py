"""Audited read boundary. Purpose is an enum, never free-form patient text."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import re

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import current_user
from app.models.entities import DataAccessAudit, UserAccount

PURPOSES = {'case_followup', 'incident_review', 'data_subject_request'}
DETAIL_ROUTES = {'/api/admin/reports/{report_id}', '/api/admin/agent-traces/{trace_id}',
                 '/api/admin/conversations/{session_id}', '/api/admin/cases/{case_id}/notes', '/api/admin/cases/{case_id}'}
RESOURCES = {'reports', 'agent-traces', 'conversations', 'cases', 'excel-records',
             'alerts', 'tool-jobs', 'dead-letters', 'tool-audits', 'data-access-audits',
             'model-usage', 'model-usage-summary'}


def audit_target(request):
    if request.url.path=='/api/reports/me':return 'my-reports',''
    parts = request.url.path.split('/')
    resource = parts[3] if len(parts)>3 and parts[3] in RESOURCES else 'admin-data'
    values = list(request.path_params.values())
    value = str(values[0]) if values else ''
    if value and (not value.isdecimal() or len(value)>9):
        value = 'sha256:' + hashlib.sha256(value.encode()).hexdigest()[:32]
    return resource, value


def write_audit(db, request, actor_id, status, *, purpose='', action='READ'):
    resource, value = audit_target(request)
    # Own transaction: a later failed query/rollback cannot erase the attempt.
    with Session(bind=db.get_bind()) as audit_db:
        from app.services.observability import correlation
        row = DataAccessAudit(actor_id=actor_id, request_id=correlation().get('request_id') or '',
                              action=action, resource=resource, resource_id=value,
                              purpose=purpose if purpose in PURPOSES else '', status=status)
        audit_db.add(row); audit_db.commit(); audit_db.refresh(row)
        return row.id


@dataclass
class ReadAccess:
    sensitive: bool = False
    rows: int = 0


def exposed(access, value):
    access.rows=len(value) if isinstance(value,list) else 1
    return value


def admin_read(request: Request, user: UserAccount = Depends(current_user), db: Session = Depends(get_db)):
    purpose = request.headers.get('X-Access-Purpose', '')
    requested = request.query_params.get('includeSensitive', 'false').lower()
    permitted = 'ROLE_ADMIN' in user.roles
    message = '此操作需要管理员权限。'; status = 403
    if requested not in {'true','false'}:
        permitted = False; status = 400; message = 'includeSensitive只能为true或false。'
    sensitive = requested == 'true'
    if permitted and sensitive:
        route = getattr(request.scope.get('route'), 'path', '')
        if route not in DETAIL_ROUTES:
            permitted=False; status=400; message='列表不提供批量敏感正文，请按单条记录读取。'
        elif 'ROLE_SENSITIVE_READER' not in user.roles:
            permitted=False; message='读取正文需要独立的敏感数据读取权限。'
        elif purpose not in PURPOSES:
            permitted=False; status=400; message='请提供有效的X-Access-Purpose访问用途。'
    if request.url.path=='/api/admin/data-access-audits' and 'ROLE_PRIVACY_AUDITOR' not in user.roles:
        permitted=False; message='此操作需要隐私审计权限。'
    audit_id=write_audit(db,request,user.id,'STARTED' if permitted else 'DENIED',purpose=purpose)
    if not permitted:raise HTTPException(status,message)
    grant=ReadAccess(sensitive=sensitive)
    outcome='FAILED'
    try:
        yield grant
        outcome='ALLOWED'
    except HTTPException as exc:
        outcome='NOT_FOUND' if exc.status_code==404 else 'DENIED'
        raise
    finally:
        # Failure to save the audit is an error, never a silent success.
        with Session(bind=db.get_bind()) as audit_db:
            row=audit_db.get(DataAccessAudit,audit_id);row.status=outcome;row.row_count=grant.rows
            audit_db.commit()
