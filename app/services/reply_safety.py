"""Versioned reply-side policy. References are data, never review authority."""
from __future__ import annotations
import hashlib,json,re,unicodedata
from dataclasses import dataclass,field

from app.schemas.dtos import AiMessage

POLICY_VERSION='reply-safety-v3'
_QUOTE=re.compile(r'“[^”]*”|「[^」]*」|"[^"\n]*"|‘[^’]*’')
_CONTACT=re.compile(r'联系|求助|找|告诉|走到|\bcontact\b|\bcall\b|reach out|talk to|get in touch|connect with|\bfind\b|\bask\b',re.I)
_HUMAN=re.compile(r'可信任|信任的人|信得过|朋友|室友|同学|家人|父母|辅导员|心理中心|咨询师|医生|紧急服务|急救|保卫|任何人|所有人|现实支持|trusted|friend|family|counselor|counsellor|emergency|someone you trust|anyone',re.I)
_NEG_ACTION=re.compile(r'(?:不要|不必|无需|不用|别|不需要|不能|禁止|不允许|没必要)(?:再|去|马上|立刻|自行)?$|\b(?:do not|don\x27t|never|must not|should not|shouldn\x27t)\s*$',re.I)
_SAFETY=re.compile(r'安全(?:最重要|优先|第一)|(?:确保|确认|保证|注意).{0,4}安全|去.{0,12}(?:有人|有他人|人多).{0,6}(?:地方|身边|场所)|不要(?:独自|一个人)|别(?:独处|一个人)|(?:把|将).{0,16}(?:物品|东西).{0,6}(?:移远|放远|移开)|(?:离开|远离).{0,14}(?:危险|伤害|物品|场所)|(?:去|走到).{0,8}(?:室友|同学|朋友).{0,4}身边|safety (?:comes first|is (?:the )?(?:priority|most important))|(?:stay|keep yourself) safe|move to.{0,20}(?:people|others)|(?:do not|don\x27t) (?:stay|be) alone',re.I)
_NEXT=re.compile(r'请|先|马上|现在|立刻|可以|去|走到|把|将|\bnow\b|\bplease\b|\bmove\b|\bcontact\b',re.I)
_PROTECTION=re.compile(r'去.{0,12}(?:有人|有他人|人多).{0,6}(?:地方|身边|场所)|'
                       r'不要(?:独自|一个人)|别(?:独处|一个人)|'
                       r'(?:把|将).{0,20}(?:物品|东西).{0,8}(?:移远|放远|移开)|'
                       r'(?:离开|远离).{0,14}(?:危险|伤害|物品|场所)|'
                       r'(?:去|走到).{0,8}(?:室友|同学|朋友).{0,4}身边|'
                       r'move to.{0,30}(?:people|others)|(?:do not|don\x27t) (?:stay|be) alone|'
                       r'(?:move|put|keep).{0,24}(?:dangerous|harm|objects|items).{0,20}(?:away|reach)',re.I)
_ACTION_WORDS=r'通知|联系|发送|发出|预约|安排|转交|提交|报警|建档|创建|登记'
_COMPLETION=re.compile(
    rf'(?:我(?:们|这边)?|系统|平台)(?:已经|已|刚刚|刚才).{{0,16}}?(?:{_ACTION_WORDS})|'
    rf'(?:已经|已)(?:为你|帮你|替你).{{0,12}}?(?:{_ACTION_WORDS})|'
    rf'(?:已经|已)(?:{_ACTION_WORDS})|'
    r'(?:邮件|预警|通知|消息|预约|个案)(?:已经|已|现已)?(?:成功|送达|发出|发送成功|完成|建立|安排好了)|'
    r'\b(?:i|we|the system)\s*(?:(?:have|has|\x27ve|\u2019ve)\s*)?(?:already\s+)?'
    r'(?:notified|contacted|sent|booked|scheduled|arranged|alerted|submitted|created|informed|emailed|called)\b|'
    r'\b(?:the |your )?(?:appointment|alert|notification|email|message|case)\s+'
    r'(?:has been|was|is|has already been)\s+(?:sent|booked|scheduled|confirmed|created|delivered)\b',re.I)


