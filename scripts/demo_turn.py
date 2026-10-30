"""Compatibility entry point for the real mock routing/risk harness.

This reports runtime assertions, not generated reply quality or live API results.
Use the HTTP/SSE example in README for a visible chat demonstration.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.harness.runner import main

if __name__ == '__main__':
    raise SystemExit(main(['--suite', 'routing', '--suite', 'risk']))
