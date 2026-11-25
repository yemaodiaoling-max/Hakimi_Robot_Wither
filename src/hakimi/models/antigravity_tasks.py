"""Optional per-instance Antigravity routes using the existing QQ tool executor."""
import time

from hakimi.core.runtime_io import read_json_cached
from hakimi.models import antigravity_acp as acp
from hakimi.models.vertex_route import root_for
from hakimi.models.vertex_session import VertexSession


def settings(base):
    return read_json_cached(root_for(base) / 'antigravity_tasks.json', {}, readonly=True)


def routing(base, payload, route):
    from hakimi.models.task_routing import ARGON, CARBON, FLASH, select
    options = settings(base)
    if not route.enabled and not (options.get('complex') or options.get('reflection')):
        return None
    original, reason = select(base, payload, route.model if route.enabled else FLASH)
    model = None
    if isinstance(payload.get('internal_review'), dict):
        model = options.get('reflection')
    elif original in {CARBON, ARGON} and reason not in {'single_config', 'config_missing_unchanged'}:
        model = options.get('complex')
    if model:
        return {'model': model, 'reason': 'internal_review' if 'internal_review' in payload else reason,
                'provider': 'antigravity'}
    return {'model': original, 'reason': reason} if route.enabled else None


def prompt_blocks(body):
    blocks = []
    for part in body['systemInstruction']['parts']:
        blocks.append({'type': 'text', 'text': part['text']})
    for content in body['contents']:
        blocks.append({'type': 'text', 'text': '\nConversation role: ' + content['role']})
        for part in content['parts']:
            if part.get('thought'):
                continue
            if 'text' in part:
                blocks.append({'type': 'text', 'text': part['text']})
            elif 'inlineData' in part:
                image = part['inlineData']
                blocks.append({'type': 'image', 'mimeType': image['mimeType'], 'data': image['data']})
    return blocks


class AntigravitySession(VertexSession):
    def __init__(self, model, cwd, env, **kwargs):
        super().__init__([], cwd, env, **kwargs)
        self.model = model
        self.reflection = False

    def _request(self, payload, timeout):
        from hakimi.models.carbon_session import JSON_PROTOCOL, encode_request, decode_response
        if self.reflection:
            # tools.list may expand normal QQ turns. A reflection remains silent.
            from hakimi.prompt.tool_selection import select_names
            for tool in payload.get('tools', []):
                rows = tool['functionDeclarations']
                names = select_names({'internal_review': {}}, {r['name'] for r in rows})
                tool['functionDeclarations'] = [r for r in rows if r['name'] in names]
        body, schemas = encode_request(payload, protocol=JSON_PROTOCOL +
            '\nReturn the JSON object directly. Only the application executes its declared tools. '
            'Do not invoke Antigravity host tools or respond in Markdown fences.')
        raw, ident, usage = acp.complete(self.model, prompt_blocks(body), timeout, self._progress)
        # ACP counts thought tokens within outputTokens.
        thoughts = usage.get('thoughtTokens', 0)
        metadata = {'promptTokenCount': usage.get('inputTokens', 0), 'thoughtsTokenCount': thoughts,
                    'candidatesTokenCount': max(0, usage.get('outputTokens', 0) - thoughts),
                    'totalTokenCount': usage.get('totalTokens', 0)}
        value = {'_provider': 'antigravity', '_provider_request_id': ident,
                 'modelVersion': self.model, 'usageMetadata': metadata,
                 'candidates': [{'content': {'role': 'model', 'parts': [{'text': acp.json_text(raw)}]}}]}
        try:
            return decode_response(value, schemas)
        except RuntimeError as exc:
            # The shared loop audits this request and allows one protocol correction.
            value['_carbon_error'] = str(exc)
            value['_invalid_tool'] = getattr(exc, 'tool_name', '')
            value['_argument_issue'] = getattr(exc, 'argument_issue', '')
            return value


def run_chat(prompt, coordinator, claim, cwd, env, review_timeout):
    review = isinstance(claim['payload'].get('internal_review'), dict)
    left = max(0.1, claim.get('deadline', time.time() + 420) - time.time())
    limit = min(review_timeout if review else 420, left)
    session = AntigravitySession(claim['task_model_routing']['model'], cwd, env)
    session.reflection = review
    if not coordinator.record_progress(claim['token'], wait_seconds=min(360, limit)):
        raise RuntimeError('antigravity_turn_ownership_lost')
    return session.generate(prompt, coordinator, claim, timeout=limit)


def diary_text(model, prompt, timeout):
    text, _, _ = acp.complete(model, [{'type': 'text', 'text': prompt +
        '\n只根据上述材料直接输出所要求的 JSON 对象。'}], timeout)
    return acp.json_text(text)
