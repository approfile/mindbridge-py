import unittest
import json

from app.core.enums import RiskLevel
from app.services.assessment import PsychologicalAssessmentService
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer
from app.core.privacy_crypto import decrypt_text, PREFIX


class ExplodingAi:
    def complete(self, messages):
        raise AssertionError("high risk hard guard should not call the model")


class PrivacyAndAssessmentTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app.core.config import Settings
        from app.core.privacy_crypto import cipher
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        settings=Settings(_env_file=None,privacy_key_file=str(Path(temp.name)/'fixture.key'))
        crypto=patch('app.core.privacy_crypto.get_settings',return_value=settings)
        crypto.start();self.addCleanup(crypto.stop);cipher(settings)

    def test_privacy_sanitizer_masks_common_identifiers(self):
        text = PrivacySanitizer().sanitize("电话 13800138000 邮箱 a@example.com 身份证 110101199003071234")

        self.assertNotIn("13800138000", text)
        self.assertNotIn("a@example.com", text)
        self.assertNotIn("110101199003071234", text)
        self.assertEqual(text.count("[已脱敏]"), 3)

    def test_redis_memory_serializes_sanitized_content(self):
        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        store.privacy = PrivacySanitizer()

        payload = json.loads(store._serialize("user", "电话 13800138000 邮箱 a@example.com"))
        self.assertTrue(payload['content'].startswith(PREFIX))
        content=decrypt_text(payload['content'])
        self.assertNotIn("13800138000", content)
        self.assertNotIn("a@example.com", content)
        self.assertEqual(content.count("[已脱敏]"), 2)

    def test_high_risk_signal_uses_hard_guard_before_model(self):
        result = PsychologicalAssessmentService(ExplodingAi()).assess("我不想活了，想结束生命")

        self.assertEqual(result.risk, RiskLevel.HIGH)
        self.assertIsNone(result.confidence)
        self.assertEqual(result.confidence_info['kind'], 'not_applicable')


if __name__ == "__main__":
    unittest.main()
