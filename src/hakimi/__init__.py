"""哈基米 QQ 机器人。

子包按职责划分，详见 docs/architecture.md。旧的扁平模块名（如
``import memory_store``）由 ``hakimi._legacy`` 继续支持。
"""
__version__ = "2026.10.30"

from hakimi import _legacy

_legacy.install()
