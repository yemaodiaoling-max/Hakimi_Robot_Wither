import base64
import json
import random
import tempfile
import unittest
from pathlib import Path

from hakimi.core import media_codes as mc

ROOT = '/srv/hk/state/img_cache/'
A = ROOT + 'img_group_100_7001_1.jpg'
B = ROOT + 'img_group_100_7002_1.jpg'
CARD = ('[CQ:json,data={"app":"com.tencent.miniapp"&#44;"prompt":"&#91;QQ小程序&#93;一起看"&#44;'
        '"meta":{"detail_1":{"title":"猫猫合集"&#44;"desc":"猫猫合集 第3期"&#44;'
        '"qqdocurl":"https://b23.tv/AbCd?share_source=qq&amp;spm_id_from=333.1&amp;p=2"&#44;'
        '"preview":"https://pic.example.com/x.jpg"}}}]')
XML = ('[CQ:xml,data=<?xml version="1.0"?><msg brief="&#91;分享&#93;深夜食堂" url="https://example.com/a?utm_source=qq&amp;id=7">'
       '<item><title>深夜食堂 &amp; 小鹿</title><summary>第二季</summary></item></msg>]')
CORPUS = [
    '', '普通一句话', f'题目 [群友发图(可用view_file看图): {A}] 怎么做',
    f'[表情包图片(可用view_file看图):   {B}  ]', '[群友发图(可用view_file看图): /x/y.jpg]',
    '[群友发图(可用view_file看图): \n]', '[群友发图(可用view_file看图): ', f'[CQ:image,file=file:///{A[1:]}]',
    f'[CQ:image,file={A.replace("/", chr(92))},sub_type=1]', '[CQ:image,file=doge.gif,url=https://gchat.qpic.cn/x?rkey=1]',
    '[CQ:image,file=0123456789ABCDEF0123456789ABCDEF.jpg,summary=&#91;动画表情&#93;]', '[CQ:image,url=x]',
    '[CQ:image,file=%E5%9B%BE.png]', '[CQ:record,file=a.amr,url=https://multimedia.nt.qq.com.cn/download?appid=1&fileid=2&rkey=3]',
    '[CQ:video,file=v.mp4,url=https://multimedia.nt.qq.com.cn/download?rkey=9]', '[CQ:file,file=abc,name=期末&#44;复习.pdf,size=3]',
    '[CQ:mface,emoji_id=1,summary=&#91;开心&#93;]', '[CQ:poke,type=1,id=2]', '[CQ:location,lat=1,lon=2,title=图书馆]',
    '[CQ:music,type=custom,title=晴天,url=https://y.example.com]', '[CQ:contact,type=group,id=123456]',
    '[CQ:share,url=https://example.com/p?id=1&amp;utm_medium=x,title=标题,content=摘要]', CARD, XML,
    '[CQ:json,data={"prompt":"\\u8fd9\\u662f\\ud83d\\ude00 \\"q\\" \\n x\\/y \\ud800 bad\\uZZZZ"}]',
    '[CQ:json,data={}]', '[CQ:json]', '[CQ:at,qq=123] [CQ:reply,id=-5] [CQ:face,id=1] [CQ:forward,id=9]',
    '[CQ:markdown,content=# 标题]', '[CQ:unknown,a=b]', '[CQ:image,file=a.jpg',
    '看 https://tieba.baidu.com/p/9105?share=9105&amp;fr=sharewise&amp;see_lz=1 。',
    '链接https://www.bilibili.com/video/BV1xx?t=30&spm_id_from=333.1&vd_source=abc#reply1',
    '(见 https://example.com/wiki/A_(B)) 和 (https://example.com/x)', 'https://example.com/?utm_a=1&utm_b=2',
    'xhttps://example.com/?utm_a=1', 'https://', 'http://a.b', 'HTTPS://EXAMPLE.COM/?UTM_X=1&Q=2',
    'https://user@multimedia.nt.qq.com.cn:443/download?rkey=1', 'https://example.com/a?fr=bar&share=x&share=12',
    '`[CQ:record,file=x]` 中文😀 \x00 tail', '[引用 小鹿 的消息(id=12): "hi [群友发图:无可读缓存]"] 回复',
    '[引用 小鹿 的消息(id=-3): "a"] [引用 小鹿 的消息(id=99): "b"]', '[引用 坏\n名 的消息(id=12): "x"]',
    '[引用 小鹿 的消息(id=12): "没结束', '[引用  的消息(id=): "x"]',
]
TABLE = [A, B]


