"""Search, fetch and parse public pages for web tools."""
from html.parser import HTMLParser
import base64
import html
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qs, urlencode, quote, unquote
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


from hakimi.prompt.tool_catalog import TOOLS as _CATALOG
TOOLS = [{'name':t['name'],'description':t['description'],'parametersJsonSchema':t['inputSchema']}
         for t in _CATALOG if t['name'] in {'web.search','web.read','web.sources'}]


_TAG_RE = re.compile(r'<[^>]+>')
_BING_DECOY_HOSTS = ('amctheatres.com',)


def _normalize_url(url):
    parsed = urlsplit(str(url or '').strip())
    path = quote(unquote(parsed.path or '/'), safe="/:@!$&'()*+,;=._~-")
    query = quote(unquote(parsed.query or ''), safe="=&/:@!$'()*+,;._~-?")
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ''))


def _bili_cookie_header():
    try:
        from hakimi.tools.bilibili_login import VAULT, _crypt
        if VAULT.exists():
            rows = json.loads(_crypt(VAULT.read_bytes(), False))
            return '; '.join(f"{r['name']}={r['value']}" for r in rows if r.get('name') and r.get('value'))
    except Exception:
        pass
    return ''


def public_url(url):
    url = _normalize_url(url)
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('public_https_url_required')
    if parsed.port not in (None, 443):
        raise ValueError('public_https_url_required')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('public_https_url_required')
    return url


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skipped = 0
        self.rows = []
        self.links = []
        self.link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'select', 'option'):
            self.skipped += 1
        if not self.skipped:
            if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr'):
                self.rows.append('\n')
            if tag == 'a':
                self.link = [attrs.get('href', ''), []]

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'select', 'option'):
            self.skipped = max(0, self.skipped - 1)
        if tag == 'a' and self.link:
            self.links.append((self.link[0], ''.join(self.link[1]).strip()))
            self.link = None

    def handle_data(self, data):
        if not self.skipped:
            self.rows.append(data)
            if self.link:
                self.link[1].append(data)


