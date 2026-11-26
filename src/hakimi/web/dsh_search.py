"""Optional, read-only DSH web provider in a disposable headless profile."""
import base64
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit


def search(query, runtime):
    from hakimi.models import fallback_runner as cli
    installation = cli.discover_installation()
    provider = cli._provider(installation)
    if urlsplit(provider['base_url']).hostname != 'api.deepseek.com':
        return {'error': 'dsh_official_search_not_configured'}

    runtime = Path(runtime)
    enabled = {'web', 'web-search-deepseek', 'headless-startup'}
    patch = [{'id': row, 'disabled': row not in enabled} for row in cli.ALL_ROWS]
    patch.extend([
        {'id': 'web-search-deepseek', 'config': {'maxUses': 1, 'maxTokens': 768}},
        {'insert': [{'id': 'hakimi-dsh-search', 'name': Path(__file__).with_suffix('.mjs').as_uri()}]},
    ])
    cli.write_headless_profile(runtime, patch)
    command = base64.b64encode(cli.POWERSHELL_DRIVER.encode('utf-16-le')).decode('ascii')
    process = subprocess.run(
        [installation['powershell'], '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
        input=('Search: ' + query).encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=cli._child_environment(runtime, installation, provider), cwd=runtime,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    for line in process.stdout.splitlines() if process.returncode == 0 else ():
        if line.startswith(b'HAKIMI_SEARCH ') and len(line) < 110_000:
            return json.loads(line.split(b' ', 1)[1])
    return {'error': 'dsh_search_unavailable'}
