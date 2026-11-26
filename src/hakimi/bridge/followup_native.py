"""Use the native follow-up classifier when installed with a C++ toolchain."""


def _explicit_followup(previous, incoming):
    if not previous.strip() or not incoming.strip() or max(len(previous), len(incoming)) > 4096:
        return False
    return incoming.lstrip().lower().startswith((
        "补充一下", "补充一点", "再补充", "更正一下", "我说错了", "我打错了",
        "说错了", "打错了", "我的意思是", "我指的是", "i meant ", "to clarify, ",
    ))


try:
    from ._followup_rules import is_continuation
except ImportError:
    is_continuation = _explicit_followup
