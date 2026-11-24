"""Local, per-turn selection; the trusted account route still owns the budget."""
import json
import html
import re
import time
from pathlib import Path

from hakimi.models.vertex_model import HYDROGEN as FLASH, vertex_model
ARGON = 'gemini-4-argon'
CARBON = 'gemini-4-carbon'
DEFAULT = {'mode': 'single', 'single_model': FLASH}

_MODEL_REQUEST = re.compile(
    r'(?:请|麻烦|改用|换成|切到|切换到?|调用|呼叫|召唤|交给|让|用|开启|启动|试试|问问)\s*(?:gemini[- ]?4[- ]?)?carbon(?![a-z0-9_])'
    r'|^\s*(?:@哈基米\s*)?(?:gemini[- ]?4[- ]?)?carbon(?:[，,:：\s]|$)'
    r'|(?:深度|深度度|深度模型|深度思考|deep reasoning|deep think).{0,8}(?:分析|思考|推导|看看|解答|模式)'
    r'|(?:用|开|切到|开启).{0,4}(?:深度|推理|思考)模式',
    re.I
)
_NO_CARBON = re.compile(r'(?:不用|别用|不要用|无需|别让|不要让)\s*(?:gemini[- ]?4[- ]?)?carbon', re.I)

_CASUAL_CHAT = re.compile(
    r'^(?:[哈呵嘻嘿呀哇哦噢嗯额呐吧么呢嘛]|你好|早安|晚安|在吗|哈哈+|笑死|好玩|牛逼|太强了|确实|对啊|真的吗|怎么说|无语|离谱|救命|绷不住|好家伙|懂了|赞同|摸摸|抱抱|吃啥|外卖|睡觉|打卡)+[！!？?~～\s]*$'
    r'|^(?:今天|明天|后天)?(?:天气|气温|温度|穿什么|吃什么|去哪吃|听什么歌|推荐电影|发个表情包|讲个笑话)\??$'
    r'|(?:今天|明天|后天)?(?:天气|温度|穿什么|吃什么|听什么歌|发个表情包)',
    re.I
)

_DEPTH = re.compile(
    r'(?:仔细|深入|深度|详细|系统|严谨|研究|帮我|请).{0,6}(?:分析|论证|评估|比较|推演)'
    r'|(?:提出|设计|构建|建立).{0,20}(?:理论|模型|实验方案|算法)'
    r'|(?:梳理|制定|推荐).{0,12}学习(?:路径|路线)'
)

_MATH_PHYSICS = re.compile(
    r'推导|证明|微积分|矩阵|概率论|代数几何|热力学|量子力学|电磁学|相对论|哈密顿|拉格朗日'
    r'|微分方程|波动方程|薛定谔|纳维-斯托克斯|麦克斯韦|本征值|本征态|傅里叶|偏导|高斯积分'
    r'|群论|流形|拓扑空间|泛函分析|对偶性|协变性',
    re.I
)

_CS_ALGO_SYS = re.compile(
    r'动态规划|二叉树|红黑树|平衡树|图论|最短路径|拓扑排序|复杂度分析|时间复杂度|空间复杂度'
    r'|状态机|死锁|协程|指令集|微架构|寄存器|编译原理|抽象语法树|虚拟内存|缓存一致性'
    r'|Paxos|Raft|分布式一致性|内核|系统调用|并发安全|垃圾回收机制|内存屏障|B\+?树|LSM[- ]?Tree',
    re.I
)

_HARD_REASONING = re.compile(
    r'(?:比较|对比|权衡|取舍|优缺点|本质区别).{1,30}(?:机制|架构|瓶颈|边缘情况|底层逻辑|优劣|异同)'
    r'|(?:分析|论证|剖析).{1,25}(?:因果关系|传导机制|博弈过程|数学期望|稳定性条件)',
    re.I
)

_QUESTION = re.compile(r'为什么|为何|怎么|如何|能否|可行|区别|差异|好处|影响|可能|原理|讲讲|推算|求解|意味着|成立|支持')
_TECH = re.compile(r'超导|拓扑|量子|哈密顿|指令集|微架构|虚拟化|产品线|制程|编译器|复杂度|梯度|归纳偏置|形式逻辑|神经网络|训练后权重|方程|定理|悖论|\b(?:[A-Z]+_[A-Z0-9_]+|cortex[- ][a-z0-9]+|\d+(?:\.\d+)?nm)\b', re.I)


def reasoning_request(row, by_id):
    text = html.unescape(str(row.get('text', ''))[:8000])
    if text.lstrip().startswith(('[卡片消息]', '[群聊的聊天记录]')):
        return None
    text = re.sub(r'https?://\S+', ' ', text)
    if _MODEL_REQUEST.search(text) and not _NO_CARBON.search(text):
        return 'requested_carbon'
    if _CASUAL_CHAT.search(text.strip()):
        return None
    if _DEPTH.search(text):
        return 'analysis_request'
    if _MATH_PHYSICS.search(text) and len(text) >= 10:
        return 'math_physics_reasoning'
    if _CS_ALGO_SYS.search(text) and len(text) >= 10:
        return 'cs_algorithm_reasoning'
    if _HARD_REASONING.search(text) and len(text) >= 12:
        return 'hard_reasoning'
    if not _QUESTION.search(text):
        return None
    # Only a directly quoted human message may supply the subject of an
    # elliptical follow-up. Nearby unrelated chatter is never a topic signal.
    quoted = by_id.get(str(row.get('reply_to') or ''))
    subject = str(quoted.get('text', ''))[:1200] if quoted and quoted.get('user_id') != 'SELF' else ''
    identifier = re.search(r'(?<![A-Za-z0-9_])[A-Z]{2,}_[A-Z0-9_]{2,}(?![A-Za-z0-9_])', text)
    if identifier or _TECH.search(text) or (_TECH.search(subject) and len(text) <= 90):
        return 'technical_reasoning'
    if (len(text) >= 45 and re.search(r'如果|假设|为什么|为何|比较|对比', text)
            and re.search(r'是否|能否|怎么|如何|会不会|区别|差异|取舍|优缺点|原因', text)):
        return 'compound_question'
    return None


