"""Metadata-only model accounting. No prompts, vectors, credentials or raw errors.

HTTP attempts are written before I/O when a ledger session factory is bound.
The shared observation collects concurrent workers; ContextVars identify logical
calls and retry groups. Missing usage is unknown, never a synthetic zero.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

_CALL = ContextVar('model_gateway_call', default=None)
_RETRY = ContextVar('model_gateway_retry', default=None)
_STAGE = ContextVar('model_gateway_stage', default=None)
_DOCUMENT = ContextVar('model_gateway_document_embedding', default=False)
TOKEN_KEYS = ('inputTokens', 'outputTokens', 'totalTokens', 'cacheHitTokens', 'cacheMissTokens', 'reasoningTokens')


def _state():
    from app.services.observability import current_observation
    return current_observation()


def safe_name(value):
    return value if isinstance(value, str) and re.fullmatch(r'[\w./:@+-]{1,160}', value, flags=re.ASCII) else None


def _integer(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def normalize_usage(data, provider='openai', operation='chat'):
    result = {key: None for key in TOKEN_KEYS}
    malformed = False
    raw = data.get('usage') if isinstance(data, dict) else None
    if provider == 'ollama':
        raw = data if data.get('done') is True else None
        if raw:
            result.update(inputTokens=_integer(raw.get('prompt_eval_count')), outputTokens=_integer(raw.get('eval_count')))
    elif isinstance(raw, dict):
        result['inputTokens'] = _integer(raw.get('prompt_tokens', raw.get('input_tokens')))
        result['outputTokens'] = 0 if operation == 'embedding' else _integer(raw.get('completion_tokens', raw.get('output_tokens')))
        result['totalTokens'] = _integer(raw.get('total_tokens'))
        details = raw.get('prompt_tokens_details', raw.get('input_tokens_details')) or {}
        details = details if isinstance(details, dict) else {}
        result['cacheHitTokens'] = _integer(raw.get('prompt_cache_hit_tokens', details.get('cached_tokens')))
        result['cacheMissTokens'] = _integer(raw.get('prompt_cache_miss_tokens'))
        output_details = raw.get('completion_tokens_details', raw.get('output_tokens_details')) or {}
        if isinstance(output_details, dict):
            result['reasoningTokens'] = _integer(output_details.get('reasoning_tokens'))
        # Optional absent/null counts are unknown. Explicit malformed counts
        # must not disappear and then be replaced with a derived valid total.
        optional_counts = [raw.get('total_tokens'), raw.get('prompt_cache_hit_tokens'),
                           raw.get('prompt_cache_miss_tokens'), details.get('cached_tokens')]
        if isinstance(output_details, dict):
            optional_counts.append(output_details.get('reasoning_tokens'))
        malformed = any(value is not None and _integer(value) is None for value in optional_counts)
    if not isinstance(raw, dict):
        return {**result, 'usageStatus': 'missing'}
    inp, out = result['inputTokens'], result['outputTokens']
    invalid = inp is None or out is None or malformed
    if inp is not None and out is not None:
        total = inp + out
        invalid |= result['totalTokens'] is not None and result['totalTokens'] != total
        result['totalTokens'] = total
        hit, miss = result['cacheHitTokens'], result['cacheMissTokens']
        invalid |= hit is not None and hit > inp
        invalid |= miss is not None and miss > inp
        invalid |= hit is not None and miss is not None and hit + miss != inp
        if hit is not None and miss is None and hit <= inp:
            result['cacheMissTokens'] = inp - hit
        reasoning = result['reasoningTokens']
        invalid |= reasoning is not None and reasoning > out
    return {**result, 'usageStatus': 'invalid' if invalid else 'reported'}


def estimate_cost(attempt, settings):
    unknown = {'status': 'usage_unknown', 'currency': None, 'amount': None, 'source': 'public_list_price_estimate'}
    if attempt.get('usageStatus') != 'reported':
        return unknown
    local_operation = (settings.embedding_allow_no_key if attempt.get('operation') == 'embedding'
                       else settings.openai_chat_template_no_thinking)
    if attempt['provider'] == 'ollama' or settings.model_runtime_mode == 'selfhost' or local_operation:
        return {**unknown, 'status': 'infrastructure_cost_not_configured'}
    try:
        path = Path(settings.model_pricing_file)
        path = path if path.is_absolute() else settings.project_root / path
        catalog = json.loads(path.read_text(encoding='utf-8'))
        timestamp = datetime.fromisoformat(attempt['startedAt'])
        if not datetime.fromisoformat(catalog['validFrom']) <= timestamp < datetime.fromisoformat(catalog['validUntil']):
            return {**unknown, 'status': 'price_expired_or_not_effective', 'priceVersion': catalog['version']}
        entry = next((entry for entry in catalog['entries'] if entry['host'] == attempt['host']
                      and entry['operation'] == attempt['operation']
                      and attempt.get('actualModel') in entry['models']
                      and attempt['requestedModel'] in entry['models']), None)
        if entry is None:
            return {**unknown, 'status': 'price_not_configured'}
        result = {**unknown, 'currency': entry['currency'], 'priceVersion': catalog['version'], 'priceSource': entry['source']}
        rates = entry.get('rates')
        if entry.get('schedule'):
            utc = timestamp.astimezone(timezone.utc)
            peak_window = utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)
            rates = entry['offPeak']
            result['tier'] = 'off_peak'
            if peak_window:
                date = utc.date().isoformat()
                if date in entry.get('verifiedPeakDatesUtc', []):
                    rates = entry['peak']; result['tier'] = 'peak'
                elif date not in entry.get('verifiedHolidayDatesUtc', []):
                    # Require an explicitly verified calendar before selecting
                    # a weekday peak tier. Absence is not proof of a workday.
                    return {**result, 'status': 'holiday_tier_unverified'}
        if 'input' in rates:
            value = Decimal(attempt['inputTokens']) * Decimal(rates['input'])
        else:
            if attempt['cacheHitTokens'] is None or attempt['cacheMissTokens'] is None:
                return {**result, 'status': 'cache_split_unknown'}
            value = (Decimal(attempt['cacheHitTokens']) * Decimal(rates['inputHit'])
                     + Decimal(attempt['cacheMissTokens']) * Decimal(rates['inputMiss']))
        value += Decimal(attempt['outputTokens']) * Decimal(rates.get('output', '0'))
        if not value.is_finite() or value < 0:
            raise ValueError('invalid price')
        return {**result, 'status': 'priced', 'amount': format(value / Decimal(1_000_000), 'f'), 'ratesPerMillion': rates}
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation, StopIteration):
        return {**unknown, 'status': 'price_catalog_invalid'}


@contextmanager
def retry_group():
    existing = _RETRY.get()
    token = _RETRY.set(existing or uuid.uuid4().hex)
    try:
        yield
    finally:
        _RETRY.reset(token)


@contextmanager
def model_call(client, operation):
    state = _state()
    call_id = _RETRY.get() or uuid.uuid4().hex
    details = {'id': call_id, 'agent': safe_name(getattr(client, 'agent_name', None)) or 'Unassigned',
               'stage': safe_name(_STAGE.get()) or operation, 'operationStage': operation}
    token = _CALL.set(details)
    if state is not None and client.settings.ai_provider.lower() in {'openai', 'ollama'}:
        with state.lock:
            state.usage_calls.setdefault(call_id, details)
    try:
        yield
    finally:
        _CALL.reset(token)


@contextmanager
def stage_scope(stage):
    token = _STAGE.set(stage)
    document = _DOCUMENT.set(True) if stage == 'embedding.document' else None
    try:
        yield
    finally:
        _STAGE.reset(token)
        if document is not None:
            _DOCUMENT.reset(document)


def embedding_stage():
    return 'embedding.document' if _DOCUMENT.get() else 'embedding.query'


def bind_user(user_id):
    state = _state()
    if state is not None:
        state.usage_user_id = user_id


def bind_ledger(state, session_factory, scope='online_chat', user_id=None):
    state.usage_session_factory = session_factory
    state.usage_scope = scope
    if user_id is not None:
        state.usage_user_id = user_id


def _ensure_run(state):
    with state.lock:
        if not state.ids['run_id']:
            state.ids['run_id'] = uuid.uuid4().hex
        return state.ids['run_id']


def _write(state, model, key, values):
    factory = getattr(state, 'usage_session_factory', None)
    if factory is None:
        return
    try:
        with state.usage_db_lock, factory() as db:
            row = db.get(model, key)
            if row is None:
                row = model(id=key, **values)
                db.add(row)
            else:
                for name, value in values.items():
                    setattr(row, name, value)
            db.commit()
    except Exception as exc:
        from app.services.observability import log_event
        with state.lock:
            state.usage_ledger_errors += 1
        log_event('usage.ledger_write_failed', error_type=type(exc).__name__, status='unknown')


class ModelAttempt:
    def __init__(self, settings, *, provider, base_url, model, operation='chat', stage=None):
        self.settings = settings
        self.state = _state()
        call = _CALL.get() or {'id': _RETRY.get() or uuid.uuid4().hex, 'agent': 'EmbeddingService' if operation == 'embedding' else 'Unassigned',
                               'stage': stage or ('embedding.query' if operation == 'embedding' else _STAGE.get() or 'model.complete'),
                               'operationStage': 'embedding' if operation == 'embedding' else 'model.complete'}
        self.row = {'id': uuid.uuid4().hex, 'logicalCallId': call['id'], 'agent': call['agent'],
                    'stage': stage or call['stage'], 'operationStage': call['operationStage'],
                    'provider': safe_name(provider), 'host': urlsplit(base_url).hostname,
                    'requestedModel': safe_name(model), 'actualModel': None, 'providerRequestId': None,
                    'operation': operation, 'startedAt': datetime.now(timezone.utc).isoformat(),
                    'modelProfile': {'id': settings.model_profile_id or 'deployment-default',
                                     'revision': settings.model_profile_revision, 'mode': settings.model_runtime_mode},
                    'status': 'started', 'httpStatus': None, 'errorType': None, 'durationMs': None,
                    'firstTokenMs': None, **{key: None for key in TOKEN_KEYS}, 'usageStatus': 'missing',
                    'cost': {'status': 'pending', 'currency': None, 'amount': None}}
        self.started = time.perf_counter()
        self.finished = False

    def __enter__(self):
        if self.state is not None:
            _ensure_run(self.state)
            with self.state.lock:
                self.state.usage_calls.setdefault(self.row['logicalCallId'], {'id': self.row['logicalCallId']})
                self.row['attemptNumber'] = 1 + sum(row['logicalCallId'] == self.row['logicalCallId'] for row in self.state.usage_attempts)
                self.state.usage_attempts.append(self.row)
            self.persist()
            persist_run(self.state)
        self.started = time.perf_counter()
        return self

    def persist(self):
        if self.state is not None:
            from app.models.entities import ModelUsageAttempt
            _write(self.state, ModelUsageAttempt, self.row['id'],
                   {'run_id': _ensure_run(self.state), 'logical_call_id': self.row['logicalCallId'],
                    'user_id': self.state.usage_user_id, 'payload_json': json.dumps(self.row, allow_nan=False)})

    def response(self, response):
        self.row['httpStatus'] = response.status_code

    def capture(self, data):
        if not isinstance(data, dict):
            return
        self.row['actualModel'] = safe_name(data.get('model')) or self.row['actualModel']
        self.row['providerRequestId'] = safe_name(data.get('id') or data.get('request_id')) or self.row['providerRequestId']
        if isinstance(data.get('usage'), dict) or (self.row['provider'] == 'ollama' and data.get('done') is True):
            # Streaming usage is cumulative. Replace, never sum repeated chunks.
            self.row.update(normalize_usage(data, self.row['provider'], self.row['operation']))

    def first_token(self):
        if self.row['firstTokenMs'] is None:
            self.row['firstTokenMs'] = round((time.perf_counter() - self.started) * 1000, 3)

    def __exit__(self, kind, error, traceback):
        self.row.update(status='completed' if kind is None else 'cancelled' if kind.__name__ in {'CancelledError', 'GeneratorExit'} else 'failed',
                        errorType=kind.__name__ if kind else None,
                        durationMs=round((time.perf_counter() - self.started) * 1000, 3))
        self.row['cost'] = estimate_cost(self.row, self.settings)
        self.finished = True
        self.persist()
        return False


def record_cache(layer, *, hits=0, misses=0, errors=0, bypasses=0, reason, duration_ms=0, unit='requests'):
    state = _state()
    if state is None:
        return
    row = {'id': uuid.uuid4().hex, 'layer': layer, 'hits': hits, 'misses': misses, 'errors': errors,
           'bypasses': bypasses, 'reason': reason, 'durationMs': round(duration_ms, 3), 'unit': unit}
    _ensure_run(state)
    with state.lock:
        state.usage_cache_events.append(row)
    from app.models.entities import UsageCacheEvent
    _write(state, UsageCacheEvent, row['id'], {'run_id': state.ids['run_id'], 'payload_json': json.dumps(row)})


def aggregate_usage(attempts, cache_events, logical_calls=None):
    reported = [row for row in attempts if row.get('usageStatus') == 'reported']
    tokens = {key: sum(row[key] for row in reported if row.get(key) is not None)
              if any(row.get(key) is not None for row in reported) else None for key in TOKEN_KEYS}
    cache_rows = [row for row in reported if row.get('cacheHitTokens') is not None and row.get('cacheMissTokens') is not None]
    input_covered = sum(row['inputTokens'] for row in cache_rows)
    costs = {}
    for row in attempts:
        cost = row.get('cost', {})
        if cost.get('status') == 'priced':
            currency = cost['currency']
            costs[currency] = costs.get(currency, Decimal(0)) + Decimal(cost['amount'])
    layers = {}
    for event in cache_events:
        layer = layers.setdefault(event['layer'], {'hits': 0, 'misses': 0, 'errors': 0, 'bypasses': 0, 'durationMs': 0, 'unit': event['unit']})
        for key in ['hits', 'misses', 'errors', 'bypasses', 'durationMs']:
            layer[key] += event[key]
    for layer in layers.values():
        eligible = layer['hits'] + layer['misses']
        layer['hitRate'] = layer['hits'] / eligible if eligible else None
        layer['durationMs'] = round(layer['durationMs'], 3)
    logical_ids = set(logical_calls or []) | {row['logicalCallId'] for row in attempts}
    priced = sum(row.get('cost', {}).get('status') == 'priced' for row in attempts)
    return {'schemaVersion': 1, 'logicalCalls': len(logical_ids), 'httpAttempts': len(attempts),
            'retryAttempts': sum(max(0, sum(row['logicalCallId'] == key for row in attempts) - 1) for key in {r['logicalCallId'] for r in attempts}),
            'attemptStatuses': {status: sum(row['status'] == status for row in attempts) for status in ['started', 'completed', 'failed', 'cancelled']},
            'tokens': tokens if reported else None, 'reportedUsageAttempts': len(reported),
            'unknownUsageAttempts': len(attempts) - len(reported),
            'usageCoverage': len(reported) / len(attempts) if attempts else None,
            'tokensComplete': bool(attempts) and len(reported) == len(attempts),
            'cacheTokenCoverage': len(cache_rows) / len(attempts) if attempts else None,
            'providerCacheHitRate': sum(row['cacheHitTokens'] for row in cache_rows) / input_covered if input_covered else None,
            'providerCacheInputTokensCovered': input_covered,
            'cost': {'knownAmounts': {key: format(value, 'f') for key, value in costs.items()},
                     'pricedAttempts': priced, 'unknownPriceAttempts': len(attempts) - priced,
                     'complete': bool(attempts) and priced == len(attempts),
                     'semantics': 'Public list-price estimate; currencies are separate; unknown is not zero. Reasoning tokens are included in output, not added again.'},
            'cacheLayers': layers,
            'unimplementedCaches': ['query_embedding', 'retrieval_result', 'final_answer'],
            'durationSemantics': 'HTTP attempt durations can overlap; their sum is not end-to-end latency.'}


def usage_snapshot(state):
    with state.lock:
        attempts = [dict(row) for row in state.usage_attempts]
        events = [dict(row) for row in state.usage_cache_events]
        summary = aggregate_usage(attempts, events, state.usage_calls)
        return {**summary, 'attempts': attempts, 'cacheEvents': events,
                'ledgerWriteErrors': state.usage_ledger_errors,
                'ledgerBound': state.usage_session_factory is not None}


def persist_run(state, *, finalized=False):
    if not state.usage_attempts and not state.usage_cache_events:
        return
    with state.usage_db_lock:
        state.usage_finalized = state.usage_finalized or finalized
        from app.models.entities import ModelUsageRun
        snapshot = usage_snapshot(state)
        snapshot.update(correlation=dict(state.ids), scope=state.usage_scope,
                        modelProfile=getattr(state, 'model_profile', None),
                        durationMs=round(state.elapsed_ms(), 3), streamStatus=state.stream_status,
                        serverFirstTokenMs=state.server_first_token_ms)
        # Attempt/cache rows are authoritative; do not duplicate them into the run blob.
        snapshot.pop('attempts'); snapshot.pop('cacheEvents')
        _write(state, ModelUsageRun, _ensure_run(state),
               {'user_id': state.usage_user_id, 'scope': state.usage_scope, 'finalized': state.usage_finalized,
                'payload_json': json.dumps(snapshot, allow_nan=False), 'updated_at': datetime.now(timezone.utc).replace(tzinfo=None)})
