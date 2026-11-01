import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.entities import UserAccount
from app.services.turn_budget import current_turn, checkpoint
from app.services.observability import span


@dataclass(frozen=True)
class ChatPrincipal:
    id: int
    roles: frozenset[str]


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(password), hashed)


def _credentials(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Basic authorization")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Basic authorization") from exc


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> UserAccount:
    try:
        username, password = _credentials(request)
    except HTTPException:
        _audit_auth_denial(request,db);raise
    control = current_turn()
    if control and control.lease and request.url.path == '/api/chat/stream':
        # FastAPI may cancel its await while the sync auth call is still in
        # flight. This worker owns a separate Session and keeps its lease.
        control.lease.external_started()
        try:
            with span('authentication'):
                bound = db.get_bind()
                engine = bound.engine if hasattr(bound, 'engine') else bound
                with Session(bind=engine) as owned:
                    user = owned.query(UserAccount).filter(UserAccount.username == username).first()
                    if user is None or not verify_password(password, user.password_hash):
                        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Bad credentials')
                    principal = ChatPrincipal(user.id, frozenset(user.roles))
                checkpoint()
                from app.services.model_gateway import bind_user
                bind_user(principal.id)
                return principal
        finally:
            control.lease.external_finished()
    user = db.query(UserAccount).filter(UserAccount.username == username).first()
    if user is None or not verify_password(password, user.password_hash):
        _audit_auth_denial(request,db)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials")
    from app.services.model_gateway import bind_user
    bind_user(user.id)
    return user


def _audit_auth_denial(request,db):
    if request.method=='GET' and request.url.path.startswith('/api/admin/'):
        from app.services.data_access import write_audit
        write_audit(db,request,None,'AUTH_DENIED')


def require_admin(user: Annotated[UserAccount, Depends(current_user)]) -> UserAccount:
    if "ROLE_ADMIN" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user

