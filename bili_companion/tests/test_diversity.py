import unittest
from bili_companion.runtime.diversity import candidates, exploration_required, topic

class DiversityTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertTrue(exploration_required(['hearthstone']*2))
        self.assertTrue(exploration_required(['other','hearthstone']*4))
        self.assertFalse(exploration_required(['other','hearthstone']))

    def test_hard_filter_and_empty_pool(self):
        game=dict(bvid='a',title='Hearthstone',category='gaming')
        outside=dict(bvid='b',title='Science',category='Science',source='popular')
        self.assertEqual(candidates([game],[],set(),True),[])
        self.assertEqual([x['bvid'] for x in candidates([game],[outside],set(),True)],['b'])
        self.assertEqual(candidates([game],[outside],{'b'},True),[])

    def test_normal_balanced_deduplicated_bounded(self):
        games=[dict(bvid=str(i),title='Hearthstone') for i in range(20)]
        outside=[dict(bvid='s'+str(i),category='Science') for i in range(8)]
        result=candidates(games,outside+outside,set(),False)
        self.assertEqual(len(result),12)
        self.assertEqual(sum(x['selection_topic']=='other' for x in result),6)
        self.assertEqual(len({x['bvid'] for x in result}),12)

    def test_unknown_is_not_outside_gaming(self):
        self.assertEqual(topic(dict(title='Anything')),'unknown')
        self.assertEqual(candidates([dict(bvid='a')],[],set(),True),[])
