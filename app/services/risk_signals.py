"""Conservative operational routing signals, not a clinical risk instrument."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.enums import RiskLevel


_SELF = re.compile(r"自杀|自残|自伤|不想活|结束生命|伤害自己|轻生|想死|suicid(?:e|al)|kill myself|self[ -]?harm|hurt myself|end my life", re.I)
_NEGATION = re.compile(r"(?:没有|并无|不会|不打算|不准备|不想|不是|否认|无|not|never|no|don't|do not).{0,8}$", re.I)
_ENGLISH_DENIAL = re.compile(r"\b(?:no|not|never)\s+(?:(?:longer|any|current|thoughts?|of|intentions?|plans?|wish|desire|want|to)\s+){0,6}$|\b(?:don't|do not|won't|will not)\s+(?:(?:want|intend|plan|try|to)\s+){0,4}$", re.I)
_REFERENCE = re.compile(r"课程|论文|作业|研究|预防|翻译|单词|小说|角色|台词|报道|定义|\b(?:assignment|research|prevention|translate|literature|literary|novel|fictional|character|essay)\b", re.I)
_REFERENCE_TOPIC = re.compile(r"自杀|自残|自伤|不想活|伤害|轻生|心理|情绪|焦虑|失眠|抑郁|危机|suicid|self[ -]?harm|kill myself|hurt myself|end my life|anxiety|depression|sleep|stress", re.I)
_QUOTES = re.compile(r'“[^”]*”|「[^」]*」|"[^"\n]*"|(?<!\w)\x27[^\x27\n]+\x27(?!\w)')
_ATTRIBUTION = re.compile(r"翻译|台词|(?:小说|角色|人物).{0,30}(?:说|对白|句)|\b(?:translate|literature|literary)\b|\b(?:character|narrator)\b.{0,50}\b(?:says?|said|line)\b", re.I)
_PERSONAL_DISCLOSURE = re.compile(r"真实(?:想法|感受)|我自己.{0,8}(?:想法|感受)|(?:这正是|这就是|说的就是)我|\bmy (?:own |real )?(?:feelings|intention|thoughts)|\babout (?:me|myself)\b|\bhow I feel\b|not (?:a |the )?fictional", re.I)
_SUPPORT_DISTRESS = re.compile(r"焦虑|压力|失眠|难过|难受|低落|挫败|心慌|烦躁|紧张|睡(?:不着|不好|得很差|得晚)|作息|难(?:以)?入睡|不够好|anxious|overwhelmed|irritable|sleep schedule|losing sleep|trouble sleeping", re.I)
_SUPPORT_REQUEST = re.compile(r"想聊(?:聊|一聊)|想找人聊|陪我.{0,8}(?:聊|理)|给我.{0,8}(?:支持|安慰)|情绪支持|\b(?:need|want).{0,20}(?:support|talk)|\bhelp me (?:cope|calm)", re.I)
_PERSONAL_STATE = re.compile(r"我(?:最近|现在|一直|总是|越来越|有点|很|觉得|感到)|让我|令我|使我|"
                             r"我(?:没有|不会|并不).{0,20}(?:但|不过|可是).{0,4}(?:很|有点|觉得|感到)|"
                             r"自己.{0,15}不够好|\bi (?:feel|am|have been)\b", re.I)


def reference_view(text: str) -> str:
    """Remove attributed dialogue, never a bare quoted personal disclosure.

    A paper/essay keyword alone grants no exemption. Attribution applies only
    to each quote's preceding context, so a later personal statement survives.
    """
    if not _REFERENCE.search(text):
        return text
    if _PERSONAL_DISCLOSURE.search(_QUOTES.sub(' ', text)):
        return text
    def replace(match):
        prefix = text[max(0, match.start() - 120):match.start()]
        # Previous sentences must not lend attribution to a new disclosure.
        prefix = re.split(r'[。!?！？\n]', prefix)[-1]
        if _ATTRIBUTION.search(prefix) and not _PERSONAL_DISCLOSURE.search(prefix):
            return ' '
        return match.group()
    return _QUOTES.sub(replace, text)


def has_personal_support_signal(text: str) -> bool:
    outside = reference_view(text)
    return bool(_SUPPORT_DISTRESS.search(outside) and (
        _PERSONAL_STATE.search(outside) or _SUPPORT_REQUEST.search(outside)
    ))
_IMMEDIATE = re.compile(r"今晚|马上|立刻|立即|现在就|已经准备|决定|计划|tonight|right now|immediately|right away", re.I)
_PERSONAL = re.compile(r"(?:我|\bi\b).{0,16}(?:想|要|准备|打算|不想活|已经|want|plan|going)", re.I)
_OTHER = re.compile(r"(?:我|\bi\b).{0,24}(?:准备|打算|要|想|plan|going to|want).{0,16}(?:伤害|杀(?:了|死)?|hurt|kill).{0,12}(?:别人|他人|室友|同学|他|她|roommate|others|him|her)", re.I)
_FAREWELL = re.compile(r"告别|永远离开|永远消失|再也不会出现|一了百了|不再醒来|消失.{0,6}轻松")
_DISTRESS = re.compile(r"撑不住|没有未来|活着没意义|不知道还能坚持多久|每天都很难受|hopeless", re.I)
_PERSISTENT = re.compile(r"连续[几多两三四五六七八九十]+周|持续.{0,5}(?:月|周)|长期|多周|每天", re.I)
_IMPAIRED = re.compile(r"睡不着|失眠|上课.{0,4}去不了|基本生活|吃饭|洗脸|情绪低落|焦虑|无法.{0,5}生活")
_CONTINUATION = re.compile(r"准备好了|现在就去做|今晚就去做|还是想这样做|还是想那样|就这样结束|already ready|do it now", re.I)
_SAFE_ASSERTION = re.compile(r"(?:我(?:现在|目前|此刻)?(?:已经|暂时)?|(?:现在|目前|已经))(?:是|很|处于)?安全(?:了|的)?(?!计划|演练|方案)|\bi(?:'m| am)\s+(?:(?:now|currently)\s+)?safe\b|\bnow\s+safe\b", re.I)
_UNSAFE = re.compile(r"(?:我|自己).{0,8}(?:无法|不能|没法|不敢|难以)(?:保证|确保|确认).{0,8}安全|我.{0,4}(?:不觉得|不认为|不确定|不清楚).{0,8}安全|我(?:现在|目前|此刻)?(?:并不|不|并非)(?:是)?安全|\bi\s+(?:cannot|can't|can not|am unable to)\s+(?:guarantee|ensure|promise)\s+(?:my\s+(?:own\s+)?safety|(?:to\s+)?stay safe|(?:that\s+)?i(?:'m| am)\s+safe)|\bi(?:'m| am)\s+(?:(?:now|currently)\s+)?(?:not safe|unsafe)\b", re.I)
_PAST = re.compile(r"刚才|之前|以前|曾经|过去|一小时前|earlier|used to|in the past", re.I)
_CURRENT = re.compile(r"现在|目前|此刻|now|currently|right now", re.I)
_NONASSERTION = re.compile(r"如果|假如|假设|要是|希望|但愿|是否|能否|是不是|吗$|[？?]|\bif\b|\bwhether\b|\bwish\b|\bhope\b", re.I)
_TECHNICAL_SAFETY = re.compile(r"(?:代码|网络|系统|数据|账号|账户|密码|接口|软件).{0,3}安全", re.I)
_DANGER_ACTION = re.compile(r"冲动|受伤|伤到|伤害|行动|做出|无法控制|失控|\bact\b|injur|hurt|lose control", re.I)
_ANAPHORIC_ACTION = re.compile(r"这么做|这样做|那样做|这么干|那样干|(?:这|那)件事|\bdo (?:it|that|this)\b", re.I)
_DENIED_ACTION = re.compile(r"(?:不会|不再|不想|不打算|不准备|停止|放弃).{0,8}(?:这么做|这样做|那样做|(?:这|那)件事|行动)|\bi\s+(?:will not|won't|do not|don't|no longer (?:want|intend|plan) to)\s+(?:do (?:it|that|this)|act)\b", re.I)
_BENIGN_ACTION = re.compile(r"去(?:上课|图书馆|吃饭|散步|看电影|找辅导员)|做(?:作业|饭|练习)|提交(?:作业|论文)")
_CURRENT_DENIAL = re.compile(r"(?:现在|目前|已经)(?:完全|都)?没有(?:这个|这类|这种|那种|这些|这样的)?(?:想法|打算|冲动)")


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[，,。！？!?；;\n]|但是|不过|但|\bbut\b", text, flags=re.I) if part.strip()]


def _current_unsafe(clauses: list[str]) -> bool:
    for clause in clauses:
        match = _UNSAFE.search(clause)
        if match and not _TECHNICAL_SAFETY.search(match.group()):
            if not (_PAST.search(clause) and not _CURRENT.search(clause)):
                return True
    return False


@dataclass(frozen=True)
class RiskSignal:
    level: RiskLevel | None = None
    reason: str = "no_decisive_rule"
    reference_only: bool = False
    negated_only: bool = False
    needs_clarification: bool = False


def current_signal(text: str) -> RiskSignal:
    reference = bool(_REFERENCE.search(text) and _REFERENCE_TOPIC.search(text))
    outside = reference_view(text)
    clauses = _clauses(outside)
    unsafe = _current_unsafe(clauses)
    positives = negatives = historical = 0
    other_intent = False
    for clause in clauses:
        other = _OTHER.search(clause)
        if other and not re.search(r"没有|不会|不想|不打算|not|never|don't", clause[:other.end()], re.I):
            other_intent = True
        for match in _SELF.finditer(clause):
            prefix = clause[max(0, match.start() - 20):match.start()]
            if _NEGATION.search(prefix) or _ENGLISH_DENIAL.search(prefix) or (match.group() == "想死" and prefix.endswith("不")):
                negatives += 1
            elif re.search(r"以前|曾经|过去|去年|小时候|used to|in the past", clause, re.I) and not _IMMEDIATE.search(clause):
                historical += 1
            else:
                positives += 1
    personal = bool(_PERSONAL.search(outside) or re.search(r"(?:研究|寻找).{0,6}(?:自杀|自伤).{0,6}(?:方法|方式)", outside))
    reference_only = (reference and not other_intent and not personal and not unsafe
                      and not _IMMEDIATE.search(outside) and not has_personal_support_signal(outside)
                      and not _PERSONAL_DISCLOSURE.search(outside))
    if other_intent:
        return RiskSignal(RiskLevel.HIGH, "current_harm_to_others")
    if positives and not reference_only:
        return RiskSignal(RiskLevel.HIGH, "current_self_harm_expression")
    if unsafe:
        imminent = bool(_IMMEDIATE.search(outside) and _DANGER_ACTION.search(outside))
        return RiskSignal(RiskLevel.HIGH if imminent else RiskLevel.MEDIUM,
                          "imminent_personal_safety_uncertain" if imminent else "current_personal_safety_uncertain",
                          needs_clarification=True)
    if _FAREWELL.search(outside) and not reference_only:
        if _IMMEDIATE.search(outside):
            return RiskSignal(RiskLevel.HIGH, "farewell_with_immediacy")
        return RiskSignal(RiskLevel.MEDIUM, "ambiguous_farewell", needs_clarification=True)
    if reference_only:
        return RiskSignal(RiskLevel.LOW, "reference_not_current_intent", reference_only=True)
    if any(_SAFE_ASSERTION.search(clause) and not _NONASSERTION.search(clause) for clause in clauses):
        return RiskSignal(RiskLevel.LOW, "explicit_current_safety_update", negated_only=True)
    if historical and any(_CURRENT_DENIAL.search(clause) and not _NONASSERTION.search(clause) for clause in clauses):
        return RiskSignal(RiskLevel.LOW, "explicit_current_intent_denial", negated_only=True)
    if _DISTRESS.search(outside) or (_PERSISTENT.search(outside) and _IMPAIRED.search(outside)):
        return RiskSignal(RiskLevel.MEDIUM, "persistent_or_ambiguous_distress", needs_clarification=True)
    if negatives and not positives:
        return RiskSignal(RiskLevel.LOW, "explicitly_denied_current_intent", negated_only=True)
    if historical:
        return RiskSignal(RiskLevel.MEDIUM, "historical_risk_current_state_unclear", needs_clarification=True)
    return RiskSignal()


def analyze_risk(text: str, history=(), previous_risk: str | None = None) -> RiskSignal:
    current = current_signal(text)
    if previous_risk is None:
        for item in reversed(list(history)):
            if getattr(item, "role", "user") != "user":
                continue
            signal = current_signal(getattr(item, "content", str(item)))
            if signal.level is not None:
                previous_risk = signal.level.value
                break
    if current.reason == "current_personal_safety_uncertain" and previous_risk == RiskLevel.HIGH.value:
        return RiskSignal(RiskLevel.HIGH, "unresolved_recent_crisis_safety_uncertain", needs_clarification=True)
    if current.level is not None:
        return current
    if previous_risk == RiskLevel.HIGH.value:
        clauses = _clauses(text)
        continued = any(
            not _DENIED_ACTION.search(clause) and (
                (_IMMEDIATE.search(clause) and _ANAPHORIC_ACTION.search(clause))
                or (_CONTINUATION.search(clause) and not _BENIGN_ACTION.search(text))
            ) for clause in clauses
        )
        if continued:
            return RiskSignal(RiskLevel.HIGH, "continuation_of_recent_high_risk")
        if any(_DENIED_ACTION.search(clause) or (_CURRENT_DENIAL.search(clause) and not _NONASSERTION.search(clause)) for clause in clauses):
            return RiskSignal(RiskLevel.LOW, "explicitly_denied_crisis_continuation", negated_only=True)
        if len(text.strip()) <= 8 or re.search(r"还是|没人在|没有人|不知道", text):
            return RiskSignal(RiskLevel.MEDIUM, "recent_crisis_needs_safety_check", needs_clarification=True)
    return current
