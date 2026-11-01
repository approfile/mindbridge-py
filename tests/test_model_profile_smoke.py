import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx


spec = importlib.util.spec_from_file_location(
    'profile_smoke', Path(__file__).resolve().parents[1] / 'scripts/smoke_model_profile.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class ModelProfileSmokeAcceptanceTests(unittest.TestCase):
    def run_smoke(self, consult_outcome):
        turns = []
        def respond(request):
            if request.url.path == '/api/model-profile':
                return httpx.Response(200, json={'mode': 'selfhost', 'revision': 1})
            if request.url.path == '/api/agent/status':
                return httpx.Response(200, json={'realModelEnabled': True, 'model': 'synthetic-local'})
            turns.append(request)
            outcome = 'done' if len(turns) == 1 else consult_outcome
            events = [{'type': 'token', 'content': 'synthetic reply'}, {'type': outcome}]
            return httpx.Response(200, text='\n\n'.join('data: ' + json.dumps(e) for e in events))

        original = httpx.Client
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(smoke.httpx, 'Client', lambda **kw: original(
                    transport=httpx.MockTransport(respond), **kw)), \
                 patch.object(smoke, 'Path', lambda path: Path(folder) / path), \
                 patch('sys.argv', ['smoke_model_profile.py', '--expect-mode', 'selfhost']):
                if consult_outcome == 'done':
                    smoke.main()
                else:
                    with self.assertRaisesRegex(RuntimeError, 'At least one turn fell back'):
                        smoke.main()
            files = list(Path(folder).rglob('report.json'))
            self.assertEqual(len(files), 1)
            report = json.loads(files[0].read_text())
        return report

    def test_one_success_and_one_fallback_is_not_accepted(self):
        self.assertFalse(self.run_smoke('fallback')['passed'])

    def test_both_successful_turns_are_accepted(self):
        self.assertTrue(self.run_smoke('done')['passed'])
