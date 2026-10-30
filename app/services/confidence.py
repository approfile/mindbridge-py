"""Confidence provenance, never a calibrated probability or decision gate."""
from __future__ import annotations

import json
import math

VERSION = 'confidence-semantics-v1'


def parse_model_confidence(data):
    if 'confidence' not in data or data['confidence'] is None:
        return None, 'missing'
    value = data['confidence']
    # A score-shaped string/bool is not a JSON number.
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
        return None, 'invalid'
    return float(value), 'valid'


def confidence_info(value, source, validation='not_applicable'):
    model = source == 'model'
    return {'version': VERSION, 'kind': 'model_self_report' if model else 'not_applicable',
            'value': value if model and validation == 'valid' else None,
            'source': source, 'validation': validation if model else 'not_applicable',
            'calibrationStatus': 'not_calibrated' if model else 'not_applicable',
            'usedForRouting': False, 'usedForPlanAcceptance': False}


def report_confidence_info(report):
    raw = getattr(report, 'confidence_metadata_json', None)
    try:
        data = json.loads(raw) if raw else None
        if isinstance(data, dict) and data.get('version') == VERSION:
            source = data.get('source')
            validation = data.get('validation')
            if source in {'model', 'rule', 'fallback', 'heuristic', 'budget_fallback'}:
                if source != 'model':
                    return confidence_info(None, source)
                value, actual = parse_model_confidence({'confidence': report.confidence})
                if validation == 'valid' and actual == 'valid':
                    return confidence_info(value, source, 'valid')
                if validation in {'missing', 'invalid'}:
                    return confidence_info(None, source, validation)
    except (ValueError, TypeError):
        pass
    # Existing numeric rows carry no reliable provenance; never retrospectively
    # reinterpret a rule constant as a model probability.
    return {'version': VERSION, 'kind': 'legacy_unknown', 'value': None,
            'legacyValue': report.confidence, 'source': 'legacy_unknown',
            'validation': 'unknown', 'calibrationStatus': 'unknown',
            'usedForRouting': False, 'usedForPlanAcceptance': False}


def confidence_summary(report):
    info = report_confidence_info(report)
    if info['kind'] == 'legacy_unknown':
        value = info.get('legacyValue')
        return f'历史参考值{value:g}（来源未标注，不作概率解释）' if isinstance(value, (int, float)) else '历史记录未标注来源'
    if info['kind'] == 'not_applicable':
        return '不适用（规则或兜底判断，无模型自报值）'
    value = info.get('value')
    return f'{value:.2f}（模型自报，未校准）' if value is not None else '未提供有效模型自报值（未校准）'