def native_pair(text, paths, technical):
    lib = mc._library()
    mapping = mc.code_map(paths)
    data = mc._bytes(text)
    table = ''.join(f'{p}\t{c}\n' for p, c in mapping.items()).encode()
    native = mc._call(lib.hk_compact, data, len(data), table, len(table), int(technical))
    python = mc._py_compact(data, {mc._normalize(mc._bytes(p)): c.encode() for p, c in mapping.items()},
                            technical).decode('utf-8', 'replace')
    return native, python


class MediaCodeBehaviour(unittest.TestCase):
    def test_codes_are_short_stable_and_ignore_directory_and_case(self):
        code = mc.image_code(A)
        self.assertRegex(code, r'^[a-hjkmnp-tv-z][0-9a-hjkmnp-tv-z]{3}$')
        self.assertEqual(code, mc.image_code('C:\\cache\\IMG_GROUP_100_7001_1.JPG'))
        self.assertNotEqual(code, mc.image_code(B))
        self.assertEqual(mc._py_image_code(A.encode(), 4), code)

    def test_marker_rows_lose_paths_and_keep_resolvable_codes(self):
        text = mc.compact_media(f'看 [群友发图(可用view_file看图): {A}]', TABLE)
        code = mc.image_code(A)
        self.assertEqual(text, f'看 [群友发图#{code}]')
        for token in (code, '#' + code, code.upper(), f'[群友发图#{code}]'):
            self.assertEqual(mc.resolve_image(token, TABLE), A)
        self.assertIsNone(mc.resolve_image(code, [B]))
        self.assertIsNone(mc.resolve_image('I1', TABLE))
        self.assertEqual(mc.compact_media(f'[群友发图(可用view_file看图): {A}]', [B]), '[群友发图:无可读缓存]')

    def test_code_survives_window_changes(self):
        later = [B, A, ROOT + 'img_group_100_7003_1.jpg']
        self.assertEqual(mc.code_map(TABLE)[A], mc.code_map(later)[A])

    def test_collisions_widen_deterministically(self):
        original = mc.image_code
        try:
            mc.image_code = lambda path, length=4: ('zzzz' if length == 4 else original(path, length))
            codes = mc.code_map(TABLE)
        finally:
            mc.image_code = original
        self.assertEqual(len(set(codes.values())), 2)
        self.assertTrue(all(len(code) == 6 for code in codes.values()))

    def test_media_cards_and_urls(self):
        self.assertEqual(mc.compact_media('[CQ:record,file=a.amr,url=https://multimedia.nt.qq.com.cn/download?rkey=1]'), '[语音]')
        self.assertEqual(mc.compact_media('[CQ:file,file=abc,name=期末&#44;复习.pdf]'), '[文件:期末,复习.pdf]')
        self.assertEqual(mc.compact_media(CARD), '[卡片:[QQ小程序]一起看｜猫猫合集 第3期 链接:https://b23.tv/AbCd?p=2]')
        self.assertEqual(mc.compact_media(XML), '[卡片:[分享]深夜食堂｜深夜食堂 & 小鹿｜第二季 链接:https://example.com/a?id=7]')
        self.assertEqual(mc.compact_media('https://tieba.baidu.com/p/9105?share=9105&amp;fr=sharewise。'),
                         'https://tieba.baidu.com/p/9105。')
        self.assertEqual(mc.compact_media('https://www.bilibili.com/video/BV1?t=30&spm_id_from=1&vd_source=a#r'),
                         'https://www.bilibili.com/video/BV1?t=30#r')
        self.assertEqual(mc.compact_media('https://gchat.qpic.cn/a?rkey=1 看'), '[QQ临时媒体链接] 看')
        self.assertEqual(mc.compact_media('[CQ:image,file=0123456789ABCDEF0123456789ABCDEF.jpg]'),
                         '[表情#' + mc.image_code('0123456789ABCDEF0123456789ABCDEF.jpg') + ']')
        self.assertEqual(mc.compact_media('[CQ:image,file=doge.gif]'), '[表情:doge.gif]')

    def test_technical_mode_keeps_protocol_text_but_never_paths(self):
        raw = f'[CQ:record,file=x] https://e.com/?utm_a=1 [群友发图(可用view_file看图): {A}]'
        out = mc.compact_media(raw, TABLE, technical=True)
        self.assertTrue(out.startswith('[CQ:record,file=x] https://e.com/?utm_a=1 '))
        self.assertNotIn(ROOT, out)

    def test_quote_bodies_of_visible_messages_become_references(self):
        text = '[引用 小鹿 的消息(id=12): "原话 [群友发图#abcd]"] 好耶 [引用 小鹿 的消息(id=13): "旧话"]'
        self.assertEqual(mc.trim_quotes(text, {'12'}), '[引用 小鹿 的消息(id=12)] 好耶 [引用 小鹿 的消息(id=13): "旧话"]')
        self.assertEqual(mc.trim_quotes(text, set()), text)

    def test_python_mirror_used_without_library(self):
        saved = (mc._LIBRARY, mc._LOADED)
        try:
            mc._LIBRARY, mc._LOADED = None, True
            self.assertEqual(mc.compact_media('[CQ:video,file=v]'), '[视频]')
        finally:
            mc._LIBRARY, mc._LOADED = saved


