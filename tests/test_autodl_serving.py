"""CPU-only checks for the Linux GPU-service supervisor's environment and I/O."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
from threading import Thread
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'autodl_serve', Path(__file__).resolve().parents[1] / 'deploy/autodl/serve.py')
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


@contextmanager
def http_server(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class AutoDLServingTests(unittest.TestCase):
    def test_native_sampling_default_preserves_explicit_opt_in(self):
        with patch.dict(os.environ, {}, clear=True):
            env = serve.service_environment('synthetic-service-key')
            self.assertEqual(env['VLLM_USE_FLASHINFER_SAMPLER'], '0')
            self.assertEqual(env['VLLM_API_KEY'], 'synthetic-service-key')
            self.assertEqual(env['CUDA_VISIBLE_DEVICES'], '0')
            self.assertEqual(env['HF_HUB_OFFLINE'], '1')
            os.environ['VLLM_USE_FLASHINFER_SAMPLER'] = '1'
            self.assertEqual(serve.service_environment('other')['VLLM_USE_FLASHINFER_SAMPLER'], '1')

    def test_readiness_auth_goes_to_loopback_despite_download_proxy(self):
        direct, proxied = [], []

        class ModelHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                direct.append((self.path, self.headers.get('Authorization')))
                self.send_response(200 if direct[-1][1] == 'Bearer synthetic-test-token' else 401)
                self.end_headers()

            def log_message(self, *args):
                pass

        class ProxyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                proxied.append((self.path, self.headers.get('Authorization')))
                self.send_response(502)
                self.end_headers()

            def log_message(self, *args):
                pass

        with http_server(ModelHandler) as port, http_server(ProxyHandler) as proxy_port:
            proxy = f'http://127.0.0.1:{proxy_port}'
            with patch.dict(os.environ, {'http_proxy': proxy, 'HTTP_PROXY': proxy,
                            'https_proxy': proxy, 'HTTPS_PROXY': proxy,
                            'all_proxy': proxy, 'ALL_PROXY': proxy,
                            'no_proxy': '', 'NO_PROXY': ''}):
                serve.wait_ready(Mock(poll=Mock(return_value=None)), port,
                                 'synthetic-test-token', timeout=3)
        self.assertEqual(direct, [('/v1/models', 'Bearer synthetic-test-token')])
        self.assertEqual(proxied, [])

    def test_exited_model_fails_without_waiting_for_deadline(self):
        with self.assertRaisesRegex(RuntimeError, 'Model process exited'):
            serve.wait_ready(Mock(poll=Mock(return_value=1)), 1, 'synthetic', timeout=30)


if __name__ == '__main__':
    unittest.main()