def validate(value):
    if not isinstance(value, dict) or not set(DEFAULT) <= set(value) or set(value) - set(DEFAULT) - {'dual_model'}:
        raise ValueError('task_routing_schema_invalid')
    if (value['mode'] not in {'single', 'dual'} or vertex_model(value['single_model']) not in {FLASH, ARGON, CARBON}
            or value.get('dual_model', ARGON) not in {ARGON, CARBON}):
        raise ValueError('task_routing_value_invalid')
    return value


def select(root, payload, original_model):
    # Auxiliary reflections retain their existing model. A missing config also
    # preserves old installations; malformed operator configuration fails closed.
    if isinstance(payload.get('internal_review'), dict):
        return vertex_model(original_model), 'internal_review_unchanged'
    try:
        config = validate(json.loads((Path(root)/'task_routing.json').read_text()))
    except FileNotFoundError:
        return vertex_model(original_model), 'config_missing_unchanged'
    if config['mode'] == 'single':
        return vertex_model(config['single_model']), 'single_config'
    companion = config.get('dual_model', CARBON if original_model == FLASH else ARGON)
    if original_model != ARGON and not (original_model == FLASH and companion == CARBON):
        raise ValueError('dual_routing_requires_compatible_budget_route')
    from hakimi.core.context_window import select_context
    rows, target = select_context(payload)
    window = payload.get('listening_window') or {}
    ids = window.get('selected_message_ids', window.get('new_message_ids', []))
    ids = {str(mid) for mid in ids} if isinstance(ids, list) else set()
    # Only current selected events or the explicit reply anchor, never a group's
    # name, old nearby context, SELF text or the generated system prompt.
    directed = {str(mid) for mid in payload.get('pending_explicit_question_ids', [])}
    anchor = next((i for i, r in enumerate(rows) if r is target), len(rows))
    fresh = {id(r) for r in rows[anchor:] if r.get('user_id') == target.get('user_id')}
    current = [r for r in rows if r.get('user_id') != 'SELF' and
               ((str(r.get('message_id', '')) in ids) if ids else id(r) in fresh or str(r.get('message_id', '')) in directed)]
    text = '\n'.join(str(r.get('text', ''))[:8000] for r in current)
    if companion == CARBON and _NO_CARBON.search(str(target.get('text', ''))[:8000]):
        return FLASH, 'requested_no_carbon'
    if re.search(r'(?:请|帮我|能否|能不能|麻烦)(?:你|我|帮忙|一下|给我|帮我|来|把|将|\s){0,12}(?:推导|证明)|(?:详细|完整|逐步|严谨|从头|一步步).{0,12}(?:推导|证明|论证)|(?:推导|证明).{0,12}(?:详细|完整|每一步)|derive step.by.step|rigorous proof', text, re.I):
        return companion, 'explicit_derivation'
    if re.search(r'考据|交叉(?:核验|验证)|(?:核实|核对|查证).{0,16}(?:原始|一手|多个|多方|出处)|(?:原始|一手).{0,12}(?:文献|史料|来源)', text):
        return companion, 'source_research'
    if re.search(r'(?:先|首先).{1,120}(?:再|然后).{1,120}(?:最后|接着)|(?:检索|搜索|查询|查找).{0,60}(?:比较|对比|整理|计算|验证).{0,60}(?:生成|导出|制作|汇总)', text, re.S):
        return companion, 'explicit_multistep'
    image = any(any(r.get(k) for k in ('image', 'image_path', 'image_paths', 'images')) or re.search(r'\[CQ:image,|\[群友发图', str(r.get('text', ''))) for r in current)
    if image and re.search(r'这题怎么做|(?:求解|解答|证明|推导).{0,12}(?:这题|此题|图中|题目)|(?:这题|此题|题目).{0,12}(?:求解|解答|证明|推导)', text):
        return companion, 'image_problem_solving'
    by_id = {str(r['message_id']): r for r in rows if r.get('message_id')}
    for row in current:
        reason = reasoning_request(row, by_id)
        if reason and (reason != 'requested_carbon' or companion == CARBON):
            return companion, reason
    return FLASH, 'routine_default'


def audit(root, claim, selected, reason, actual, error=None):
    """Metadata only, one record for the full tool loop (including failures)."""
    import fcntl
    row = {'at': time.time(), 'trigger_id': claim.get('trigger_id') or claim.get('payload', {}).get('trigger_id'),
           'reply_target_message_id': claim.get('payload', {}).get('reply_target_message_id'),
           'session_key': claim.get('payload', {}).get('session_key'),
           'selected_model': selected, 'reason': reason, 'actual_requests': actual,
           'error': type(error).__name__ if error else None}
    if getattr(error, 'carbon_failure', None):
        row['failure'] = error.carbon_failure
    try:
        path = Path(root)/'state/task_model_routing.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(row)+'\n')
    except OSError:
        pass
