"""Read-only console message scope and incremental JSONL regression tests."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hakimi.console.message_log import _TAIL_CACHE, _attach_relation_people, _reverse_lines, _trailing_start, archived_messages, scoped_tail


class MessageScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.jsonl'
        _TAIL_CACHE.clear()

    def row(self, message, session='private:7'):
        return json.dumps({'session_key': session, 'message_id': message}).encode() + b'\n'

    def messages(self):
        return [r['message_id'] for r in scoped_tail(self.path, 3, 'private')]

    def test_sparse_scope_filters_before_limit(self):
        self.path.write_bytes(self.row('private') + b''.join(self.row(str(i), 'group:9') for i in range(2200)))
        self.assertEqual(self.messages(), ['private'])
        self.assertTrue(all(r['session_key'] == 'group:9' for r in scoped_tail(self.path, 3, 'group')))

    def test_append_and_partial_final_line(self):
        self.path.write_bytes(self.row('first'))
        self.assertEqual(self.messages(), ['first'])
        partial = self.row('second')
        with self.path.open('ab') as stream:
            stream.write(partial[:-5])
        self.assertEqual(self.messages(), ['first'])
        with self.path.open('ab') as stream:
            stream.write(partial[-5:] + self.row('third'))
        self.assertEqual(self.messages(), ['first', 'second', 'third'])

    def test_cold_incomplete_line_then_completion(self):
        self.path.write_bytes(self.row('first') + self.row('second')[:-5])
        self.assertEqual(self.messages(), ['first'])
        with self.path.open('ab') as stream:
            stream.write(self.row('second')[-5:])
        self.assertEqual(self.messages(), ['first', 'second'])

    def test_valid_unterminated_record_is_not_duplicated(self):
        self.path.write_bytes(self.row('first') + self.row('second')[:-1])
        self.assertEqual(self.messages(), ['first', 'second'])
        self.assertEqual(self.messages(), ['first', 'second'])
        with self.path.open('ab') as stream:
            stream.write(b'\n' + self.row('third'))
        self.assertEqual(self.messages(), ['first', 'second', 'third'])

    def test_large_partial_record_resumes_from_its_real_boundary(self):
        large = self.row('x' * 150000)
        self.path.write_bytes(self.row('first') + large[:-5])
        self.assertEqual(self.messages(), ['first'])
        with self.path.open('ab') as stream:
            stream.write(large[-5:])
        self.assertEqual(self.messages(), ['first', 'x' * 150000])
        _TAIL_CACHE.clear()
        self.path.write_bytes(large[:-1])
        self.assertEqual(self.messages(), ['x' * 150000])
        with self.path.open('ab') as stream:
            stream.write(b'\n')
        self.assertEqual(self.messages(), ['x' * 150000])

    def test_helpers_obey_snapshot_size_during_append(self):
        original = self.row('original')
        stream = io.BytesIO(original + self.row('appended') + b'incomplete')
        self.assertEqual(_trailing_start(stream, len(original)), len(original))
        self.assertEqual(list(_reverse_lines(stream, len(original))), [original[:-1]])

    def test_truncate_and_rotation(self):
        self.path.write_bytes(self.row('long-original-name'))
        self.assertEqual(self.messages(), ['long-original-name'])
        self.path.write_bytes(self.row('short'))
        self.assertEqual(self.messages(), ['short'])
        replacement = self.path.with_suffix('.new')
        replacement.write_bytes(self.row('rotated'))
        os.replace(replacement, self.path)
        self.assertEqual(self.messages(), ['rotated'])

    def test_relation_links_require_group_visible_records(self):
        with sqlite3.connect(self.path.parent / 'memory.sqlite3') as db:
            db.execute('CREATE TABLE records(user_id, state, session)')
            db.executemany('INSERT INTO records VALUES(?,?,?)', [
                ('7', 'active', 'group:9'), ('8', 'active', 'private:8'),
                ('10', 'deleted', 'group:9'),
            ])
        db.close()
        rows = [dict(kind=kind, user_id=user, session='private:' + user)
                for user in ['7', '8', '10'] for kind in ['received', 'sent']]
        _attach_relation_people(self.path.parent, rows)
        self.assertEqual([r['relation_person_id'] for r in rows], ['7', '7', '', '', '', ''])
        self.assertFalse(any('_relation_candidate' in row for row in rows))

    def test_archived_originals_are_scoped_and_deleted_excluded(self):
        with sqlite3.connect(self.path.parent / 'memory.sqlite3') as db:
            db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY, kind, state, session, origin, user_id, text, at, nickname)')
            for index, (session, state) in enumerate([('private:7', 'active'), ('group:9', 'active'), ('private:8', 'deleted')]):
                db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)', (index, 'chat', state, session, str(index), '7', 'synthetic original', index, 'Test'))
        db.close()
        self.assertEqual([r['session_key'] for r in archived_messages(self.path.parent, scope='private')], ['private:7'])
        self.assertEqual([r['session_key'] for r in archived_messages(self.path.parent)], ['group:9'])


if __name__ == '__main__':
    unittest.main()
