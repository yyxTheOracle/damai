"""可选的本机历史帧回放；仓库独立回归使用 test_button_ready.py 的合成画面。"""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import Mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from button_ready import ButtonMatcher


FIXTURES = Path(__file__).parent / 'fixtures' / 'button_text'


class CrossShowButtonTests(unittest.TestCase):
    @unittest.skipUnless(FIXTURES.is_dir(), "本机未保留历史按钮裁图")
    def test_every_calibrated_show_releases_its_first_native_frame_without_ocr(self):
        """每场从全新 matcher 开始，避免前一个样本的 OCR 学习掩盖首帧未命中。"""
        samples = (
            ('buy_original_20261117.png', '立即购票'),
            ('buy_xuezhiqian_first_20261117.png', '立即购票'),
            ('buy_xuezhiqian_last_20261117.png', '立即购票'),
            ('book_first_20261117.png', '立即预订'),
            ('book_stable_20261117.png', '立即预订'),
        )
        for name, expected in samples:
            with self.subTest(sample=name):
                reader = Mock(error=None)
                reader.read.side_effect = AssertionError('已标定购买样本不得回退 OCR')
                matcher = ButtonMatcher(reader)
                with Image.open(FIXTURES / name) as button:
                    self.assertEqual(matcher.match(button), expected)
                self.assertEqual(matcher.method, 'template')
                reader.read.assert_not_called()

    @unittest.skipUnless(FIXTURES.is_dir(), "本机未保留历史按钮裁图")
    def test_reserved_frames_from_both_sales_do_not_match_purchase_templates(self):
        """负样本 OCR 仅返回不可购买；一旦误中任一购买模板，本测试必然失败。"""
        for name in ('reserved_20261117.png', 'reserved_xuezhiqian_20261117.png'):
            with self.subTest(sample=name):
                reader = Mock(error=None, last_text='已预约')
                reader.read.return_value = None
                matcher = ButtonMatcher(reader)
                with Image.open(FIXTURES / name) as button:
                    self.assertIsNone(matcher.match(button))
                    self.assertIsNone(matcher.match(button))

    def test_local_recorded_streams(self):
        """原记录存在时按原次序回放全部帧，在本地验证两场的首次购买帧。"""
        repo = Path(__file__).resolve().parent.parent
        cases = (('run_20261117_155745_389282', 7790, '立即预订'),
                 ('run_20261117_170806_241662', 31810, '立即购票'))
        if not all((repo / 'artifacts' / run / 'button_frames/index.json').is_file() for run, _, _ in cases):
            self.skipTest('完整实战记录仅保存在原运行机器；独立样本回归仍执行')
        for run, first_purchase, text in cases:
            path = repo / 'artifacts' / run
            index = json.loads((path / 'button_frames/index.json').read_text())
            left, top, right, bottom = index['bounds']
            sequences = [frame['sequence'] for frame in index['frames']]
            self.assertIn(first_purchase - 1, sequences)
            self.assertIn(first_purchase, sequences)
            reader = Mock(error=None, last_text='已预约')
            reader.read.return_value = None
            matcher = ButtonMatcher(reader)
            for frame in index['frames']:
                expected = text if frame['sequence'] >= first_purchase else None
                calls_before = reader.read.call_count
                with Image.open(path / frame['path']) as image:
                    button = image.crop((left - 340, top - 2428, right - 340, bottom - 2428))
                    self.assertEqual(matcher.match(button), expected, (run, frame['sequence']))
                if expected:
                    self.assertEqual(matcher.method, 'template', (run, frame['sequence']))
                    self.assertEqual(reader.read.call_count, calls_before)


if __name__ == '__main__':
    unittest.main()
