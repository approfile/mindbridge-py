#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/../.."
command -v nvidia-smi >/dev/null || { echo 'NVIDIA driver is required.'; exit 1; }
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
# Keep large model downloads, environments and logs on the AutoDL data disk.
export MINDBRIDGE_MODEL_HOME="${MINDBRIDGE_MODEL_HOME:-/root/autodl-tmp/mindbridge-models}"
mkdir -p "$MINDBRIDGE_MODEL_HOME"
python3 - <<'PY'
import os, pathlib, shutil, subprocess, sys
if sys.version_info < (3, 11):
    raise SystemExit('Use a Python 3.11/3.12 AutoDL image.')
root = pathlib.Path(os.environ['MINDBRIDGE_MODEL_HOME'])
if shutil.disk_usage(root).free < 60 * 1024**3:
    raise SystemExit('At least 60 GiB free space is required before installation; recommend a 150GB data disk.')
line = subprocess.check_output(['nvidia-smi','--query-gpu=memory.total,driver_version','--format=csv,noheader,nounits'], text=True).splitlines()[0]
memory, driver = [part.strip() for part in line.split(',')]
if int(memory) < 30 * 1024:
    raise SystemExit('This preset requires a 32GB GPU. Do not choose a 24GB variant.')
if int(driver.split('.')[0]) < 580:
    raise SystemExit('The pinned CUDA 13 vLLM wheel needs a newer host driver; choose a compatible instance.')
print('Preflight: Python, disk, GPU VRAM and driver major version passed.')
PY
python3 -m venv "$MINDBRIDGE_MODEL_HOME/venv"
"$MINDBRIDGE_MODEL_HOME/venv/bin/python" -m pip install --upgrade pip
"$MINDBRIDGE_MODEL_HOME/venv/bin/python" -m pip install -r deploy/autodl/requirements.txt
"$MINDBRIDGE_MODEL_HOME/venv/bin/python" deploy/autodl/prepare.py
echo 'Prepared. Run: bash deploy/autodl/start.sh'
