"""One-command demo of the MindBridge multi-agent runtime.

Runs three representative turns (normal chat / consult / high risk) against the
real claim-based runtime using the deterministic mock model, a temporary SQLite
database and in-memory short-term memory. No external service, API key, Redis or
Chroma instance is required.

    python -m scripts.demo_turn
    python -m scripts.demo_turn --message "我最近压力很大，睡不着"
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TEMP_DIR = Path(tempfile.mkdtemp(prefix="mindbridge-demo-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEMP_DIR / 'demo.sqlite3').as_posix()}"
os.environ["AI_PROVIDER"] = "mock"
os.environ["AGENT_FRAMEWORK"] = "event_driven_multi_agent"
os.environ["KNOWLEDGE_VECTOR_ENABLED"] = "false"
os.environ["KNOWLEDGE_VECTOR_REQUIRED"] = "false"
os.environ["TOOL_QUEUE_ENABLED"] = "false"
os.environ["ALERT_EMAIL_DELIVERY_MODE"] = "log"
os.environ["EXCEL_PATH"] = (_TEMP_DIR / "ledger.xlsx").as_posix()


class InMemoryMemory:
    """Short-term memory stub so the demo runs without Redis."""

    _store: dict[str, list] = {}

    def __init__(self, settings):
        self.settings = settings

    def load_recent(self, session_public_id):
        return list(self._store.get(session_public_id, []))[-self.settings.redis_memory_max_messages:]

    def messages_from_rows(self, rows):
        from app.schemas.dtos import AiMessage

        return [AiMessage(role=row.role.lower(), content=row.content) for row in rows]

    def append(self, session_public_id, role, content):
        from app.schemas.dtos import AiMessage

        values = self._store.setdefault(session_public_id, [])
        values.append(AiMessage(role=role.lower(), content=content))
        del values[:-self.settings.redis_memory_max_messages]

    def replace(self, session_public_id, messages):
        self._store[session_public_id] = list(messages)[-self.settings.redis_memory_max_messages:]


DEFAULT_TURNS = [
    ("普通聊天 / CHAT", "帮我解释一下 Python 字典推导式怎么写。"),
    ("心理倾诉 / CONSULT", "我最近压力很大，连续几天失眠，白天也很焦虑。"),
    ("高风险 / RISK", "我不想活了，觉得真的撑不下去了。"),
]


def install_stubs() -> None:
    import app.agents.event_driven_runtime as runtime_module
    import app.agents.harness as harness_module
    import app.services.memory as memory_module

    harness_module.RedisShortTermMemoryStore = InMemoryMemory
    memory_module.RedisShortTermMemoryStore = InMemoryMemory
    runtime_module.RedisShortTermMemoryStore = InMemoryMemory


def prepare_environment() -> None:
    from app.core.bootstrap import create_schema, seed_data
    from app.core.config import get_settings
    from app.core.database import SessionLocal

    get_settings.cache_clear()
    create_schema()
    db = SessionLocal()
    try:
        seed_data(db)
    finally:
        db.close()


def run_turn(message: str) -> None:
    from app.agents.harness import MindBridgeAgentHarness
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.models.entities import ChatSession, UserAccount
    from app.schemas.dtos import ChatRequest

    settings = get_settings()
    db = SessionLocal()
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=message[:36])
        db.add(session)
        db.commit()
        db.refresh(session)

        outcome = MindBridgeAgentHarness(db, settings).run(
            user,
            ChatRequest(message=message, sessionId=session.public_id),
        )
        print_collaboration(outcome)
    finally:
        db.close()


def print_collaboration(outcome) -> None:
    print(f"  脱敏后输入   : {outcome.model_input}")
    print(f"  意图 / 风险  : {outcome.intent.value} / {outcome.risk_level or 'LOW (不生成报告)'}")
    if outcome.assessment is not None:
        assessment = outcome.assessment
        print(
            "  情绪判定     : "
            f"{assessment.emotion.value} score={assessment.emotion_score} "
            f"confidence={assessment.confidence}"
        )
    print(f"  检索知识片段 : {len(outcome.retrieved_knowledge)}")
    print(f"  心理报告 ID  : {outcome.report_id}")
    print()

    print("  协作事件流")
    for index, step in enumerate(outcome.agent_steps, start=1):
        detail = step.observation if len(step.observation) <= 96 else f"{step.observation[:93]}..."
        print(f"    {index:>2}. {step.agent:<18} {step.action:<18} {detail}")

    print()
    print("  最终采纳的回复（prompt 方案中的学生可见部分）")
    preview = outcome.response_messages[-1].content if outcome.response_messages else "(空)"
    preview = " ".join(preview.split())
    print(f"    {preview[:180]}{'...' if len(preview) > 180 else ''}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MindBridge demo turns end to end.")
    parser.add_argument("--message", action="append", help="Custom message to run. Can be repeated.")
    args = parser.parse_args(argv)

    install_stubs()
    prepare_environment()

    turns = [("自定义输入", message) for message in args.message] if args.message else DEFAULT_TURNS
    print("=" * 100)
    print("MindBridge 多 Agent runtime 演示（mock 模型 / SQLite / 内存记忆，无需外部服务）")
    print("=" * 100)
    for index, (label, message) in enumerate(turns, start=1):
        print()
        print(f"[场景 {index}] {label}")
        print(f"  学生输入     : {message}")
        run_turn(message)
        print("-" * 100)
    print()
    print("完整协作快照（事件 / 任务 / 产物）可通过 GET /api/admin/agent-traces 查看。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
