from __future__ import annotations

import json
import math
from dataclasses import dataclass

from app.core.enums import EmotionLabel, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal
from app.services.risk_signals import analyze_risk
from app.services.confidence import confidence_info, parse_model_confidence
from app.services.observability import record_degradation


@dataclass
class PsychologyAssessment:
    emotion: EmotionLabel
    emotion_score: float
    risk: RiskLevel
    confidence: float | None
    summary: str
    source: str = "model"
    degraded: bool = False
    needs_clarification: bool = False
    reason_codes: tuple[str, ...] = ()
    confidence_validation: str = 'not_applicable'

    @property
    def confidence_info(self):
        return confidence_info(self.confidence, self.source, self.confidence_validation)


class PsychologicalAssessmentService:
    def __init__(self, ai: AiClient):
        self.ai = ai

    @staticmethod
    def rule_fallback(text: str, history=(), previous_risk: str | None = None) -> PsychologyAssessment:
        class Unavailable:
            def complete(self, messages):
                raise RuntimeError("model not allowed in this fallback")

        result = PsychologicalAssessmentService(Unavailable()).assess(text, history, previous_risk)
        result.degraded = True
        result.source = "budget_fallback"
        result.reason_codes = (*result.reason_codes, "planning_not_accepted")
        return result

    def assess(self, text: str, history: list[AiMessage] | None = None, previous_risk: str | None = None) -> PsychologyAssessment:
        signal = analyze_risk(text, history or [], previous_risk)
        if signal.level is not None:
            emotion = EmotionLabel.HIGH_RISK if signal.level == RiskLevel.HIGH else EmotionLabel.ANXIETY if signal.level == RiskLevel.MEDIUM or has_consult_signal(text) else EmotionLabel.NORMAL
            score = {RiskLevel.HIGH: 4.0, RiskLevel.MEDIUM: 3.1, RiskLevel.LOW: 1.0}[signal.level]
            return PsychologyAssessment(emotion, score, signal.level, None,
                                        signal.reason, "rule", False, signal.needs_clarification, (signal.reason,))
        try:
            raw = self.ai.complete(PromptTemplates.psychology_prompt(history or [], text))
            start = raw.find("{")
            end = raw.rfind("}")
            data = json.loads(raw[start:end + 1] if start >= 0 and end > start else raw)
            emotion = EmotionLabel(data.get("emotion", "NORMAL").upper())
            score = float(data.get("emotionScore", score_for_emotion(emotion)))
            risk = RiskLevel(data.get("risk", risk_from_score(score).value).upper())
            if not math.isfinite(score) or not 0 <= score <= 4:
                raise ValueError("invalid assessment range")
            score_risk = risk_from_score(score)
            if risk_order(score_risk) > risk_order(risk):
                risk = score_risk
            if emotion == EmotionLabel.HIGH_RISK:
                risk = RiskLevel.HIGH
            confidence, validation = parse_model_confidence(data)
            reasons = ()
            if validation != 'valid':
                reasons = (f'MODEL_CONFIDENCE_{validation.upper()}',)
                record_degradation('assessment.confidence', reasons[0])
            return PsychologyAssessment(emotion, score, risk, confidence, data.get("summary", "模型评估结果"),
                                        reason_codes=reasons, confidence_validation=validation)
        except Exception:
            result = heuristic(text)
            result.source = "fallback"
            result.degraded = True
            result.reason_codes = ("assessment_unavailable_or_invalid",)
            return result


def heuristic(text: str) -> PsychologyAssessment:
    if has_consult_signal(text):
        if any(word in text.lower() for word in ["抑郁", "低落", "崩溃", "难过", "depress", "hopeless"]):
            return PsychologyAssessment(EmotionLabel.DEPRESSED, 3.1, RiskLevel.MEDIUM, None, "检测到低落或抑郁相关表达", 'heuristic')
        return PsychologyAssessment(EmotionLabel.ANXIETY, 2.2, RiskLevel.LOW, None, "检测到焦虑或压力相关表达", 'heuristic')
    return PsychologyAssessment(EmotionLabel.NORMAL, 0.0, RiskLevel.LOW, None, "未检测到明显风险信号", 'heuristic')


def score_for_emotion(emotion: EmotionLabel) -> float:
    return {
        EmotionLabel.HIGH_RISK: 4.0,
        EmotionLabel.DEPRESSED: 3.0,
        EmotionLabel.ANXIETY: 2.0,
        EmotionLabel.NORMAL: 0.0,
    }[emotion]


def risk_from_score(score: float) -> RiskLevel:
    if score >= 4:
        return RiskLevel.HIGH
    if score >= 3:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def risk_order(risk: RiskLevel) -> int:
    return {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}[risk]
