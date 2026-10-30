"""Optional per-trial counters. No token/currency estimates are fabricated."""
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT = ContextVar("mindbridge_model_usage", default=None)


def record_model_call(provider: str, kind: str) -> None:
    stats = _CURRENT.get()
    if stats is not None:
        stats["logicalCalls"] += 1
        stats["apiAttempts"] += int(provider in {"openai", "ollama"})
        key = f"{provider}:{kind}"
        stats["byKind"][key] = stats["byKind"].get(key, 0) + 1


@contextmanager
def measure_model_calls():
    stats = {"logicalCalls": 0, "apiAttempts": 0, "byKind": {}}
    token = _CURRENT.set(stats)
    try:
        yield stats
    finally:
        _CURRENT.reset(token)
