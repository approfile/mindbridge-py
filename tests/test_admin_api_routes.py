import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes import router
from app.core.config import Settings
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.models.entities import ChatSession, PsychologicalReport, ToolAuditRecord, UserAccount


class AdminQueryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from app.core.privacy_crypto import cipher
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        privacy_settings=Settings(_env_file=None,privacy_key_file=str(Path(temporary.name)/'fixture.key'))
        crypto=patch('app.core.privacy_crypto.get_settings',return_value=privacy_settings)
        crypto.start();self.addCleanup(crypto.stop);cipher(privacy_settings)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False)
        with self.sessions() as db:
            for username, roles in [
                ("admin", {"ROLE_ADMIN", "ROLE_USER"}),
                ("student", {"ROLE_USER"}),
                ("other", {"ROLE_USER"}),
                ('reader', {'ROLE_ADMIN','ROLE_SENSITIVE_READER'}),
            ]:
                user = UserAccount(
                    username=username,
                    display_name=username.title(),
                    password_hash=hash_password("test-password"),
                )
                user.roles = roles
                db.add(user)
            db.commit()

        def test_db():
            with self.sessions() as db:
                yield db

        settings = Settings(
            _env_file=None,
            ai_provider="mock",
            knowledge_vector_enabled=False,
            tool_queue_enabled=True,
        )
        self.settings_patch = patch("app.api.routes.get_settings", return_value=settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        memory_patch = patch("app.services.memory.RedisShortTermMemoryStore._connect", return_value=None)
        memory_patch.start()
        self.addCleanup(memory_patch.stop)

        # The API uses an isolated database, with no startup worker or external tools.
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = test_db
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.admin_auth = ("admin", "test-password")
        self.student_auth = ("student", "test-password")
        self.turns = [
            self.chat("我最近考试压力很大，连续几天失眠，想找人聊聊。"),
            self.chat("我不想活了，想伤害自己。"),
        ]

    def chat(self, message):
        response = self.client.post(
            "/api/chat/stream", auth=self.student_auth, json={"message": message}
        )
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["type"], "done")
        assistant = "".join(event.get("content") or "" for event in events if event["type"] == "token")
        self.assertTrue(assistant)
        return {"sessionId": events[0]["sessionId"], "input": message, "assistant": assistant}

    def admin_get(self, path):
        response = self.client.get(path, auth=self.admin_auth)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_reports_after_consult_and_high_risk_chat(self):
        reports = self.admin_get("/api/admin/reports")
        self.assertEqual(len(reports), 2)
        self.assertEqual({row["riskLevel"] for row in reports}, {"LOW", "HIGH"})
        self.assertEqual({row["content"] for row in reports}, {'[敏感正文已隐藏]'})
        self.assertEqual({row["sessionId"] for row in reports}, {turn["sessionId"] for turn in self.turns})
        for row in reports:
            self.assertTrue(row['username'].startswith('student-'))
            self.assertTrue(row['displayName'].startswith('学生#'))
            self.assertTrue(row["summary"])
            self.assertTrue(row["createdAt"])

    def test_personal_reports_exclude_other_students(self):
        with self.sessions() as db:
            other = db.query(UserAccount).filter_by(username="other").one()
            session = ChatSession(public_id="other-session", user_id=other.id, title="Other consultation")
            db.add(session)
            db.flush()
            report = PsychologicalReport(
                user_id=other.id, session_id=session.id, content="另一个学生的咨询记录",
                intent="CONSULT", emotion="ANXIETY", emotion_score=2.0,
                risk_level="LOW", confidence=0.72, summary="Other student's report",
            )
            db.add(report)
            db.flush()
            other_report_id = report.id
            db.commit()
        response = self.client.get("/api/reports/me", auth=self.student_auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 2)
        self.assertNotIn(other_report_id, {row["id"] for row in response.json()})
        self.assertEqual(len(self.admin_get("/api/admin/reports")), 3)

    def test_agent_traces_include_saved_run_data(self):
        traces = self.admin_get("/api/admin/agent-traces")
        self.assertEqual(len(traces), 2)
        self.assertEqual({row["intent"] for row in traces}, {"CONSULT", "RISK"})
        for row in traces:
            self.assertTrue(row['username'].startswith('student-'))
            self.assertIsNotNone(row["reportId"])
            self.assertTrue(row["agentSteps"])
            self.assertEqual(row["responseMessages"],[])
            self.assertEqual(row['originalInput'],'[原文不通过接口提供]')
            self.assertIsInstance(row["retrievedKnowledge"], list)
            self.assertIsInstance(row["assessment"], dict)

    def test_exact_trace_lookup_keeps_admin_authorization(self):
        trace = self.admin_get("/api/admin/agent-traces")[0]
        path = "/api/admin/agent-traces/" + str(trace["id"])
        response = self.client.get(path, auth=self.admin_auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), trace)
        self.assertEqual(self.client.get(path, auth=self.student_auth).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/agent-traces/999999", auth=self.admin_auth).status_code, 404)

    def test_tool_audits_parse_saved_payload(self):
        # Seed an audit to test its query/serialization; this does not test execution governance.
        with self.sessions() as db:
            report = db.query(PsychologicalReport).first()
            db.add(ToolAuditRecord(
                report_id=report.id, tool_name="EXCEL_REPORT", policy="EXCEL_REPORT",
                allowed=True, status="AUTHORIZED", reason="query regression fixture",
                payload=json.dumps({"reportId": report.id}),
            ))
            db.commit()
        audits = self.admin_get("/api/admin/tool-audits")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["toolName"], "EXCEL_REPORT")
        self.assertEqual(audits[0]["payload"]["reportId"], audits[0]["reportId"])

    def test_conversation_includes_saved_messages_in_order(self):
        for turn in self.turns:
            with self.subTest(session=turn["sessionId"]):
                response=self.client.get('/api/admin/conversations/'+turn['sessionId']+'?includeSensitive=true',
                                         auth=('reader','test-password'),headers={'X-Access-Purpose':'case_followup'})
                self.assertEqual(response.status_code,200,response.text)
                conversation=response.json()
                self.assertEqual(conversation["sessionId"], turn["sessionId"])
                self.assertEqual(
                    [(row["role"], row["content"]) for row in conversation["messages"]],
                    [("USER", turn["input"]), ("ASSISTANT", turn["assistant"])],
                )

    def test_unknown_conversation_returns_not_found(self):
        response = self.client.get("/api/admin/conversations/missing-session", auth=self.admin_auth)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Session not found")

    def test_admin_queries_require_admin_role(self):
        for path in [
            "/api/admin/reports", "/api/admin/agent-traces", "/api/admin/tool-audits",
            "/api/admin/conversations/" + self.turns[0]["sessionId"],
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(self.client.get(path, auth=self.student_auth).status_code, 403)


if __name__ == "__main__":
    unittest.main()
