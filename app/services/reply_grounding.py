"""Candidate-side coverage and source-bound quotes for a model grounding check.

Quote provenance is deterministic; semantic entailment remains the reviewer's
judgment. This is not a replacement for independent RAG evaluation.
"""
import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class EvidenceCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sentenceId: StrictInt
    kind: Literal['support', 'user_fact', 'retrieved_fact', 'unsupported']
    referenceId: StrictStr | None = None
    sourceQuote: StrictStr | None = None


def candidate_sentences(text):
    parts = [p.strip() for p in re.split(r'(?<=[。！？!?])\s*|(?<=[.])\s+|\n+', text) if p.strip()]
    return [{'id': i + 1, 'text': part} for i, part in enumerate(parts)]


def user_statements(conversation, facts, current):
    statements = [{'id': f'U{i + 1}', 'content': m['content']} for i, m in enumerate(conversation) if m['role'] == 'user']
    if current and not any(m['content'] == current for m in statements):
        statements.append({'id': 'CURRENT', 'content': current})
    for i, fact in enumerate(facts.values()):
        if isinstance(fact, dict) and isinstance(fact.get('value'), str) and type(fact.get('sourceMessageId')) is int:
            statements.append({'id': f'F{i + 1}', 'content': fact['value']})
    return statements


def verify_grounding(checks, sentences, references, statements):
    expected = {s['id'] for s in sentences}
    if checks is None or len(checks) != len(expected) or {c.sentenceId for c in checks} != expected:
        return False, [], 'SENTENCE_COVERAGE_INVALID'
    retrieved = {r['id']: r['content'] for r in references}
    users = {r['id']: r['content'] for r in statements}
    bodies = {s['id']: s['text'] for s in sentences}
    audit = []
    for check in checks:
        if check.kind == 'unsupported':
            return False, audit, 'MODEL_MARKED_UNSUPPORTED'
        if check.kind == 'support':
            if check.referenceId is not None or check.sourceQuote is not None:
                return False, audit, 'SUPPORT_HAS_SOURCE_FIELDS'
            if support_contains_fact(bodies[check.sentenceId]):
                return False, audit, 'FACT_MISCLASSIFIED_AS_SUPPORT'
        else:
            allowed = retrieved if check.kind == 'retrieved_fact' else users
            quote = check.sourceQuote
            if not quote or not quote.strip() or quote not in allowed.get(check.referenceId, ''):
                return False, audit, 'SOURCE_OR_QUOTE_INVALID'
        audit.append({'sentenceId': check.sentenceId, 'kind': check.kind, 'referenceId': check.referenceId,
                      'sourceQuoteSha256': hashlib.sha256(check.sourceQuote.encode()).hexdigest() if check.sourceQuote else None})
    return True, audit, 'VERIFIED'


def support_contains_fact(sentence):
    """Conservative escape-hatch guard, not an entailment or clinical model."""
    return bool(re.search(
        r'大脑|神经|激素|皮质醇|执行功能|生理机制|生物钟|'
        r'\b(?:brain|neural|hormone|cortisol|executive function|circadian)\b|'
        r'\d+\s*(?:分钟|小时|天|周|个月|次|%|minutes?|hours?|days?|weeks?)|'
        r'因为|是由于|原因是|(?:会|有助于|能够|能).{0,12}(?:降低|改善|提高|减少|增加|导致|加重|调节|缓解)|'
        r'\b(?:causes?|because|reduces?|increases?|improves?)\b|'
        r'(?:本校|学校心理中心|校医院|辅导员).{0,20}(?:开放|值班|收费|工作时间|规定)', sentence, re.I))
