"""Compatibility entrypoint forwarding to desktop_pet.server_agent."""
import sys

import desktop_pet.server_agent as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())

sys.modules[__name__] = _impl
