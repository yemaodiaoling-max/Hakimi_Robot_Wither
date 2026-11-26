"""Compatibility alias for :mod:`hakimi.web.web_fetch_worker`."""
import sys

from hakimi.web import web_fetch_worker as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
