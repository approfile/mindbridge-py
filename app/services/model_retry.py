"""Bounded transport retries for buffered reply generation and body review.

No retries for invalid model JSON, safety rejection, billing/auth errors or
cancelled turns. Callers own the total deadline and must discard partial bodies.
"""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time

import httpx

from app.services.observability import log_event
from app.services.turn_budget import checkpoint, current_turn
from app.services.model_gateway import retry_group


def retry_delay(error, attempt, settings):
    delay = settings.model_retry_base_delay_seconds * (2 ** (attempt - 1))
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code not in {408, 429, 500, 502, 503, 504}:
            return None
        value = error.response.headers.get('Retry-After')
        if value:
            try:
                wait = float(value)
            except ValueError:
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    wait = (date - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    wait = 0
            # Never shorten a server-requested wait merely to fit our budget.
            delay = max(delay, wait)
    elif not isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return None
    return delay


async def retry_transport(operation, settings, *, deadline, stage, events):
    with retry_group():
        return await _retry_transport(operation, settings, deadline=deadline, stage=stage, events=events)


async def _retry_transport(operation, settings, *, deadline, stage, events):
    for attempt in range(1, settings.model_transport_max_attempts + 1):
        checkpoint()
        try:
            result = await operation(attempt)
            checkpoint()
            return result
        except Exception as exc:
            delay = retry_delay(exc, attempt, settings)
            if delay is None:
                raise
            remaining = deadline - time.monotonic()
            control = current_turn()
            if control:
                checkpoint()
                remaining = min(remaining, control.remaining())
            status = ('exhausted' if attempt == settings.model_transport_max_attempts else
                      'budget_insufficient' if delay + settings.model_retry_min_remaining_seconds >= remaining else 'scheduled')
            event = {'kind': 'model_retry', 'stage': stage, 'attempt': attempt,
                     'status': status, 'errorType': type(exc).__name__,
                     'httpStatus': exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                     'delayMs': round(delay * 1000, 3) if status == 'scheduled' else None}
            events.append(event)
            log_event('model.transport_retry', stage=stage, attempt=attempt, status=status,
                      error_type=type(exc).__name__, http_status=event['httpStatus'])
            if status != 'scheduled':
                raise
            await asyncio.sleep(delay)
            checkpoint()
