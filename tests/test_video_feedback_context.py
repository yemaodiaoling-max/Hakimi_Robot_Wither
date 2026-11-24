"""The Sol request uses only the visible QQ turn and bounded structured excerpts."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hakimi.tools.bilibili_video import FEEDBACK_REVISION, feedback_input


class VideoFeedbackContextTest(unittest.TestCase):
    def test_qq_sees_contextual_notes_and_only_the_promoted_frame(self):
        from hakimi.tools.bilibili_video import brief, notes_receipt
        shot = dict(id='F1', timestamp=2.5, evidence_id='v')
        report = dict(ok=True, overview='一份无关的整片概述', evidence=[{'id': 'irrelevant'}],
                      _frame_path='/private/frame.jpg', coverage_status='complete',
                      feedback=dict(status='extracted', video_notes='换色时听见脚步声。',
                                    evidence_ids=['v'], screenshot=shot))
        view = brief(report)
        self.assertEqual(view, dict(ok=True, coverage_status='complete',
                                    feedback=dict(status='extracted', video_notes='换色时听见脚步声。', screenshot=shot)))
        self.assertEqual(notes_receipt(report, keep_frame_path=True)['_frame_path'], report['_frame_path'])
        report['feedback'].pop('screenshot')
        self.assertNotIn('_frame_path', notes_receipt(report, keep_frame_path=True))
        self.assertEqual(brief(dict(report, feedback=dict(status='unavailable', video_notes='')))['feedback'],
                         dict(status='unavailable', video_notes=''))
        self.assertIn('overview', brief(dict(report, feedback=None)))

    def test_group_exchange_and_103_quote_preserve_roles_and_order(self):
        group = 'group:1'
        source = dict(session_key=group, message_id='12', user_id='alice', nickname='小李',
                      received_at=120, text='看这个 https://b23.tv/example?session=SIGNED')
        self_reply = dict(session_key=group, message_id='13', user_id='SELF',
                          received_at=130, text='我想知道是背景音乐还是角色配音')
        trigger = dict(session_key=group, message_id='14', user_id='bob', nickname='小王',
                       received_at=140, reply_to='13', text='能辨认出来吗？ access_token=SECRET',
                       qq_prior_messages=[
                           {'speaker': '小李', 'text': '一开始是不是唱歌？'},
                           {'speaker': 'QQ未标注', 'text': self_reply['text'],
                            'same_text_as': '哈基米在本群的已发送回执',
                            'quotes': [{'speaker': '小李', 'text': '一开始是不是唱歌？',
                                        'openid': 'HIDDEN'}]},
                       ])
        older = dict(session_key=group, message_id='11', user_id='OLD', received_at=20,
                     text='较早发言')
        trigger['received_at'] += 900
        source['received_at'] += 900
        self_reply['received_at'] += 900
        foreign = dict(session_key='private:3', message_id='13', user_id='foreign',
                       text='FOREIGN')
        after = dict(session_key=group, message_id='15', user_id='third', received_at=1050,
                     text='UNRELATED_AFTER')
        payload = dict(session_key=group, trigger_id='14', recent_context_messages=[
            trigger, older, foreign, self_reply, after, source])
        request = feedback_input(payload, source)
        self.assertEqual(FEEDBACK_REVISION, 'qq-contextual-video-notes-v5')
        self.assertEqual(request['question'], '能辨认出来吗？')
        first, sent, question = request['context']
        self.assertTrue(first['video_source'])
        self.assertEqual(first['text'], '看这个')
        self.assertEqual(sent['speaker'], '哈基米（本群已发送）')
        self.assertEqual(question['reply_to'], sent['sequence'])
        self.assertTrue(question['current_question'])
        self.assertIn('小王', question['speaker'])
        self.assertEqual(request['qq_prior_messages'][1]['quotes'],
                         [{'speaker': '小李', 'text': '一开始是不是唱歌？'}])
        self.assertEqual(request['qq_prior_messages'][1]['same_text_as'], '哈基米在本群的已发送回执')
        blob = json.dumps(request, ensure_ascii=False)
        for hidden in ('SIGNED', 'SECRET', 'HIDDEN', 'FOREIGN', 'UNRELATED_AFTER', '较早发言'):
            self.assertNotIn(hidden, blob)

    def test_explicit_video_source_must_be_a_current_same_group_row(self):
        question = {'message_id': '14', 'user_id': 'bob', 'text': '这句是什么？'}
        payload = dict(session_key='group:1', trigger_id='14', recent_context_messages=[question])
        source = {'message_id': '13', 'session_key': 'group:2', 'text': 'OTHER_GROUP'}
        self.assertNotIn('OTHER_GROUP', json.dumps(feedback_input(payload, source)))
        self.assertFalse(any(row.get('video_source') for row in feedback_input(payload, source)['context']))
        payload['session_key'] = 'private:1'
        question['reply_to'] = '13'
        prior = dict(session_key='private:1', message_id='13', user_id='bob', text='本地分享说明')
        instruction = dict(session_key='private:1', message_id='12', user_id='bob',
                           received_at=100, text='给组会挑一个无声片头')
        question['received_at'] = 120
        payload['recent_context_messages'] = [instruction, prior, question]
        instruction_row, first, current = feedback_input(payload, prior)['context']
        self.assertEqual(instruction_row['text'], instruction['text'])
        self.assertEqual(first['text'], '本地分享说明')
        self.assertTrue(first['video_source'])
        self.assertEqual(current['reply_to'], first['sequence'])

    def test_ten_short_103_entries_and_serialized_input_cap(self):
        session = 'group:1'
        clip = dict(message_id='1', user_id='alice', text='早先分享的这条影片')
        question = dict(message_id='12', user_id='bob', reply_to='1',
                        text='这个视频的第2段是什么？',
                        qq_prior_messages=[dict(speaker='小李', text=f'QQ前文{i}') for i in range(10)])
        dialogue = [clip] + [dict(message_id=str(i), user_id='alice', text=f'本次群内对话{i}')
                            for i in range(2, 12)] + [question]
        payload = dict(session_key=session, trigger_id='12', recent_context_messages=dialogue)
        short = feedback_input(payload, clip)
        self.assertEqual(len(short['context']), 12)
        self.assertEqual(len(short['qq_prior_messages']), 10)
        self.assertEqual(short['question'], question['text'])

        for row in dialogue:
            row.update(nickname='群友'*60, text=row['text']+'啊'*1000)
        for row in question['qq_prior_messages']:
            row.update(text=row['text']+'文'*1000, attachments=['附件：'+('图'*150)],
                       quotes=[dict(speaker='小王', text='引用'*500)]*4)
        long = feedback_input(payload, clip)
        self.assertLessEqual(len(json.dumps(long, ensure_ascii=False).encode()), 12288)
        self.assertEqual(long['context'][0]['sequence'], long['context'][-1]['reply_to'])
        self.assertTrue(long['context'][0]['video_source'])
        self.assertTrue(long['context'][-1]['current_question'])
        self.assertTrue(long['qq_prior_messages'][-1]['quotes'])

    def test_both_video_entry_points_forward_turn_context_and_refresh_identity(self):
        from hakimi.tools import bilibili_video, qq_video
        session = 'group:1'
        signed_url = 'https://qq.example/video?auth_token=SIGNED_MEDIA_URL'
        source = dict(session_key=session, message_id='1', user_id='author',
                      text='分享标题 https://www.bilibili.com/video/BV1aa411a7aa')
        question = dict(session_key=session, message_id='2', user_id='questioner', text='看第2段',
                        reply_to='1')
        payload = dict(session_key=session, trigger_id='turn-1', reply_target_message_id='2',
                       recent_context_messages=[source, question])
        with TemporaryDirectory() as directory, patch('hakimi.models.search_policy.network_allowed',
                                                      return_value=True):
            base = Path(directory)
            with patch.object(bilibili_video, '_view_process', return_value={}) as view:
                bilibili_video.prepare(payload, base, source=source)
                first_request = view.call_args.args
                question['text'] = '看第3段'
                bilibili_video.prepare(payload, base, source=source)
                second_request = view.call_args.args
            self.assertEqual(first_request[2]['question'], '看第2段')
            self.assertEqual(second_request[2]['question'], '看第3段')
            self.assertNotEqual(first_request[6], second_request[6])

            (base / 'state').mkdir()
            (base / 'video_tools_config.json').write_text(
                json.dumps({'companion_config': str(base / 'companion.json')}), encoding='utf-8')
            (base / 'companion.json').write_text(json.dumps({'video_selection_profile': 'sol'}),
                                                 encoding='utf-8')
            with patch.object(qq_video, 'approved', return_value=True):
                qq_video.register(base, session, '1', [dict(content_type='video/mp4', url=signed_url)])
            with patch.object(qq_video.subprocess, 'run', return_value=SimpleNamespace(stdout='{}')) as worker:
                qq_video.watch(base, payload, '1-0')
                task1 = json.loads(worker.call_args.kwargs['input'])
                question['text'] = '看第4段'
                qq_video.watch(base, payload, '1-0')
                task2 = json.loads(worker.call_args.kwargs['input'])
            self.assertEqual(task1['question'], second_request[2])
            self.assertNotEqual(task1['request_id'], task2['request_id'])
            self.assertNotIn('SIGNED_MEDIA_URL', json.dumps(task2['question']))
