from app.core.bootstrap import create_schema
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.tool_execution import ToolExecutionService
from app.services.observability import configure_logging

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请先安装 requirements.txt 中的 mcp 依赖") from exc


mcp = FastMCP("mindbridge-python-tools")


def _execute(name: str, **arguments) -> str:
    create_schema()
    db = SessionLocal()
    try:
        return ToolExecutionService(db, get_settings()).execute(name, source="mcp-stdio", **arguments)
    finally:
        db.close()


@mcp.tool()
def mindbridge_excel_report(report_id: int, job_id: int | None = None, claim_token: str | None = None) -> str:
    """Write a report ledger row after policy authorization; repeated writes are idempotent."""
    return _execute("EXCEL_REPORT", report_id=report_id, job_id=job_id, claim_token=claim_token)


@mcp.tool()
def mindbridge_case_create(report_id: int, job_id: int | None = None, claim_token: str | None = None) -> str:
    """Create or reuse a case only for a medium/high-risk report."""
    return _execute("CASE_CREATE", report_id=report_id, job_id=job_id, claim_token=claim_token)


@mcp.tool()
def mindbridge_alert_send(
    case_id: int | None = None, report_id: int | None = None,
    job_id: int | None = None, claim_token: str | None = None,
) -> str:
    """Send a high-risk case alert. Delivery failures are MCP errors, never success text."""
    return _execute("ALERT_SEND", report_id=report_id, case_id=case_id, job_id=job_id, claim_token=claim_token)


@mcp.tool()
def mindbridge_alert_ack(case_id: int, actor: str, note: str = "") -> str:
    """Acknowledge a case as a known administrator over trusted local stdio."""
    return _execute("CASE_ACK", case_id=case_id, actor=actor, note=note)


@mcp.tool()
def mindbridge_case_note_add(case_id: int, actor: str, note: str) -> str:
    """Append a case note as a known administrator over trusted local stdio."""
    return _execute("CASE_NOTE_ADD", case_id=case_id, actor=actor, note=note)


@mcp.tool()
def mindbridge_alert_notify(report_id: int, job_id: int | None = None, claim_token: str | None = None) -> str:
    """Legacy report notification, subject to the same high-risk policy and audit boundary."""
    return _execute("RISK_ALERT", report_id=report_id, job_id=job_id, claim_token=claim_token)


if __name__ == "__main__":
    # MCP child logs go to stderr for the process collector. Never let short-
    # lived children race the parent's single-process rotating file handler.
    configure_logging(get_settings().model_copy(update={'observability_log_file': ''}))
    mcp.run()
