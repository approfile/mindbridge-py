#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/../.."
export MINDBRIDGE_MODEL_HOME="${MINDBRIDGE_MODEL_HOME:-/root/autodl-tmp/mindbridge-models}"
exec "$MINDBRIDGE_MODEL_HOME/venv/bin/python" deploy/autodl/serve.py
