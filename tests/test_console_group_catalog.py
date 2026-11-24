import json
import sqlite3
import tempfile
import pathlib
import unittest
from unittest.mock import patch
from hakimi.console import group_catalog as m

class Names(unittest.TestCase):

    def test_names_mapping_privacy_and_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            base = root / 'official_test_runtime'
            (base / 'state').mkdir(parents=True)
            (root / 'config.json').write_text(json.dumps({'group_weights': {'77': {'name': '旧群真名'}}}), encoding='utf8')
            (base / 'config.json').write_text(json.dumps({'group_weights': {'2': {'name': '同名'}, '3': {'name': '同名'}, '4': {'name': '群 ID 4'}, '6': {'name': '123456789'}}}), encoding='utf8')
            db = sqlite3.connect(base / 'state/official_routes.sqlite3')
            db.execute('create table settings(key text,value text)')
            db.execute('insert into settings values(?,?)', ('group_routes', json.dumps({'SECRET_OPENID': {'local_id': '1', 'legacy_group': '77', 'openid': 'SECRET_OPENID', 'name': ''}, 'other': {'local_id': '5', 'name': '官方真名', 'style_source': '77'}})))
            db.commit()
            db.close()
            result = m.group_metadata(base, ['group:1', 'group:2', 'group:3', 'group:4', 'group:5', 'group:6', 'private:99', 'group:bad'])
            self.assertEqual(result['group:1']['name'], '旧群真名')
            self.assertEqual(result['group:5']['name'], '官方真名')
            self.assertNotEqual(result['group:2']['label'], result['group:3']['label'])
            self.assertEqual(result['group:4']['name'], '')
            self.assertEqual(result['group:6']['name'], '')
            self.assertNotIn('SECRET_OPENID', json.dumps(result))
            self.assertNotIn('private:99', result)
            self.assertEqual(m.group_metadata(base, ['group:4'])['group:4']['label'], result['group:4']['label'])

    def test_missing_database_does_not_create(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(m.group_metadata(d, ['group:1'])['group:1']['name_source'], 'unknown')
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])

    def test_identity_projection_preserves_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'config.json').write_text(json.dumps({'group_weights': {'1': {'name': '真实名字'}}}), encoding='utf-8')
            data = {'old_group': '1', 'new_group': '2', 'identity_label': '旧群1→新群2', 'old_user': '88'}
            result = m.identity_display(root, data)
            self.assertEqual(result['old_group'], '1')
            self.assertEqual(result['new_group'], '2')
            self.assertEqual(result['old_user'], '88')
            self.assertIn('真实名字', result['identity_label'])
            self.assertNotIn('新群2', result['identity_label'])
            self.assertEqual(data['identity_label'], '旧群1→新群2')
class ConsistentLabels(unittest.TestCase):

    def test_duplicate_label_is_same_in_single_group_and_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'config.json').write_text(json.dumps({
                'group_weights': {'1': {'name': '同名'}, '2': {'name': '同名'}}
            }), encoding='utf-8')
            full = m.group_metadata(root, ['group:1', 'group:2'])
            single = m.group_metadata(root, ['group:1'])
            self.assertEqual(single['group:1']['label'], full['group:1']['label'])
            self.assertNotEqual(single['group:1']['label'], '同名')

    def test_empty_or_private_sessions_do_not_read_sources(self):
        with patch.object(m, '_configured', side_effect=AssertionError('unexpected read')):
            self.assertEqual(m.group_metadata('unused', []), {})
            self.assertEqual(m.group_metadata('unused', ['private:1']), {})
            self.assertEqual(m.identity_display('unused', {'value': 1}), {'value': 1})

    def test_same_root_config_is_read_once_for_identity_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(m, '_configured', return_value={}) as configured:
                m.identity_display(directory, {'old_group': '1', 'new_group': '2'})
            self.assertEqual(configured.call_count, 1)


if __name__ == '__main__':
    unittest.main()