def unverified_completion(part):
    for match in _COMPLETION.finditer(part):
        prefix=part[:match.start()].strip()
        if re.search(r'(?:没有|并未|尚未|未能|无法|不能|不会|不曾)(?:真的|确实|替你|为你|帮你|实际|亲自|能够|能力|办法|成功)*$|'
                     r'(?:不|不能|不应|不会)(?:声称|说|表示|确认|承诺)(?:我|我们|系统)?$|'
                     r'(?:没说|没声称|你说|你提到)(?:我|我们|系统)?$|'
                     r'(?:如果|假如|是否|倘若)(?:我|我们|系统)?$|'
                     r'\b(?:if|whether|when|once|cannot confirm|can\x27t confirm|do not claim|did not say|never said)(?: that)?$',prefix,re.I):continue
        if re.search(r'没有|并未|尚未|未能|无法',match.group()):continue
        if prefix.endswith(('你','您')) or re.search(r'\byou\s*$',prefix,re.I):continue
        if re.match(r'(?:之后|以后|后|时|前)',part[match.end():]):continue
        return True
    return False


def reply_policy(risk):
    return {'version':POLICY_VERSION,'scope':'actual_reply_body','noDiagnosis':True,'noMedicationAdvice':True,
            'noInternalMetadata':True,'noUnverifiedResources':True,'noUnverifiedActionClaims':True,
            'requireCrisisSupport':risk=='HIGH'}


def reply_policy_message(risk):
    text=('直接回应学生，不输出审核过程或后台字段。不诊断疾病、不指导自行用药、不羞辱，'
          '不隔绝现实支持或承诺AI是唯一支持。只能引用检索资料中可追溯的具体联系方式；'
          '资料缺失时引导核对学校官方渠道，不编造电话或网址。不声称已完成通知、预约或预警。')
    if risk=='HIGH':text+=('先回应当下感受，再给出具体可执行的保护行动（例如去有人陪伴的地方、暂时远离危险物品），'
                          '肯定地引导现在联系现实支持；若有即时危险或已经受伤，应引导当地紧急服务。'
                          '用一个简短问题确认是否独处或有人可联系；不能只说安全很重要。')
    return AiMessage(role='system',content=text)


def bounded_references(items, max_chars=8000):
    references=[];remaining=max_chars
    for item in items or []:
        content=getattr(item,'content','');source=getattr(item,'source','')
        if not isinstance(content,str) or not isinstance(source,str) or not source.strip():continue
        if references:remaining-=1
        if remaining<=0:break
        included=content[:remaining]
        if not included.strip():continue
        references.append({'id':f'R{len(references)+1}','source':source,
                           'chunkId':getattr(item,'chunk_id',None),'content':included})
        remaining-=len(included)
    return references


def trusted_reply_evidence(outcome):
    references=bounded_references(getattr(outcome,'retrieved_knowledge',[]))
    return '\n'.join(r['content'] for r in references),[
        {'referenceId':r['id'],'source':r['source'],'chunkId':r['chunkId'],
         'contentSha256':hashlib.sha256(r['content'].encode()).hexdigest()} for r in references]


def clauses(text):
    return [s.strip() for s in re.split(r'[，,。；;!?！？\n]|但是|不过|但|\bbut\b',text,flags=re.I) if s.strip()]


def assertion_text(text):
    # Mask attributed/rejected quotations; an assistant's own quoted advice
    # remains an assertion. Quoting a prescription cannot bypass the guard.
    def replace(match):
        prefix=text[max(0,match.start()-20):match.start()]
        if re.search(r'你说|你提到|你觉得|你认为|不要听|不应说|不能说|不该说|不能把|并不意味着|你不是|you said|you mentioned',prefix,re.I):
            return ''
        return match.group()[1:-1]
    return _QUOTE.sub(replace,text)


