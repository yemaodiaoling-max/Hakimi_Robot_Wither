from pathlib import Path
import tempfile
import unittest

from bili_companion.policy.video_comments import (
    sufficient_observation,
    specific_comment,
    VideoCommentExecutor,
    ai_cliche_comment,
)
from bili_companion.runtime.storage import Database


class CommentPolicyTests(unittest.TestCase):
    def test_requires_two_chunks_and_union_of_sixty_seconds(self):
        self.assertFalse(sufficient_observation(dict(comment_chunks=2,comment_coverage=[[0,30],[0,30]])))
        self.assertFalse(sufficient_observation(dict(comment_chunks=1,comment_coverage=[[0,60]])))
        self.assertTrue(sufficient_observation(dict(comment_chunks=2,comment_coverage=[[0,30],[30,60]])))

    def test_short_video_must_cover_entire_known_duration(self):
        self.assertTrue(sufficient_observation(dict(duration=20,comment_chunks=1,comment_coverage=[[0,20]])))
        self.assertFalse(sufficient_observation(dict(duration=20,comment_chunks=2,comment_coverage=[[0,5],[10,40]])))
        self.assertFalse(sufficient_observation(dict(comment_chunks=1,comment_coverage=[[0,20]])))
        self.assertFalse(sufficient_observation(dict(duration=20,comment_chunks=1,comment_coverage=[[1,30]])))

    def test_detail_filter_rejects_generic_praise_and_guesses(self):
        self.assertFalse(specific_comment('这一段真让人感动','这一段有钢琴独奏'))
        self.assertFalse(specific_comment('感觉太棒了','钢琴感觉很温暖'))
        self.assertFalse(specific_comment('很棒，好看！','钢琴旋律逐渐升高'))
        self.assertFalse(specific_comment('应该是钢琴独奏','钢琴旋律逐渐升高'))
        self.assertTrue(specific_comment('这一小段钢琴旋律逐渐升高，听着有期待感','钢琴旋律逐渐升高'))

    def test_ai_cliche_and_template_comments_are_rejected(self):
        # AI cliches (substring match)
        self.assertFalse(specific_comment('一针见血，火药味直接拉满了', '一针见血的辩论'))
        self.assertTrue(ai_cliche_comment('一针见血，火药味直接拉满了'))

        # Symmetric recap + cat ear ending (pattern match)
        self.assertFalse(specific_comment('前一秒还在夸，转头就骂，快得我耳朵都差点没转过来', '前一秒还在夸奖'))
        self.assertTrue(ai_cliche_comment('前一秒还在夸，转头就骂，快得我耳朵都差点没转过来'))

        # Multi-comma 3-part recap > 42 chars (without triggering specific cliche substrings/patterns)
        long_recap = '先是把常规赛的稳定压制力拿出来对比，后面又把十年的工龄拉出来辩论，双方你来我往，火药味十足'
        self.assertGreater(len(long_recap.strip()), 42)
        self.assertGreaterEqual(long_recap.count('，') + long_recap.count(','), 3)
        self.assertFalse(specific_comment(long_recap, '常规赛的稳定压制力'))
        self.assertTrue(ai_cliche_comment(long_recap))

        long_recap_2 = '台前信誓旦旦说再也不碰这游戏，背后偷偷练了一整夜连招，结果今天一上线，还是被小怪无情放倒'
        self.assertGreater(len(long_recap_2.strip()), 42)
        self.assertGreaterEqual(long_recap_2.count('，') + long_recap_2.count(','), 3)
        self.assertFalse(specific_comment(long_recap_2, '小怪'))
        self.assertTrue(ai_cliche_comment(long_recap_2))

    def test_natural_grounded_comment_passes(self):
        evidence = 'up主拿扫雷搭建了基础逻辑门'
        comment = '拿扫雷搭逻辑门，以后踩雷都算搞科研了'
        self.assertFalse(ai_cliche_comment(comment))
        self.assertTrue(specific_comment(comment, evidence))

    def test_recent_comments_filters_ai_cliches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / 'test.sqlite3'
            db = Database(db_path)
            executor = VideoCommentExecutor(db, None, enabled=False)
            account_id = 'test_acc'
            records = [
                ('前一秒还在夸，转头就骂，快得我耳朵都差点没转过来', 100),
                ('拿扫雷搭逻辑门，以后踩雷都算搞科研了', 101),
                ('嘴上放狠话要是啃玉米火了就再也不创作，手头倒是真的一秒没歇，嚼得咔咔响，听得我耳朵都跟着动了动。', 102),
                ('一针见血，火药味直接拉满了', 103),
                ('这手速真是一绝，直接省掉重开的步骤了', 104),
            ]
            with db.connect(write=True) as conn:
                for text, at in records:
                    conn.execute(
                        "INSERT INTO video_comment_attempts (account, bvid, session, text, at, day, state) "
                        "VALUES (?, ?, 'sess', ?, ?, '2026-11-07', 'sent')",
                        (account_id, f'BV{at}', text, at)
                    )
            recent = executor.recent_comments(account_id)
            self.assertEqual(recent, [
                '这手速真是一绝，直接省掉重开的步骤了',
                '拿扫雷搭逻辑门，以后踩雷都算搞科研了',
            ])


if __name__ == '__main__':
    unittest.main()
