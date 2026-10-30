"""Run real offline tests and engineering harness; never substitute import stubs."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='mindbridge-verify-') as folder:
        env = {key: value for key, value in os.environ.items() if not key.startswith('AGENT_MODEL_')}
        env.update(AI_PROVIDER='mock',
                   DATABASE_URL=f'sqlite:///{folder}/verify.sqlite3',
                   REDIS_URL='redis://127.0.0.1:1/15', REDIS_SOCKET_TIMEOUT_SECONDS='0.1',
                   KNOWLEDGE_VECTOR_ENABLED='false', KNOWLEDGE_VECTOR_REQUIRED='false',
                   PRIVACY_KEY_FILE=f'{folder}/privacy.key', ALERT_EMAIL_DELIVERY_MODE='log')
        for args in [['-m', 'unittest', 'discover', '-s', 'tests'], ['-m', 'app.harness.runner']]:
            result = subprocess.run([sys.executable, *args], cwd=root, env=env)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