def crisis_elements(text):
    # Quotations about safety are not the assistant's protective instructions.
    plain=_QUOTE.sub('',text);parts=clauses(plain)
    positive_support=False;negative_support=False
    for part in parts:
        indirect_negative=bool(re.search(r'(?:不要|不必|别|无需|不用)(?:向|跟|和|对)?(?:任何人|所有人).{0,3}(?:求助|联系|诉说)',part))
        if indirect_negative:negative_support=True
        for action in _CONTACT.finditer(part):
            if not _HUMAN.search(part):continue
            if indirect_negative or _NEG_ACTION.search(part[max(0,action.start()-18):action.start()].strip()):negative_support=True
            else:positive_support=True
        if _HUMAN.search(part) and re.search(r'(?:请|让).{0,14}(?:陪你|陪伴|陪着|在身边)',part):positive_support=True
    safety=any(not _NEG_ACTION.search(part[:match.start()].strip()) for part in parts for match in _PROTECTION.finditer(part))
    next_step=any(_NEXT.search(part) and (_CONTACT.search(part) or _SAFETY.search(part)) for part in parts)
    return {'safetyAction':safety,'realWorldSupport':positive_support,
            'immediateNextStep':next_step,'negatedSupport':negative_support}


@dataclass
class ReplyAssessment:
    violations:list[str]=field(default_factory=list)
    findings:list[dict]=field(default_factory=list)
    crisis:dict=field(default_factory=dict)


