"""Conservative local estimates, never provider usage or an exact token count."""
import math


def estimate_tokens(text):
    # Estimate ASCII at 1/3 token per character and non-ASCII at 2/3
    # token per UTF-8 byte (Chinese: about 2 tokens/character). This is a
    # planning heuristic, not a provider token count or a hard context limit.
    return max(1, math.ceil(sum(1 if ord(c)<128 else len(c.encode("utf-8"))*2
                                for c in text)/3))


def positive_setting(env, name, default, minimum=1, maximum=64000):
    try:
        value=int(env.get(name,default))
        return min(maximum,max(minimum,value)) if value>0 else default
    except (TypeError,ValueError):
        return default


def segment_estimates(prompt):
    """Known renderer headings only; unknown content stays in the preamble."""
    markers=(("rules", "=== 本轮可信运行规则"),
             ("tools", "=== 可调用工具"),
             ("context", "=== 本群语气、真实对话参考与数据"),
             ("execution_and_memory", "=== 执行提醒"))
    boundaries=[(0,"preamble")]
    for name,marker in markers:
        offset=prompt.find(marker)
        if offset>=0:boundaries.append((offset,name))
    boundaries.sort()
    result={}
    for index,(start,name) in enumerate(boundaries):
        end=boundaries[index+1][0] if index+1<len(boundaries) else len(prompt)
        if end>start:
            part=prompt[start:end]
            result[name]={"chars":len(part),"estimated_tokens":estimate_tokens(part)}
    return result
