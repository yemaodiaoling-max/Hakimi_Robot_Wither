"""Native Vertex Google Search: only snippets tied to supplied source indices."""
import json
import ssl
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from hakimi.models import web_quality


def sources(candidate):
    meta = candidate.get('groundingMetadata') or {}
    chunks = meta.get('groundingChunks') or []
    excerpts = {}
    for support in meta.get('groundingSupports') or []:
        if not isinstance(support, dict):
            continue
        text = (support.get('segment') or {}).get('text')
        if not isinstance(text, str) or not text.strip():
            continue
        for index in support.get('groundingChunkIndices') or []:
            if type(index) is int:
                excerpts.setdefault(index, []).append(text.strip())
    result = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            continue
        web = chunk.get('web') or {}
        url, title = web.get('uri'), web.get('title') or web.get('domain')
        if not isinstance(url, str) or not isinstance(title,str):
            continue
        evidence = ' '.join(dict.fromkeys(excerpts.get(index, [])))[:1100]
        item = web_quality.source({'url':url, 'title':title, 'snippet':evidence, 'provider':'gemini_google_search'})
        if item:
            item['evidence_level'] = 'grounded_summary' if evidence else 'search_snippet'
            result.append(item)
    return result[:5]


def request(query, explicit, timeout, base):
    from hakimi.models import vertex_route
    route = vertex_route.load(base)
    if not route.enabled or route.error:
        return {'status':'gateway_unavailable'}
    key, ca = route.credentials()
    address = vertex_route.GATEWAY_URL + '/hakimi/google-search'
    message = Request(address, data=json.dumps({'query':query, 'explicit':explicit}).encode(),
                      headers={'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
    tls = ssl.create_default_context(cafile=str(ca))
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls), vertex_route._NoRedirect())
    try:
        with opener.open(message,timeout=timeout) as response:
            value = json.loads(response.read(128000))
    except HTTPError as error:
        with error:
            if error.code == 429:
                try:
                    body = json.loads(error.read(2048))
                except (ValueError, OSError):
                    body = None
                if isinstance(body, dict) and body.get('status') in ('budget_unavailable', 'gateway_busy', 'google_http_429'):
                    return {'status': body['status']}
        return {'status':'gateway_http_'+str(error.code)}
    return value if isinstance(value,dict) else {'status':'invalid_response'}
