from copy import deepcopy
import unittest

from bili_companion.cognition.qq_summary import validate_summary,SummaryValidationError
from test_cognition_live import response


class QQSummaryTests(unittest.TestCase):
    def setUp(self):
        self.segments=[dict(start=i*30,end=(i+1)*30,evidence_ids=['e'+str(i)]) for i in range(3)]
        self.value=dict(overview='实际观测材料综述',segment_indices=[0,1,2],
            chapters=[dict(summary='实际阶段',segment_indices=[0,1,2])])

    def test_pure_validation_returns_sources_without_mutating_inputs(self):
        before=deepcopy(self.segments);body=response(self.value);saved=deepcopy(body)
        parsed=validate_summary(body,self.segments,'complete')
        self.assertEqual(parsed['overview_evidence_ids'],['e0','e1','e2'])
        self.assertEqual(parsed['chapters'][0]['end'],90)
        self.assertEqual(self.segments,before)
        self.assertEqual(body,saved)

    def test_fixed_failure_codes_cover_sources_timeline_and_chapters(self):
        cases=[('invalid_summary_sources',{'segment_indices':[True]}),
            ('invalid_overview',{'overview':''}),
            ('overview_missing_timeline',{'segment_indices':[0,1]}),
            ('invalid_chapters',{'chapters':[]}),
            ('invalid_chapter',{'chapters':['malformed']}),
            ('invalid_chapter',{'chapters':[dict(summary='',segment_indices=[0])]}),
            ('invalid_summary_sources',{'chapters':[dict(summary='阶段',segment_indices=[3])]}),
        ]
        for code,change in cases:
            with self.subTest(code=code),self.assertRaises(SummaryValidationError) as caught:
                validate_summary(response(dict(self.value,**change)),self.segments,'complete')
            self.assertEqual(caught.exception.code,code)
        with self.assertRaises(SummaryValidationError) as caught:
            validate_summary(response(self.value,finish='MAX_TOKENS'),self.segments,'complete')
        self.assertEqual(caught.exception.code,'summary_response_invalid')


if __name__=='__main__':unittest.main()
