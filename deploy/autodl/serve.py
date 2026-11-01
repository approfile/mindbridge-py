"""Foreground supervisor: loopback-only services, one GPU, shared VRAM budget.

Run inside tmux. Ctrl-C/TERM terminates only the child process groups created by
this supervisor. Service tokens are environment variables, never CLI arguments.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request


def command(role, model):
    common = [str(Path(sys.executable).with_name('vllm')), 'serve',
              '--model', model['path'], '--served-model-name', model['id'], '--host', '127.0.0.1',
              '--dtype', 'bfloat16', '--max-num-seqs', '4', '--no-enable-log-requests', '--no-enable-log-outputs']
    if role == 'embedding':
        return common + ['--port', '8001', '--runner', 'pooling', '--max-model-len', '2048',
                         '--max-num-batched-tokens', '2048', '--gpu-memory-utilization', '0.12', '--enforce-eager']
    return common + ['--port', '8000', '--max-model-len', '8192', '--max-num-batched-tokens', '8192',
                     '--gpu-memory-utilization', '0.80', '--enable-prefix-caching',
                     '--enable-prompt-tokens-details',
                     '--default-chat-template-kwargs', '{"enable_thinking":false}']


def service_environment(key):
    # AutoDL images can expose a CUDA toolkit incompatible with FlashInfer's
    # sampling JIT on Blackwell. Keep GPU inference, use native sampling first.
    return {**os.environ, 'VLLM_API_KEY': key, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
            'VLLM_NO_USAGE_STATS': '1', 'DO_NOT_TRACK': '1', 'CUDA_VISIBLE_DEVICES': '0',
            'VLLM_USE_FLASHINFER_SAMPLER': os.environ.get('VLLM_USE_FLASHINFER_SAMPLER', '0')}


def wait_ready(process, port, key, timeout=900):
    # Readiness is loopback-only: never send the private Bearer token to an
    # HTTP proxy inherited from the platform's model-download accelerator.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Model process exited; inspect its private log.')
        try:
            request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/models',
                                             headers={'Authorization': f'Bearer {key}'})
            with opener.open(request, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError('Model startup timed out; inspect its private log.')


def main():
    os.umask(0o077)
    root = Path(os.environ.get('MINDBRIDGE_MODEL_HOME', '/root/autodl-tmp/mindbridge-models')).resolve()
    manifest = json.loads((root / 'models.lock.json').read_text())
    key = (root / 'service.key').read_text().strip()
    if len(key) < 24:
        raise SystemExit('Missing/invalid private service key. Run install.sh first.')
    lock = (root / 'serve.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('The model supervisor is already running.')
    env = service_environment(key)
    children, logs = [], []

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        # Start the small service first, leaving 80% of total VRAM for chat.
        for role, port in [('embedding', 8001), ('chat', 8000)]:
            log = (root / f'{role}.log').open('a')
            logs.append(log)
            process = subprocess.Popen(command(role, manifest['models'][role]), env=env,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            children.append(process)
            print(f'Starting {role}; log: {root / (role + ".log")}', flush=True)
            wait_ready(process, port, key)
            print(f'{role} ready on loopback port {port}.', flush=True)
        print('Both services ready. Keep this session running; Ctrl-C stops both.', flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A model service exited; stopping both services.')
            time.sleep(2)
    except KeyboardInterrupt:
        print('Stopping model services.', flush=True)
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for log in logs:
            log.close()


if __name__ == '__main__':
    main()
