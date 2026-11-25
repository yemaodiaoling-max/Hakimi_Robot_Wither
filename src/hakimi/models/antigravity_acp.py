"""A single, model-verified Antigravity OAuth turn, without host tool authority."""
import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile


class AntigravityError(RuntimeError):
    pass


async def _exchange(process, model, work, blocks, progress):
    answer = []
    size = 0
    session_id = None

    async def send(message):
        process.stdin.write((json.dumps(dict(jsonrpc='2.0', **message)) + '\n').encode())
        await process.stdin.drain()

    async def request(index, method, **params):
        nonlocal size
        await send(dict(id=index, method=method, params=params))
        while True:
            raw = await process.stdout.readline()
            if not raw:
                raise AntigravityError('antigravity_disconnected')
            event = json.loads(raw)
            if 'method' not in event and event.get('id') == index:
                if 'error' in event:
                    raise AntigravityError('antigravity_request_failed_' + method.replace('/', '_'))
                return event['result']
            if 'id' in event and 'method' in event:
                if event['method'] == 'session/request_permission':
                    await send(dict(id=event['id'], result={'outcome': {'outcome': 'cancelled'}}))
                else:
                    await send(dict(id=event['id'], error={'code': -32601, 'message': 'Unavailable'}))
                continue
            notice = event.get('params', {})
            if event.get('method') != 'session/update' or notice.get('sessionId') != session_id:
                continue
            update = notice.get('update', {})
            kind = update.get('sessionUpdate')
            if kind in {'tool_call', 'tool_call_update'}:
                raise AntigravityError('antigravity_native_tool_blocked')
            if kind == 'agent_message_chunk' and update.get('content', {}).get('type') == 'text':
                chunk = update['content']['text']
                size += len(chunk)
                if size > 250000:
                    raise AntigravityError('antigravity_output_limit')
                answer.append(chunk)
            if progress and kind in {'agent_message_chunk', 'agent_thought_chunk'} and not progress():
                raise AntigravityError('antigravity_turn_ownership_lost')

    init = await request(1, 'initialize', protocolVersion=1, clientCapabilities={},
                         clientInfo={'name': 'hakimi', 'version': '1'})
    if any(b['type'] == 'image' for b in blocks) and not init['agentCapabilities']['promptCapabilities'].get('image'):
        raise AntigravityError('antigravity_images_unavailable')
    session = await request(2, 'session/new', cwd=str(work), mcpServers=[])
    session_id = session['sessionId']
    available = {item['modelId'] for item in session['models']['availableModels']}
    family, separator, effort = model.rpartition('-')
    if not separator or model not in available:
        raise AntigravityError('antigravity_model_unavailable')
    for index, (key, setting) in enumerate((('model', family), ('thought_level', effort)), 3):
        config = await request(index, 'session/set_config_option', sessionId=session_id,
                               configId=key, value=setting)
    active = {opt['id']: opt['currentValue'] for opt in config['configOptions']}
    if (active.get('model'), active.get('thought_level')) != (family, effort):
        raise AntigravityError('antigravity_model_mismatch')
    result = await request(5, 'session/prompt', sessionId=session_id, prompt=blocks)
    if result.get('stopReason') != 'end_turn' or not ''.join(answer).strip():
        raise AntigravityError('antigravity_incomplete')
    return ''.join(answer).strip(), session_id, result.get('usage') or {}


async def _run(model, blocks, timeout, progress):
    home = Path.home()
    gemini = home / '.gemini'
    credential = gemini / 'antigravity-cli' / 'antigravity-oauth-token'
    if not credential.is_file():
        raise AntigravityError('antigravity_login_required')
    settings = {'allowNonWorkspaceAccess': False, 'toolPermission': 'strict',
                'permissions': {'deny': [name + '(*)' for name in
                 ('read_file', 'write_file', 'command', 'mcp', 'read_url', 'execute_url')]}}
    with tempfile.TemporaryDirectory(prefix='hakimi-acp-', dir=gemini) as profile_dir, \
            tempfile.TemporaryDirectory(prefix='hakimi-acp-work-') as work:
        profile = Path(profile_dir)
        (profile / 'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        (profile / credential.name).symlink_to(credential)
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'XDG_RUNTIME_DIR',
               'DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
        env['AGY_CLI_DISABLE_AUTO_UPDATE'] = 'true'
        process = await asyncio.create_subprocess_exec(
            str(home / '.local/bin/agy'), 'acp', '--app-data-dir', profile.name,
            '--cli_dir_name', '.', '--include-default-customizations=false', '--sandbox',
            cwd=work, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=2 * 1024 * 1024)
        try:
            return await asyncio.wait_for(_exchange(process, model, work, blocks, progress), timeout)
        except (KeyError, TypeError, ValueError) as exc:
            raise AntigravityError('antigravity_protocol_error') from exc
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()


def complete(model, blocks, timeout, progress=None):
    return asyncio.run(_run(model, blocks, timeout, progress))


def json_text(text):
    text = text.strip()
    if text.startswith(('\x60\x60\x60json\n', '\x60\x60\x60\n')) and text.endswith('\x60\x60\x60'):
        return text.split('\n', 1)[1][:-3].strip()
    return text
