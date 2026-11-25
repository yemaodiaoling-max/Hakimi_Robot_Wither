import json
from datetime import date
from unittest import TestCase
from unittest.mock import Mock

from hakimi.models.anime_calendar import calendar_results
from hakimi.models.search_policy import local_decision


class AnimeSearchTests(TestCase):
    def test_current_calendar_only_and_dates_are_not_publication_dates(self):
        fetch = Mock(return_value={'body':json.dumps({'data':[
            {'platform':'TV', 'id':123, 'name_cn':'日常物语', 'date':'2026-10-04'},
            {'platform':'TV', 'id':456, 'name_cn':'旧番', 'date':'2024-10-05'},
            {'platform':'音乐短片', 'id':789, 'name_cn':'短片', 'date':'2026-10-05'}]})})
        today=date(2026,11,24)
        for query in ['下季新番','日本天气']:
            self.assertEqual(calendar_results(query,fetch,today),[])
        fetch.assert_not_called()
        items=calendar_results('最近有什么新番',fetch,today)
        self.assertEqual([i['title'] for i in items],['日常物语'])
        self.assertIsNone(items[0]['published_at'])
        self.assertEqual(items[0]['air_date'],'2026-10-04')
        self.assertEqual(calendar_results('2025年10月新番',fetch,today),[])
        self.assertEqual(calendar_results('最近有什么异世界新番',fetch,today),[])

    def test_local_decision_distinguishes_a_question_from_small_talk(self):
        self.assertTrue(local_decision('最近有什么新番'))
        self.assertFalse(local_decision('最近的大新闻有点太多了'))
