"""Download exact model revisions, create a private service key, record provenance."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


MODELS = {'chat': 'Qwen/Qwen3-8B', 'embedding': 'Qwen/Qwen3-Embedding-0.6B'}


def main():
    os.umask(0o077)
    root = Path(os.environ.get('MINDBRIDGE_MODEL_HOME', '/root/autodl-tmp/mindbridge-models')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    import torch
    from huggingface_hub import HfApi, snapshot_download
    if not torch.cuda.is_available():
        raise SystemExit('PyTorch cannot access the GPU. Check driver/CUDA compatibility before continuing.')
    (torch.ones(1, device='cuda') + 1).cpu()
    torch.cuda.synchronize()
    gpu = torch.cuda.get_device_properties(0)
    if gpu.total_memory < 30 * 1024**3:
        raise SystemExit('This initial BF16 dual-service preset requires one GPU with at least 30 GiB VRAM (5090 32GB).')
    # The first successful setup locks revisions. Re-running never follows main.
    lock_path = root / 'models.lock.json'
    manifest = json.loads(lock_path.read_text()) if lock_path.exists() else {'models': {}}
    api = HfApi()
    for role, model in MODELS.items():
        entry = manifest['models'].get(role)
        revision = entry['revision'] if entry else api.model_info(model).sha
        path = snapshot_download(model, revision=revision, cache_dir=root / 'hf',
                                 ignore_patterns=['*.gguf', '*.onnx', '*.bin'])
        manifest['models'][role] = {'id': model, 'revision': revision, 'path': path}
    manifest.update(gpu=gpu.name, vramBytes=gpu.total_memory, torch=torch.__version__, cuda=torch.version.cuda)
    import vllm
    manifest['vllm'] = vllm.__version__
    lock_path.write_text(json.dumps(manifest, indent=2) + '\n')
    key = root / 'service.key'
    if not key.exists():
        with key.open('x') as file:
            file.write(secrets.token_urlsafe(32) + '\n')
    os.chmod(key, 0o600)
    with (root / 'environment.lock.txt').open('w') as output:
        subprocess.run([sys.executable, '-m', 'pip', 'freeze'], stdout=output, check=True)
    print('Model revisions and Python dependencies recorded. Service key saved privately; no key printed.')


if __name__ == '__main__':
    main()
