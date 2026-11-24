"""Graph scoring and ranking: direction, unknowns, and evidence before affinity."""
import unittest
from hakimi.console.graph_display import display_score, select_nodes


def edge(a='1', b='2', rows=(), events=0):
    return {'source': 'person:'+a, 'target': 'person:'+b, 'directions': list(rows),
            'scores': {}, 'updated_at': 1,
            'interaction': {'stats': {'events': events, 'last_at': 1}}}


def opinion(direction, score, stamp=1, source='terra_assisted'):
    return {'direction': direction, 'affinity': score, 'scores_available': score is not None,
            'created_at': stamp, 'assessment_id': stamp, 'provenance': source}


class GraphDisplayTests(unittest.TestCase):
    def test_overview_averages_directions_and_uses_latest_without_source_bias(self):
        item = edge(rows=[opinion('1->2', 90), opinion('2->1', 10),
                          opinion('1->2', 70, 2, 'gemini_manual')])
        self.assertEqual(display_score(item), 40)
        self.assertEqual(item['display_basis'], 'bidirectional_mean')
        self.assertEqual(len(item['directions']), 3)
        self.assertEqual(display_score(item, source='terra'), 50)
        self.assertEqual(display_score(item, '1', source='manual'), 70)
        self.assertIsNone(display_score(item, '2', source='manual'))

    def test_single_direction_focus_and_explicit_unknown(self):
        item = edge(rows=[opinion('1->2', 80)])
        self.assertEqual(display_score(item), 80)
        self.assertIsNone(display_score(item, '2'))
        self.assertEqual(display_score(item, '1'), 80)
        item['directions'].append(opinion('1->2', None, 2, 'dsh_assisted'))
        self.assertIsNone(display_score(item, '1'))
        self.assertIsNone(display_score(item))

    def test_focus_neighbours_rank_by_evidence_then_interactions(self):
        nodes = {'person:'+str(n): {'last_at': 100-n} for n in range(1, 6)}
        links = [edge('1', '2', events=99), edge('1', '3', [opinion('1->3', 5)], 2),
                 edge('1', '4', [opinion('1->4', 95)], 1), edge('4', '5', events=1000)]
        self.assertEqual(select_nodes(nodes, links, 3, 'person:1'),
                         {'person:1', 'person:3', 'person:4'})
        self.assertEqual(nodes['person:1']['selection_basis']['supported_links'], 2)

    def test_filter_before_selection_keeps_matching_relationship(self):
        nodes = {'person:'+str(n): {'last_at': n} for n in range(1, 6)}
        links = [edge('1', '2', [opinion('1->2', 10)]),
                 edge('1', '3', [opinion('1->3', 10)]),
                 edge('4', '5', [opinion('4->5', 80)])]
        eligible = [item for item in links if display_score(item) >= 50]
        self.assertEqual(select_nodes(nodes, eligible, 2), {'person:4', 'person:5'})
