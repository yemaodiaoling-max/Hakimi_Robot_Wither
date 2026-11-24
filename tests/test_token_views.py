"""Focused presentation regressions; no network, LLM or live runtime required."""
import base64
import json
import unittest

from hakimi.models.vertex_web import _bwiki_search, _search_results, run
from hakimi.tools.memory_result import memory_read_result


class SearchViewTests(unittest.TestCase):
    def test_bing_keeps_each_full_snippet_once_and_discards_page_noise(self):
        target = 'https://example.org/source?q=事实'
        encoded = 'a1' + base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
        body = f'''<nav>COOKIE AND SEARCH MENU</nav><ol>
          <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u={encoded}">原文标题</a></h2>
            <div class="b_caption"><p>2026年10月30日 — 原文<strong>完整摘要</strong>和第二句话。</p></div></li>
          <li class="b_algo"><h2><a href="{target}">重复标题</a></h2><p>重复摘要</p></li>
          <li class="b_algo"><h2><a href="https://example.net/next">另一原文</a></h2><p>另一份可用摘要。</p></li>
        </ol><footer>FOOTER NOISE</footer>'''
        result = run('web.search', {'query': '事实'}, 1,
                     fetcher=lambda url, timeout: (url, 'text/html', body))
        self.assertNotIn('text', result)
        self.assertEqual([row['url'] for row in result['results']], [target, 'https://example.net/next'])
        self.assertEqual(result['results'][0]['snippet'], '2026年10月30日 — 原文 完整摘要 和第二句话。')
        self.assertNotIn('NOISE', json.dumps(result))
        self.assertNotIn('MENU', json.dumps(result))

    def test_duckduckgo_redirect_and_result_snippet(self):
        body = '''<div class="result results_links"><h2><a class="result__a"
          href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fstory">消息</a></h2>
          <a class="result__snippet" href="https://example.org/story">可核对的摘要与日期。</a></div>'''
        self.assertEqual(_search_results(body, 'https://html.duckduckgo.com/html/'), [
            {'title': '消息', 'url': 'https://example.org/story', 'snippet': '可核对的摘要与日期。'}])

    def test_heading_fallback_preserves_multiple_results_and_separate_summaries(self):
        body = '''<main><h2><a href="https://a.example/">A</a></h2><p>A摘要</p>
          <h2><a href="https://b.example/">B</a></h2><p>B摘要</p></main>'''
        results = _search_results(body, 'https://www.bing.com/search')
        self.assertEqual([row['snippet'] for row in results], ['A摘要', 'B摘要'])

    def test_bwiki_search_has_one_copy_and_keeps_opensearch_description(self):
        def fetcher(url, timeout):
            data = (['词条', ['补充词条'], ['补充摘要'], ['https://wiki.biligame.com/ys/补充词条']]
                    if 'opensearch' in url else {'query': {'search': [
                        {'title': '词条', 'snippet': '原文<span>摘要</span>'},
                        {'title': '词条', 'snippet': '重复内容'}]}})
            return url, 'application/json', json.dumps(data)
        result = _bwiki_search('原神词条', 1, fetcher)
        self.assertNotIn('text', result)
        self.assertEqual([row['snippet'] for row in result['results']], ['原文摘要', '补充摘要'])


class MemoryReadViewTests(unittest.TestCase):
    def test_preserves_provenance_lifecycle_exact_text_and_pagination(self):
        record = {
            'id': 'M17', 'kind': 'episode', 'session_key': 'group:2', 'user_id': '9',
            'message_id': '88', 'text': '  原文\n@9 [引用:87] ✨  ', 'text_kind': 'knowledge_note',
            'status': 'superseded', 'state': 'active', 'superseded_by': 'M18',
            'verification': 'unverified', 'evidence': '群友自述', 'source': 'message:88',
            'time': 10, 'observed_at': 10, 'recorded_at': 20, 'event_time': '去年',
            'provider': 'gemini', 'channel': 'official', 'raw_available': True,
            'manual_original': False, 'source_view': 'note', 'offset': 5,
            'total_chars': 100, 'next_offset': 27,
            'hash': 'a' * 64, 'canonical_hash': 'a' * 64, 'original_id': 3,
            'limit': 22, 'returned_chars': 22, 'source_message_chars': 200,
            'has_more': True, 'excerpted': True,
        }
        before = dict(record)
        view = memory_read_result(record)
        for key in ('text', 'session_key', 'user_id', 'status', 'superseded_by', 'source',
                    'raw_available', 'source_view', 'offset', 'total_chars', 'next_offset',
                    'recorded_at', 'event_time'):
            self.assertEqual(view[key], record[key])
        for key in ('hash', 'canonical_hash', 'state', 'original_id', 'returned_chars',
                    'source_message_chars', 'limit', 'has_more', 'excerpted',
                    'provider', 'channel', 'manual_original', 'verification', 'observed_at'):
            self.assertNotIn(key, view)
        self.assertEqual(record, before)
        self.assertNotIn('evidence', memory_read_result(dict(record, evidence=record['text'])))
        self.assertIsNone(memory_read_result(dict(record, next_offset=None))['next_offset'])

    def test_empty_internal_fields_and_repeated_times_do_not_hide_false_or_end(self):
        view = memory_read_result({
            'id': 'M2', 'kind': 'episode', 'text': '', 'time': 10,
            'observed_at': 10, 'recorded_at': 10, 'event_time': '',
            'evidence': '', 'relations': [], 'source': '', 'expires_at': 0,
            'raw_available': False, 'next_offset': None, 'offset': 0, 'total_chars': 0,
        })
        self.assertEqual(view, {'id': 'M2', 'kind': 'episode', 'text': '', 'time': 10,
                               'event_time': '未知', 'raw_available': False,
                               'offset': 0, 'total_chars': 0, 'next_offset': None})

    def test_legacy_origin_and_identity_mapping_remain_visible(self):
        record = {'id': 'L5', 'text': '旧看法', 'legacy': True, 'state': 'expired',
                  'text_kind': 'legacy_summary', 'origin_label': '旧摘要',
                  'identity_label': '旧群1→新群2；旧QQ3→本地ID 4',
                  'session_key': 'group:2', 'user_id': '4', 'next_offset': None}
        view = memory_read_result(record)
        self.assertEqual(view['status'], 'expired')
        self.assertEqual(view['origin_label'], '旧摘要')
        self.assertEqual(view['identity_label'], record['identity_label'])


if __name__ == '__main__':
    unittest.main()