@unittest.skipUnless(mc.native_available(), 'libhkmedia not built')
class NativeParity(unittest.TestCase):
    def test_corpus_parity(self):
        for text in CORPUS:
            for technical in (False, True):
                native, python = native_pair(text, TABLE, technical)
                self.assertEqual(native, python, (text, technical))

    def test_random_fragment_parity(self):
        rng = random.Random(20261128)
        pieces = CORPUS + ['[', ']', '"', '"]', '\n', ',', '&#44;', '&amp;', '%', '%4', '%E5', 'http', '://',
                           '?', '&', '#', ')', '(', '。', '😀', '\\', '\x7f', '[CQ:', '[引用 ', ' 的消息(id=', '): "']
        ids = {'12', '-3', '7'}
        for _ in range(3000):
            text = ''.join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
            native, python = native_pair(text, TABLE, rng.random() < .2)
            self.assertEqual(native, python, text)
            data = mc._bytes(text)
            raw = '\n'.join(sorted(ids)).encode()
            self.assertEqual(mc._call(mc._library().hk_trim_quotes, data, len(data), raw, len(raw)),
                             mc._py_trim_quotes(data, {i.encode() for i in ids}).decode('utf-8', 'replace'), text)

    def test_code_parity(self):
        for path in [A, B, '', 'x', 'C:\\A\\B.PNG', '图片.jpg', 'a/b/c/d.gif'] + [f'img_{i}.jpg' for i in range(500)]:
            for n in (3, 4, 6, 12):
                self.assertEqual(mc.image_code(path, n), mc._py_image_code(mc._bytes(path), n))

    def test_large_output_grows_buffer(self):
        text = '[CQ:poke]' * 5000
        self.assertEqual(mc.compact_media(text), '[戳一戳]' * 5000)


if __name__ == '__main__':
    unittest.main()
