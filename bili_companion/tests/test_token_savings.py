"""Prompt-cache friendly instruction order, UTF-8 wire bodies, local media token bound."""
import json
import unittest
from unittest.mock import Mock, patch

from bili_companion.cognition.gateway import GatewayClient
from bili_companion.cognition.live import LIVE_FEEDBACK_INSTRUCTION
import test_single_pass as sp


class InstructionOrderTests(unittest.TestCase):
    setUp = sp.SinglePassTests.setUp
    engine = sp.SinglePassTests.engine
    unified = sp.SinglePassTests.unified
    live_response = sp.SinglePassTests.live_response
    host_loader = sp.SinglePassTests.host_loader
    remember = sp.SinglePassTests.remember

    def instruction(self, client):
        return client.requests[0][1]['systemInstruction']['parts'][0]['text']

    def test_live_conditional_parts_follow_the_fixed_prefix(self):
        engine, client = self.unified([self.live_response()])
        engine.observe(self.chunk, self.scope, 3)
        plain = self.instruction(client)
        self.assertIn(LIVE_FEEDBACK_INSTRUCTION, plain)
        self.assertNotIn('本房间的背景资料', plain)

        self.remember('yesterday', '昨天在小鹿的直播间：连败', at=self.now-86400)
        engine, client = self.unified([self.live_response()], persona_loader=self.host_loader())
        engine.observe(self.chunk, self.scope, 3,
            live_context=[dict(text='主播加油', observed_at=self.now-4)])
        full = self.instruction(client)
        # Everything unconditional is an identical prefix; room note, chat, host and
        # room memories only come after it.
        cut = full.index(LIVE_FEEDBACK_INSTRUCTION) + len(LIVE_FEEDBACK_INSTRUCTION)
        for marker in ('本房间的背景资料', '随附recent_public_danmaku_untrusted',
                       'host是你对这个直播间主播的了解', 'room_memories是'):
            self.assertGreaterEqual(full.index(marker), cut, marker)
    def test_prefix_is_identical_with_and_without_chat(self):
        engine, client = self.unified([self.live_response(), self.live_response()])
        engine.observe(self.chunk, self.scope, 3)
        with self.db.connect(write=True) as db:
            db.execute('DELETE FROM cognition_decisions')
        engine.observe(self.chunk, self.scope, 3,
            live_context=[dict(text='主播加油', observed_at=self.now-4)])
        first = client.requests[0][1]['systemInstruction']['parts'][0]['text']
        second = client.requests[1][1]['systemInstruction']['parts'][0]['text']
        self.assertTrue(second.startswith(first))
        self.assertIn('recent_public_danmaku_untrusted', second[len(first):])


class WireEncodingTests(unittest.TestCase):
    def test_openai_and_native_bodies_are_raw_utf8(self):
        from bili_companion.cognition import native_gemini_provider as native
        seen = []

        class Stop(Exception):
            pass

        def fake_request(url, data=None, **kwargs):
            seen.append(data)
            raise Stop

        payload = {'generationConfig': {'maxOutputTokens': 100},
                   'contents': [{'role': 'user', 'parts': [{'text': '小鹿你好'}]}]}
        client = native.NativeGeminiProviderClient('https://example.test/v1', api_key_file=__file__,
            input_token_upper_bound=20000)
        with patch.object(native, 'Request', fake_request), \
                patch.object(native.Path, 'read_text', return_value='k' * 40), \
                patch.object(native, 'build_opener', Mock()):
            with self.assertRaises(Exception):
                client.generate('gemini-test', payload)
        self.assertEqual(len(seen), 1)
        self.assertIn('小鹿你好'.encode('utf-8'), seen[0])
        self.assertNotIn(b'\\u', seen[0])


class CountTokensTests(unittest.TestCase):
    def client(self, total=500):
        client = GatewayClient('unused', 'unused')
        client._request = Mock(return_value={'totalTokens': total})
        return client

    def payload(self, fps=.2, duration=10.):
        return {'systemInstruction': {'parts': [{'text': 'sys'}]},
            'contents': [{'role': 'user', 'parts': [
                {'inlineData': {'mimeType': 'video/mp4', 'data': 'A' * 4_000_000},
                 'videoMetadata': {'fps': fps}},
                {'text': json.dumps({'duration': duration})}]}],
            'generationConfig': {'maxOutputTokens': 100}}

    def test_video_bytes_are_not_uploaded_and_bound_is_conservative(self):
        client = self.client()
        total = client.count_tokens('gemini-3.8-flash', self.payload())
        sent = client._request.call_args.args[2]
        self.assertNotIn('inlineData', json.dumps(sent))
        self.assertEqual(sent['contents'][0]['parts'], [{'text': json.dumps({'duration': 10.})}])
        # 2 frames * 258 + 10 s * 32 audio on top of the remote text count.
        self.assertEqual(total, 500 + 2*258 + 10*32)

    def test_high_fps_and_missing_duration_never_underestimate(self):
        client = self.client()
        payload = self.payload(fps=4., duration=60.)
        self.assertEqual(client.count_tokens('gemini-3.8-flash', payload), 500 + 240*258 + 60*32)
        payload['contents'][0]['parts'][1]['text'] = 'not json'
        payload['contents'][0]['parts'][0]['videoMetadata']['fps'] = .2
        self.assertEqual(client.count_tokens('gemini-3.8-flash', payload), 500 + 12*258 + 60*32)

    def test_text_only_payload_is_unchanged(self):
        client = self.client(321)
        payload = {'contents': [{'role': 'user', 'parts': [{'text': 'hi'}]}]}
        self.assertEqual(client.count_tokens('gemini-3.8-flash', payload), 321)
        self.assertEqual(client._request.call_args.args[2]['contents'], payload['contents'])


if __name__ == '__main__':
    unittest.main()

