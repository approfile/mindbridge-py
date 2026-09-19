"""Route-level regression tests for the admin API surface.

`ReportService.agent_run_traces` / `tool_audits` / `conversation` used to be
declared at module level by mistake, so every admin request that used them
raised `AttributeError` and returned HTTP 500. The engineering harness never
covered those three routes, so the breakage stayed invisible. These tests call
the real routes through `TestClient` to keep that from happening again.

The suite is self-contained: it provisions its own database directory under the
system temp directory and forces the provider to `mock`, so it does not depend
on any environment variable the caller happens to have set.
"""

import base64
import os
import re
import tempfile
import unittest
from pathlib import Path

# Provision a guaranteed-writable directory. A clean checkout does not contain
# `target/` (it is git-ignored), and SQLite cannot create a database file inside
# a directory that does not exist.
_TEMP_DIR = Path(tempfile.mkdtemp(prefix="mindbridge-admin-api-")).resolve()
(_TEMP_DIR / "data").mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_URL"] = "sqlite:///%s" % (_TEMP_DIR / "admin-api.sqlite3").as_posix()
os.environ["AI_PROVIDER"] = "mock"
os.environ["AGENT_FRAMEWORK"] = "event_driven_multi_agent"
os.environ["KNOWLEDGE_VECTOR_ENABLED"] = "false"
os.environ["KNOWLEDGE_VECTOR_REQUIRED"] = "false"
os.environ["TOOL_QUEUE_ENABLED"] = "false"
os.environ["ALERT_EMAIL_DELIVERY_MODE"] = "log"
os.environ["EXCEL_PATH"] = (_TEMP_DIR / "data" / "ledger.xlsx").as_posix()

from app.core.config import get_settings  # noqa: E402

# Other test modules may have imported the app package first, so the settings
# cache can already hold a different DATABASE_URL. Clear it before the app and
# the database engine are built.
get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.core.bootstrap import create_schema, seed_data  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.main import create_app  # noqa: E402


def basic_auth(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class AdminApiRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        create_schema()
        db = SessionLocal()
        try:
            seed_data(db)
        finally:
            db.close()
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.__enter__()
        cls.student_auth = basic_auth("student", "student123")
        cls.admin_auth = basic_auth("admin", "admin123")

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_admin_trace_audit_and_conversation_routes_do_not_return_500(self):
        chat = self.client.post(
            "/api/chat/stream",
            headers=self.student_auth,
            json={"message": "我最近压力很大，晚上总是睡不着。"},
        )
        self.assertEqual(chat.status_code, 200)
        match = re.search(r'"sessionId":\s*"([0-9a-f]+)"', chat.text)
        self.assertIsNotNone(match, "chat stream did not expose a sessionId: %r" % chat.text[:200])
        session_id = match.group(1)

        traces = self.client.get("/api/admin/agent-traces", headers=self.admin_auth)
        self.assertEqual(traces.status_code, 200)
        self.assertGreaterEqual(len(traces.json()), 1)
        self.assertEqual(traces.json()[0]["intent"], "CONSULT")

        audits = self.client.get("/api/admin/tool-audits", headers=self.admin_auth)
        self.assertEqual(audits.status_code, 200)
        self.assertEqual(audits.json(), [])

        conversation = self.client.get(f"/api/admin/conversations/{session_id}", headers=self.admin_auth)
        self.assertEqual(conversation.status_code, 200)
        self.assertEqual(conversation.json()["sessionId"], session_id)
        self.assertEqual([item["role"] for item in conversation.json()["messages"]][:1], ["USER"])

    def test_unknown_conversation_returns_404_not_500(self):
        response = self.client.get("/api/admin/conversations/does-not-exist", headers=self.admin_auth)

        self.assertEqual(response.status_code, 404)

    def test_admin_routes_still_reject_student_credentials(self):
        for path in ["/api/admin/agent-traces", "/api/admin/tool-audits", "/api/admin/conversations/x"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=self.student_auth).status_code, 403)


if __name__ == "__main__":
    unittest.main()
