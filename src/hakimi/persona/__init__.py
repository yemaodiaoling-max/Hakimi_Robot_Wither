"""人设与提示词：文本数据。包内文件是默认值，实例目录可以逐个覆盖。

* ``text(name)`` - 一段提示词原文（如 ``gemini_persona.txt``、``ops/sol_repair.txt``），逐字使用。
  读取顺序：``<实例>/persona/<name>``（设置面板「人设」里保存的自定义版本）→ 包内默认。
* ``style_guide.md`` / ``style_examples.jsonl`` / ``dsh_style_examples.jsonl`` 由
  ``hakimi.paths.persona(name, base)`` 读取，顺序相同。
* 可在面板里编辑的条目见 ``hakimi.persona.catalog``；每个文件的用途见本目录 README.md。

修改后重启相关服务生效（读取结果在进程内缓存）。
"""
from functools import lru_cache

from hakimi.paths import PERSONA_DIR, persona_overrides


@lru_cache(maxsize=None)
def text(name):
    """Exact text of a persona / prompt file (no stripping, no formatting)."""
    custom = persona_overrides() / name
    path = custom if custom.is_file() else PERSONA_DIR / name
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()
