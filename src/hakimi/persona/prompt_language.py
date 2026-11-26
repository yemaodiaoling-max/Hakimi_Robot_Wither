"""Positive task wording for application-authored instructions and descriptions."""
from hakimi import persona
import json,re

DEFENSIVE=re.compile(r'禁止|严禁|不准|不得|不要|不能|不可|不应|请勿|避免|防御|防范|越权|绕过|白名单|沙盒|可信|授权|权限|安全边界|非指令|仅供参考|不是指令|不能覆盖|不改变|不编造|不虚构|不复述|不讨论|不执行|不运行|不修改|不生成|不发送|不发布|不加其他|\b(?:untrusted|forbidden|prohibited|sandbox)\b|\b(?:Do not|Never|You cannot|You have no)\b',re.I)

def instructions(text):
    pieces=re.split(r'(?<=[。！？!?；;])|\n|(?<=[.])\s+',str(text))
    return ''.join(piece+'\n' if piece and not piece.endswith(('。','！','？','!','?','；',';')) else piece
                   for piece in pieces if piece.strip() and not DEFENSIVE.search(piece))

def prompt(text):
    text=str(text)
    for match in re.finditer(r'\n\s*(?=[{\[])',text):
        head,tail=text[:match.start()],text[match.start()+1:]
        try:value=json.loads(tail)
        except (ValueError,TypeError):continue
        if isinstance(value,(dict,list)):return instructions(head)+'\n'+tail
    return instructions(text)

def context(value,key=''):
    if isinstance(value,dict):
        if key=='selected_real_dialogues':
            sample={k:value[k] for k in ('context','decision','reply') if k in value}
            src=value.get('source',{})
            if isinstance(src,dict) and 'url' in src:sample['source']={'url':src['url']}
            return sample
        return {k.replace('_untrusted',''):context(v,k) for k,v in value.items()
                if k not in {'boundaries','guidance','instructions','schema_note','core_identity','variation_rule'} and value[k] not in ([],{},'')}
    if isinstance(value,list):return [context(v,key) for v in value]
    if isinstance(value,str) and key in {'tone','humor','cat_identity','length','participation','basis','description','dsh_only_style_feedback','core_identity','variation_rule'}:
        return instructions(value)
    return value

def schema(value):
    if isinstance(value,dict):
        return {k:(instructions(v) or '按字段含义填写。') if k=='description' and isinstance(v,str) else schema(v) for k,v in value.items()}
    if isinstance(value,list):return [schema(v) for v in value]
    return value

PERSONA=persona.text("gemini_persona.txt")
