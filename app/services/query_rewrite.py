"""Observable query rewriting; fallback is a declared degraded result."""
from dataclasses import asdict, dataclass

import httpx

from app.services.observability import record_degradation, span


@dataclass(frozen=True)
class QueryRewriteResult:
    query: str
    source: str
    degraded: bool
    reasonCode: str
    errorType: str | None
    inputChars: int
    outputChars: int
    truncated: bool
    limitChars: int = 1024
    selectionStrategy: str = 'full_text'

    def metadata(self):
        return {k: v for k, v in asdict(self).items() if k != 'query'}


def rewrite_query(client, messages, original: str, limit=1024):
    if limit < 8:
        raise ValueError('query budget must allow both input ends')
    reason = 'REWRITE_OK'; error_type = None; source = 'model'
    with span('query.rewrite') as observation:
        try:
            output = client.complete(messages)
            if not isinstance(output, str):
                reason = 'REWRITE_INVALID_OUTPUT'
                raise ValueError('non-string query output')
            output = output.strip()
            if not output:
                reason = 'REWRITE_EMPTY_OUTPUT'
            elif len(output) > limit:
                reason = 'REWRITE_OUTPUT_TOO_LONG'
        except Exception as exc:
            output = ''
            error_type = type(exc).__name__
            if reason == 'REWRITE_OK':
                reason = 'REWRITE_TIMEOUT' if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else 'REWRITE_CALL_FAILED'
        degraded = reason != 'REWRITE_OK'
        if degraded:
            output = original; source = 'original_input'
            observation.set(status='degraded', reason_code=reason, error_type=error_type)
            record_degradation('query.rewrite', reason, error_type=error_type)
        truncated = len(output) > limit
        strategy = 'full_text'
        if truncated:
            # Only over-budget original inputs are windowed; never silently
            # take the first 60 chars and lose the final request/qualification.
            marker = ' … '
            head = (limit - len(marker)) // 2
            tail = limit - len(marker) - head
            output = output[:head] + marker + output[-tail:]
            strategy = 'head_tail_window'
        observation.set(input_chars=len(original), output_chars=len(output), truncated=truncated)
        return QueryRewriteResult(output, source, degraded, reason, error_type,
                                  len(original), len(output), truncated, limit, strategy)