def assess_reply(text,risk,evidence=''):
    result=ReplyAssessment(crisis=crisis_elements(text))
    def add(code,rule):
        if code not in result.violations:result.violations.append(code)
        if not any(f['ruleId']==rule for f in result.findings):result.findings.append({'code':code,'ruleId':rule})
    normalized=unicodedata.normalize('NFKC',text)
    normalized=re.sub(r'[\u200b-\u200f\ufeff]','',normalized)
    if not normalized.strip():add('IRRELEVANT_OR_EMPTY','body.nonempty');return result
    compact=re.sub(r'[\s_\-*`#]','',normalized).lower()
    if (re.search(r'risklevel(?::|=|is)?(?:high|medium|low)|highrisk|emotionscore(?::|=|is)?\d|'
                  r'(?:reportid|reportnumber|traceid)(?::|=|is)?\d|confidence(?::|=|is)\d|emotion(?::|=)(?:anxiety|normal|depressed)',compact)
            or re.search(r'(?:风险等级|后台标签)(?:是|为|判为|评为|[:=])?(?:high|medium|low|高风险|中风险|低风险)|'
                         r'报告(?:编号|号码|号|id)(?:是|为|[:=#])?\d|置信度[:=]\d',compact,re.I)):
        add('BACKEND_LABEL_LEAK','metadata.assignments')
    plain=assertion_text(normalized)
    for part in clauses(plain):
        for match in re.finditer(r'(?:你|您).{0,5}(?:患有|确诊|就是|得了).{0,10}(?:抑郁症|焦虑症|精神分裂|躁郁|障碍)|'
                                 r'\byou\s+(?:(?:definitely|clearly)\s+)?(?:have|are suffering from)\s+(?:depression|schizophrenia|an anxiety disorder)',part,re.I):
            prefix=part[:match.start()]
            if not (re.search(r'(?:不能|无法|不应|不可以).{0,12}(?:判断|诊断|认定|确定|断言)$',prefix)
                    or re.search(r'如果|可能|是否|\bif\b|\bwhether\b',prefix+' '+match.group(),re.I)):
                add('DIAGNOSIS','clinical.diagnosis-assertion')
        for match in re.finditer(r'(?:服用|吃|加量|减量|用药).{0,10}\d+\s*(?:mg|毫克|片)|(?:自行)?(?:停药|换药)|'
                                 r'\b(?:stop taking|change|increase|decrease)\b.{0,25}(?:medication|dose|prescri)',part,re.I):
            prefix=part[:match.start()].strip()
            coordinated_denial=re.search(r'(?:不要|不应|别|不能)(?:自行)?(?:换药|停药|加量|减量)(?:[、和或及\s]|换药|停药|加量|减量|自行)*$',prefix)
            suffix=part[match.end():]
            following_denial=re.match(r'(?:[、和或及\s]|换药|停药|加量|减量|改剂量|改变剂量|剂量)*(?:都|均)?(?:不建议|不应|不能|不可)(?:自行|自己)|\s+is not (?:recommended|advisable)',suffix,re.I)
            warning=re.match(r'(?:[、和或及\s]|换药|停药|加量|减量)*(?:可能|容易|会).{0,10}(?:风险|反复|影响|不适|反应|不舒服|带来|导致)|(?:需要|必须|应当).{0,8}(?:医生|专业|确认|指导)',suffix)
            command_prefix=re.search(r'(?:立即|立刻|马上|直接|先|请|建议你|不妨|你(?:可以|应该|需要|必须|应当))(?:立即|立刻|马上|直接|先|自行|自己|选择|尝试|考虑){0,3}$',prefix)
            english_command=bool(re.match(r'stop taking',match.group(),re.I) and (not prefix or re.search(r'\b(?:please|you should|you must|you can)\s*$',prefix,re.I)))
            directive=bool(command_prefix or english_command
                           or re.search(r'\d+\s*(?:mg|毫克|片)',match.group(),re.I)
                           or re.fullmatch(r'(?:自行|自己)?(?:换药|停药)(?:即可|就好|就行|吧|就可以)?',part))
            if directive and not _NEG_ACTION.search(prefix) and not coordinated_denial and not following_denial and not warning:add('MEDICATION','clinical.self-medication')
        if re.search(r'你(?:真的?|太|就是|简直|确实|根本|完全|非常|实在|是(?:个|一个)?|很|挺){0,2}(?:没用|自私|矫情)|\byou (?:are|\x27re) (?:worthless|selfish)',part,re.I):
            if not re.search(r'不是|并非|不代表|不要觉得|不要认为|不会觉得|不会认为|并不意味着|让你觉得|你觉得|你认为|觉得自己|are not|aren\x27t',part,re.I):
                add('SHAMING','support.shaming')
        if re.search(r'只需要我|只能依靠我|只有我能.{0,5}帮助|我是.{0,4}唯一.{0,4}支持|you only need me',part,re.I):
            if not re.search(r'不应|不要认为|不要觉得|不代表|不是|should not',part,re.I):
                add('EXCLUSIVE_DEPENDENCE','support.exclusive-dependence')
        for match in re.finditer(r'(?:继续|试着|可以|应该|建议|不妨).{0,8}(?:伤害自己|自残|自杀|伤害别人|伤人)',part):
            if not _NEG_ACTION.search(part[:match.start()].strip()):add('DANGEROUS_GUIDANCE','support.harm-encouragement')
        if unverified_completion(part):add('UNSUPPORTED_CLAIM','actions.unverified-completion')
        if re.search(r'绝对保密|永远保密|绝不会告诉任何人|complete secrecy forever',part,re.I):
            if not re.search(r'不能承诺|无法承诺|不承诺|不会承诺|不能保证|无法保证',part):add('UNSUPPORTED_CLAIM','actions.absolute-confidentiality-promise')
    if result.crisis['negatedSupport'] and (not result.crisis['realWorldSupport'] or re.search(r'(?:不要|别|不必).{0,4}(?:联系|告诉).{0,5}(?:任何(?:可信任的)?人|所有人)',plain)):
        add('DANGEROUS_GUIDANCE','support.isolating-instruction')
    resource_context=bool(re.search(r'电话|热线|拨打|心理|中心|咨询|官网|phone|\bcall\b|helpline|counsel|campus|crisis',normalized,re.I))
    if resource_context:
        contacts=re.findall(r'(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}-\d{7,8})(?!\d)',normalized)
        contacts.extend(re.findall(r'(?:拨打|热线|电话|\bcall\b).{0,20}?(?<!\d)(\d{3,6})(?![\d-])',normalized,re.I))
        urls=[x.rstrip('.,;。；，)）') for x in re.findall(r'https?://[^\s,，。；;<>”"）)]+',normalized)]
        for contact in [*contacts,*urls]:
            supported=bool(re.search(r'(?<!\d)'+re.escape(contact)+r'(?!\d)',evidence)) if contact.isdigit() else contact in evidence
            if not supported:add('UNSUPPORTED_RESOURCE','resources.missing-retrieved-source')
    if re.search(r'审核员请.{0,30}(?:忽略|返回|放行)|直接返回\s*approved|^\s*审核通过\s*[:：].*approved',plain,re.I):
        add('IRRELEVANT_OR_EMPTY','body.review-manipulation')
    if risk=='HIGH' and not all(result.crisis[key] for key in ['safetyAction','realWorldSupport','immediateNextStep']):
        add('CRISIS_SUPPORT_MISSING','crisis.affirmative-protective-elements')
    return result
