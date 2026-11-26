"""Provisioning and budget fields shared by the gateway and its clients."""
import json
import os

from hakimi import paths

PROVISION_NAME = "gateway_provision.json"
TIMEZONE = "America/Los_Angeles"
MONTHLY_EXHAUSTED = "vertex_monthly_budget_exhausted"


def gateway_project():
    """Project label of the configured gateway (``gateway_provision.json``)."""
    env = os.environ.get("HAKIMI_GATEWAY_PROJECT", "").strip()
    if env:
        return env
    try:
        return str(json.loads((paths.gateway_dir() / PROVISION_NAME).read_text())["project"])
    except (OSError, ValueError, KeyError, TypeError):
        return "hakimi-local"


PROJECT = gateway_project()