class _SearchHTML(HTMLParser):
    """Small tree for result-local title/snippet extraction, never page chrome."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ('', {}, [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = (tag, dict(attrs), [])
        self.stack[-1][2].append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1][2].append((tag, dict(attrs), []))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1][2].append(data)


def _nodes(node):
    yield node
    for child in node[2]:
        if not isinstance(child, str):
            yield from _nodes(child)


def _node_text(node):
    if node[0] in {'script', 'style', 'noscript', 'select', 'option'}:
        return ''
    return ' '.join(child if isinstance(child, str) else _node_text(child) for child in node[2])


def _search_target(href, base):
    href = urljoin(base, href)
    params = parse_qs(urlsplit(href).query)
    target = params.get('uddg', [href])[0]
    encoded = params.get('u', [''])[0]
    if encoded.startswith('a1'):
        try:
            target = base64.urlsafe_b64decode(encoded[2:] + '=' * (-len(encoded[2:]) % 4)).decode('utf-8')
        except (ValueError, UnicodeError):
            return ''
    host = (urlsplit(target).hostname or '').lower()
    if (urlsplit(target).scheme != 'https' or not host
            or any(host == site or host.endswith('.' + site)
                   for site in ('bing.com', 'duckduckgo.com', *_BING_DECOY_HOSTS))):
        return ''
    return target


def _search_results(body, base):
    parser = _SearchHTML(); parser.feed(body)
    nodes = list(_nodes(parser.root))
    blocks = [node for node in nodes if {'b_algo', 'result'} & set(node[1].get('class', '').split())]
    # Alternate markup: retain heading-based results and their surrounding
    # description rather than discarding results when provider classes change.
    if not blocks:
        for tags in ({'h2', 'h3'}, {'a'}):
            for node in nodes:
                starts = [i for i, child in enumerate(node[2])
                          if not isinstance(child, str) and child[0] in tags]
                for index, start in enumerate(starts):
                    end = starts[index + 1] if index + 1 < len(starts) else len(node[2])
                    blocks.append(('', {}, node[2][start:end]))
            if blocks:
                break
    results, seen = [], set()
    for block in blocks:
        children = list(_nodes(block))
        headings = [node for node in children if node[0] in {'h2', 'h3'}
                    or 'result__a' in node[1].get('class', '').split()]
        anchors = [node for heading in headings for node in _nodes(heading) if node[0] == 'a']
        if not anchors:
            anchors = [node for node in children if node[0] == 'a']
        for anchor in anchors:
            target = _search_target(anchor[1].get('href', ''), base)
            title = _clean_snippet(_node_text(anchor))
            if not target or not title or target in seen:
                continue
            descriptions = [_clean_snippet(_node_text(node)) for node in children
                            if node[0] == 'p' or 'result__snippet' in node[1].get('class', '').split()]
            snippet = ' '.join(dict.fromkeys(text for text in descriptions if text))
            if not snippet:
                snippet = _clean_snippet(_node_text(block)).replace(title, '', 1).strip()
            seen.add(target)
            results.append({'title': title, 'url': target, 'snippet': snippet})
            break
    return results


def fetch(url, timeout):
    url = public_url(url)
    opener = build_opener(ProxyHandler({}), _Redirect())
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; Hakimi/1.0)'}
    host = (urlsplit(url).hostname or '').lower()
    if any(host==domain or host.endswith('.'+domain) for domain in ('biligame.com','bilibili.com')):
        cookie = _bili_cookie_header()
        if cookie:
            headers['Cookie'] = cookie
            headers['User-Agent'] = 'Mozilla/5.0'
    with opener.open(Request(url, headers=headers), timeout=timeout) as response:
        kind = response.headers.get_content_type()
        if kind not in ('text/html', 'text/plain', 'application/xhtml+xml', 'application/json','application/rss+xml','application/atom+xml','application/xml','text/xml'):
            raise ValueError('web_content_type_not_text')
        raw=response.read(2_000_001)
        if len(raw)>2_000_000:raise ValueError('web_response_too_large')
        text = raw.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
        return response.url, kind, text


def _clean_snippet(raw):
    return re.sub(r'\s+', ' ', html.unescape(_TAG_RE.sub('', str(raw or '')))).strip()


def _bwiki_page_title(url):
    parsed = urlsplit(str(url or '').strip())
    if (parsed.hostname or '').lower() != 'wiki.biligame.com':
        return ''
    path = unquote(parsed.path or '')
    if path.startswith('/ys/') and not path.startswith('/ys/api.php') and not path.startswith('/ys/index.php'):
        return path[4:].strip('/')
    return ''


def _bwiki_read(title, canonical_url, timeout, get):
    api_url = 'https://wiki.biligame.com/ys/api.php?' + urlencode({
        'action': 'parse', 'page': title, 'prop': 'wikitext', 'redirects': 1, 'format': 'json'
    })
    _, _, body = get(api_url, timeout)
    data = json.loads(body)
    wikitext = ((data.get('parse') or {}).get('wikitext') or {}).get('*', '')
    if not wikitext:
        raise ValueError('bwiki_page_empty')
    return {'url': _normalize_url(canonical_url), 'text': wikitext[:24000]}


def _bwiki_search(query, timeout, get):
    clean_q = re.sub(r'site:\S+', ' ', str(query or ''), flags=re.I)
    clean_q = re.sub(r'^\s*原神\s*', '', clean_q).strip() or str(query or '').strip()
    api_url = 'https://wiki.biligame.com/ys/api.php?' + urlencode({
        'action': 'query', 'list': 'search', 'srsearch': clean_q, 'srlimit': 8, 'format': 'json'
    })
    _, _, body = get(api_url, timeout)
    data = json.loads(body)
    items = (data.get('query') or {}).get('search') or []
    results = []
    seen = set()
    for item in items:
        title = str(item.get('title') or '').strip()
        if not title:
            continue
        page_url = 'https://wiki.biligame.com/ys/' + quote(title, safe="/:@!$&'()*+,;=._~-")
        if page_url in seen:
            continue
        snippet = _clean_snippet(item.get('snippet', ''))
        seen.add(page_url)
        results.append({'title': f'{title} - 原神BWIKI', 'url': page_url, 'snippet': snippet})
    if len(results) < 3:
        open_url = 'https://wiki.biligame.com/ys/api.php?' + urlencode({
            'action': 'opensearch', 'search': clean_q.split()[0], 'limit': 5, 'format': 'json'
        })
        try:
            _, _, obody = get(open_url, timeout)
            odata = json.loads(obody)
            if isinstance(odata, list) and len(odata) >= 4:
                snippets = odata[2] if isinstance(odata[2], list) else []
                for index, (title, page_url) in enumerate(zip(odata[1], odata[3])):
                    if page_url and page_url not in seen:
                        seen.add(page_url)
                        snippet = _clean_snippet(snippets[index]) if index < len(snippets) else ''
                        results.append({'title': f'{title} - 原神BWIKI', 'url': page_url, 'snippet': snippet})
        except Exception:
            pass
    return {'query': query, 'results': results}


def _is_genshin_query(query):
    q = str(query or '').lower()
    if 'wiki.biligame.com/ys' in q or 'biligame' in q or 'bwiki' in q:
        return True
    keywords = ('原神', '提瓦特', '魔神任务', '世界任务', '挪德卡莱', '空月之歌', '哥伦比娅', '希珀塞莱尼',
                '三月女神', '霜月', '恒月', '虹月', '桑娜妲', '艾莉亚', '卡侬', '愚人众', '执行官', '丑角',
                '博士', '多托雷', '木偶', '桑多涅', '队长', '卡皮塔诺', '仆人', '阿蕾奇诺', '少女', '法尔伽',
                '尼可', '艾莉丝', '杜林', '丝柯克', '纳塔', '枫丹', '须弥', '至冬', '深渊', '天理', '仙灵',
                '白马仙人', '执灯人', '狂猎', '火神', '玛薇卡', '茜特菈莉', '希诺宁', '尼伯龙根', '法涅斯')
    return any(k in q for k in keywords)


def run(name, arguments, timeout, fetcher=None):
    get = fetcher or fetch
    if name == 'web.read':
        target_url = arguments['url']
        title = _bwiki_page_title(target_url) if fetcher is None else ''
        if title:
            try:
                return _bwiki_read(title, target_url, timeout, get)
            except Exception:
                pass
        url, kind, body = get(target_url, timeout)
        if kind != 'text/plain':
            parser = Text(); parser.feed(body)
            body = ''.join(parser.rows)
        return {'url': url, 'text': body[:24000]}
    if name == 'web.search':
        query = str(arguments.get('query') or '')
        if fetcher is None and _is_genshin_query(query):
            try:
                bwiki = _bwiki_search(query, timeout, get)
                if bwiki['results']:
                    return bwiki
            except Exception:
                pass
        url = 'https://www.bing.com/search?' + urlencode({'q': query})
        _, _, body = get(url, timeout)
        results = _search_results(body, url)
        if fetcher is None and not results:
            try:
                ddg_url = 'https://html.duckduckgo.com/html/?' + urlencode({'q': query})
                _, _, dbody = get(ddg_url, timeout)
                results = _search_results(dbody, ddg_url)
            except Exception:
                pass
        return {'query': query, 'results': results}
    raise ValueError('unknown_web_tool')
