"""Conservative, versioned extraction of explicit user-owned session facts.

This is derived state, never a language-model summary. Questions, hypothetical
examples and attributed statements cannot replace a previously asserted fact.
"""
import re

VERSION = 2
_KEYS = r'项目代号|项目名称|论文主题|目标岗位|回复语言|每天可用时间|称呼|project codename|target role'
_VALUE = r'''[‘“"']?(?P<value>[^，,。；;\n‘’“”"'！？!?]{1,60})(?:[’”"'])?'''
_FACT = re.compile(rf'(?P<key>{_KEYS})\s*(?:改为|改成|更正为|设为|是|为|=|:|：|\bis\b)\s*{_VALUE}', re.I)
_CORRECTION = re.compile(rf'(?P<key>{_KEYS})不是[^，,。；;\n]+[，,]\s*(?:而)?是\s*{_VALUE}', re.I)
_FORGET = re.compile(rf'(?:请(?:你|帮我)?\s*)?(?:忘记|删除|不要记住)\s*(?P<key>{_KEYS})', re.I)
_PREFIX = re.compile(r'(?:请(?:你|帮我)?记住[:：\s]*|更正(?:一下)?[:：\s]*|'
                     r'(?:请(?:你|帮我)?)?(?:把|将)\s*|'
                     r'我(?:(?:这次|本次|当前|现在)?(?:会话|项目))?(?:的)?|'
                     r'(?:my|our|the|this session\x27s)(?:\s+|$))*', re.I)
_QUESTION = re.compile(r'[?？]|(?:吗|么|呢)\s*[。.!！]?\s*$|'
                       r'^(?:什么|哪个|多少|啥|何(?:种|物|人|处|时|$)|谁(?:啊|呀|呢|$)|是否|不是|不确定|不知道|待定|未知|\bwhat\b|\bwhich\b|\bunknown\b)', re.I)


def canonical_key(key):
    key = key.lower()
    return {'project codename': '项目代号', 'target role': '目标岗位'}.get(key, key)


def _prefix(text, start):
    # Periods inside e.g. v1.2 or .NET are values, not clause boundaries.
    return re.split(r'[，,。；;!?！？\n]|\.\s+', text[:start])[-1].strip()


def fact_operations(text):
    """Return ordered (key, value|None) operations; None means explicit forget."""
    operations = []
    for pattern in (_FACT, _CORRECTION):
        for match in pattern.finditer(text):
            if not _PREFIX.fullmatch(_prefix(text, match.start())):
                continue
            value = match['value'].strip().rstrip('.。!！').strip()
            tail = re.split(r'[，,。；;\n]', text[match.end():], maxsplit=1)[0]
            if (not value or _QUESTION.search(value + tail)
                    or (tail.strip() and not re.fullmatch(r'\s*[.!！]*\s*', tail))):
                continue
            operations.append((match.start(), match.end(), canonical_key(match['key']), value))
    for match in _FORGET.finditer(text):
        suffix = re.split(r'[，,。；;\n]', text[match.end():], maxsplit=1)[0].strip()
        if _PREFIX.fullmatch(_prefix(text, match.start())) and suffix in {'', '.', '吧'}:
            operations.append((match.start(), match.end(), canonical_key(match['key']), None))
    # A correction and a plain assertion must follow the actual user order.
    operations.sort(key=lambda item: (item[0], item[1]))
    return [(key, value) for _, _, key, value in operations]
