"""Sol receives structured conversation and provenance for actual observations."""
import json
import unittest

from bili_companion.cognition.qq_summary import (
    SummaryValidationError, feedback_data, normalize_feedback, validate_feedback,
)
from test_cognition_live import response


class VideoFeedbackDialogueTest(unittest.TestCase):
    def test_contextual_prose_combines_picture_and_sound_with_one_optional_frame(self):
        evidence = [dict(id='v', text='背景换成黄色', media_start=2.5, media_end=4, modality='video'),
                    dict(id='a', text='较长的蜂鸣', media_start=3, media_end=4, modality='audio')]
        notes = '背景换成黄色后，响起一声较长的蜂鸣。'
        shot = dict(evidence_index=0, timestamp=3.5)
        value = dict(status='extracted', video_notes=notes, evidence_indices=[1, 0, 1], screenshot=shot)
        result = validate_feedback(response(value), evidence, allow_screenshot=True)
        self.assertEqual(result, dict(status='extracted', video_notes=notes, evidence_ids=['a', 'v'],
                                     screenshot=dict(id='F1', timestamp=3.5, evidence_id='v')))
        for invalid in (None, [shot, shot], dict(shot, evidence_index=1), dict(shot, timestamp=6)):
            with self.subTest(screenshot=invalid):
                result = validate_feedback(response(dict(value, screenshot=invalid)), evidence, allow_screenshot=True)
                self.assertEqual(result['video_notes'], notes)
                self.assertNotIn('screenshot', result)
        self.assertNotIn('screenshot', validate_feedback(response(value), evidence, allow_screenshot=False))

    def test_notes_require_sources_and_old_answers_cannot_replace_them(self):
        source = dict(id='v', text='背景为蓝色', media_start=0, media_end=2, modality='video')
        partial = dict(status='insufficient_evidence', video_notes='可见蓝色背景，声音未能辨清。',
                       evidence_indices=[0], screenshot=None)
        self.assertEqual(validate_feedback(response(partial), [source], allow_screenshot=False)['video_notes'],
                         partial['video_notes'])
        for invalid in (dict(partial, evidence_indices=[]), dict(partial, evidence_indices=[3]),
                        dict(partial, answer='建议换片头'), dict(status='selected', evidence_indices=[0])):
            with self.subTest(invalid=invalid), self.assertRaises(SummaryValidationError):
                validate_feedback(response(invalid), [source], allow_screenshot=False)
        missing = dict(status='insufficient_evidence', video_notes='', evidence_indices=[], screenshot=None)
        self.assertEqual(validate_feedback(response(missing), [], allow_screenshot=False)['video_notes'], '')

    def test_allowlisted_group_context_and_evidence_sources(self):
        raw = {
            'question': '他说了什么？',
            'context': [
                {'sequence': 1, 'speaker': '哈基米（本群已发送）', 'text': '我看到了字幕',
                 'private_id': 'hidden'},
                {'sequence': 2, 'speaker': '群友1', 'reply_to': 1, 'current_question': True},
            ],
            'qq_prior_messages': [
                {'speaker': 'QQ未标注', 'text': '请辨认这句话',
                 'quotes': [{'speaker': '群友1', 'text': '看字幕', 'secret': 'hidden'}]},
            ],
        }
        request = normalize_feedback(raw)
        self.assertNotIn('private_id', request['context'][0])
        self.assertEqual(request['context'][1]['reply_to'], 1)
        self.assertEqual(request['qq_prior_messages'][0]['quotes'],
                         [{'speaker': '群友1', 'text': '看字幕'}])
        versions = [('audio', 'local-wenet-transcript'), ('video', 'local-timestamped-frames'),
                    ('audio', 'native-video-audio'), ('video', 'native-video-frames'),
                    ('video', 'unknown')]
        report = dict(evidence=[dict(media_start=0, media_end=1, modality=modality, text='观察',
                                     version=f'model:{source}:v1') for modality, source in versions],
                      duration=1, coverage_status='partial', missing_modalities=['audio'])
        data, _ = feedback_data(report, request)
        self.assertEqual(data['missing_modalities'], ['audio'])
        self.assertEqual([item.get('source') for item in data['observations']],
                         [source for _, source in versions[:-1]] + [None])
        self.assertEqual(data['context'], request['context'])
        self.assertEqual(data['qq_prior_messages'], request['qq_prior_messages'])

    def test_legacy_strings_and_input_limits_remain_supported(self):
        self.assertEqual(normalize_feedback(dict(question='问题', context=[' a ', 'b', 'c', 'd'])),
                         dict(question='问题', context=['a', 'b', 'c']))
        request = normalize_feedback(dict(
            question='甲'*5000,
            context=[dict(sequence=i, speaker='乙'*300, text='丙'*900) for i in range(1, 20)],
            qq_prior_messages=[dict(speaker='丁'*300, text='戊'*900,
                                    quotes=[dict(text='己'*500)]*5)]*15))
        self.assertEqual(len(request['question']), 1200)
        self.assertEqual(len(request['context']), 12)
        self.assertEqual(len(request['qq_prior_messages']), 10)
        self.assertEqual(len(request['qq_prior_messages'][0]['quotes']), 2)

    def test_long_dialogue_and_evidence_fit_sols_actual_json_limit(self):
        request = normalize_feedback(dict(
            question='甲' * 1000,
            context=[dict(sequence=i, speaker='乙', text='丙' * 220) for i in range(1, 10)],
            qq_prior_messages=[dict(speaker='丁', text='戊' * 160)] * 4))
        evidence = [dict(id=str(i), media_start=i, media_end=i+1, modality='audio',
                         version='model:native-video-audio:v1', text='声音' * 85) for i in range(100)]
        report = dict(evidence=evidence, duration=100, coverage_status='partial',
                      unobserved_intervals=[dict(start=i+0.3333333333, end=i+0.5555555555)
                                            for i in range(100)],
                      summaries=[dict(start=i, end=i+1, text='描述' * 50) for i in range(100)])
        data, chosen = feedback_data(report, request)
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False).encode()), 22000)
        self.assertEqual(data['context'], request['context'])
        self.assertEqual(chosen[0]['id'], '0')
        self.assertEqual(chosen[-1]['id'], '99')
        self.assertEqual(len(chosen), len(data['observations']))
        self.assertEqual([item['index'] for item in data['observations']], list(range(len(chosen))))
