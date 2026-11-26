"""Historical import path for the console settings endpoints."""
import sys

from hakimi.console import setup_api

sys.modules[__name__] = setup_api
