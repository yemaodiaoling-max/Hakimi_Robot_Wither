"""Cheap recall cues for explicit memory questions; no model calls or writes."""
import re
import shlex

_HISTORY = re.compile(r"还记得|记不记得|你记得|我(?:以前|之前|上次|曾经).{0,28}(?:说|聊|问|喜欢|答应|提|约)|你(?:以前|之前|上次|曾经).{0,28}(?:说|答应|记|聊)|我们.{0,18}(?:上次|之前|约定|说过)|上次.{0,18}(?:我|你|我们)|我说过|你答应过")
_PERSONAL = re.compile(r"(?:你对我|对我的|对我什么|我的好感|我在你).{0,18}(?:印象|看法|好感|感觉|眼里)|你.{0,10}(?:怎么看我|觉得我是什么|对我好感)|我的好感")
_CONFLICT = re.compile(r"(?:你记错|记忆.{0,8}(?:不对|有误|冲突)|之前.{0,12}不是.{0,12}是|约定.{0,8}(?:变了|改了|冲突))")
_CROSS = re.compile(r"另一个群|其他群|别的群|跨群|别群")

def opportunity(target, session):
    text = str(target.get('text') or '')[:1200]
    person = str(target.get('user_id') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', person):
        return None
    personal = bool(_PERSONAL.search(text))
    history = bool(_HISTORY.search(text))
    cross = bool(_CROSS.search(text)) and bool(re.search(r'记得|说|聊|提|约定|印象|关系', text))
    conflict=bool(_CONFLICT.search(text))
    if not (personal or history or cross or conflict):
        return None
    return {'reason': '对本人既有印象的询问' if personal else '明确询问过去的记忆',
            'reason_code': 'memory_conflict' if conflict else 'personal_impression' if personal else 'explicit_history',
            'person_id': person, 'tool': 'memory.graph' if personal else 'memory.search',
            'query': ' '.join(text.split())[:180], 'scope': 'all_groups' if cross else 'current',
            'max_searches': 1, 'max_reads': 2}

def instructions(context, command, claim=None):
    cue = context.get('memory_recall_opportunity')
    if not isinstance(cue, dict):
        return ''
    session = shlex.quote(str(context.get('session_key', '')))
    token = shlex.quote(str((claim or {}).get('token', '')))
    common = f'{command} call {{tool}} --session {session} --turn {token}'
    if cue['tool'] == 'memory.graph':
        call = common.format(tool='memory.graph') + ' --person-id ' + shlex.quote(cue['person_id']) + ' --scope ' + cue['scope'] + ' --limit 12'
    else:
        call = common.format(tool='memory.search') + ' --query ' + shlex.quote(cue['query']) + ' --scope ' + cue['scope']
    return ('\n旧事、人物印象或约定值得接着聊时，可用下面的查询；已有内容够用就直接接话。'
            '按需查一次search/graph，想多了解时最多补读两条memory.read。\n' + call + '\n')



def writing_instructions(context, command, claim=None):
    cue = context.get('social_memory_opportunity')
    if not isinstance(cue, dict):
        return ''
    person = str(cue.get('target_id') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', person):
        return ''
    session = shlex.quote(str(context.get('session_key', '')))
    token = shlex.quote(str((claim or {}).get('token', '')))
    return ('\n人物小本子可用自己的口吻记观察、吐槽、约定或当下感受：在已有印象后随手添一句用memory.social_append；'
            '初次建档或大幅改写立体画像用memory.social。note原文逐字保存；混熟了或有新体感时可顺手给/微调closeness与affinity（带confidence），分数留空沿用旧值。\n'
            f"{command} call memory.social_append --session {session} --turn {token} --target-id {person} --note '<随手补在原印象后的一句新细节>'\n"
            f"{command} call memory.social --session {session} --turn {token} --target-id {person} --note '<完整立体的第一人称人物画像>'\n")
