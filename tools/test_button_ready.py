"""纯离线验证购买文案放行：合成图片和假视频流，不调用 ADB 或 OCR 进程。"""
from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import button_ready as module


DETAIL = f'''<hierarchy><node package="cn.damai" enabled="true" clickable="false"
  resource-id="{module.DETAIL_RESOURCE}" bounds="[341,2429][1168,2576]"/></hierarchy>'''


def sample_label(offset=(30, 20), subtitle=False):
    """用两块不同宽度的白色字形检验裁剪；不需要中文字体或私人手机截图。"""
    image = Image.new("RGB", (240, 100), (255, 100, 0))
    draw = ImageDraw.Draw(image)
    x, y = offset
    draw.rectangle((x, y, x + 15, y + 29), fill="white")
    draw.rectangle((x + 30, y, x + 37, y + 29), fill="white")
    if subtitle:
        draw.rectangle((10, 80, 220, 85), fill="white")
    return image


class ButtonMatcherTests(unittest.TestCase):
    def test_calibrated_glyphs_on_synthetic_buttons_without_phone_captures(self):
        """重新绘制四种已标定字形，验证提取、匹配及镜像字形拒绝，不依赖设备或字体。"""
        assets = Path(module.__file__).parent / "assets" / "button_text"
        samples = (("buy_now.png", "立即购票"), ("buy_now_xuezhiqian.png", "立即购票"),
                   ("book_now_first.png", "立即预订"), ("book_now_stable.png", "立即预订"))
        for name, expected in samples:
            with self.subTest(sample=name):
                with Image.open(assets / name) as glyph:
                    # 放大并抖动为黑白字形，重新合成画面后由生产代码独立提取灰度模板。
                    mask = glyph.resize((480, 120), Image.Resampling.NEAREST).convert("1")
                button = Image.new("RGB", (600, 150), "orange")
                button.paste("white", (40, 15, 520, 135), mask)
                reader = Mock(error=None, last_text=None)
                matcher = module.ButtonMatcher(reader)
                self.assertEqual(matcher.match(button), expected)
                self.assertEqual(matcher.last_match["matched_template"], name)
                reader.read.assert_not_called()

                # 镜像保留笔画密度和大小，但文字方向不同，识别器必须先问 OCR。
                reader.read.return_value = None
                self.assertIsNone(matcher.match(button.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))
                self.assertEqual(matcher.method, "ocr")
                reader.read.assert_called_once()

    def test_mask_ignores_background_translation_and_smaller_subtitle(self):
        plain = module.label_mask(sample_label())
        shifted = module.label_mask(sample_label((100, 35), subtitle=True))
        self.assertEqual(plain.size, (160, 40))
        self.assertEqual(plain.tobytes(), shifted.tobytes())

    def test_blank_and_tiny_text_have_no_matchable_mask(self):
        self.assertIsNone(module.label_mask(Image.new("RGB", (200, 100), "orange")))
        tiny = Image.new("RGB", (200, 100), "orange")
        ImageDraw.Draw(tiny).rectangle((5, 5, 100, 10), fill="white")
        self.assertIsNone(module.label_mask(tiny))

    def test_native_template_fast_path_does_not_call_ocr(self):
        reader = Mock(error=None)
        matcher = module.ButtonMatcher(reader)
        # 字形提取单独验证；这里直接喂入仓库原生模板，检验模板放行不依赖 OCR。
        with patch.object(module, "label_mask", return_value=matcher.templates[0][0].copy()):
            self.assertEqual(matcher.match(sample_label()), "立即购票")
        self.assertEqual(matcher.method, "template")
        reader.read.assert_not_called()

    def test_reserved_text_is_cached_as_negative_without_repeated_ocr(self):
        reader = Mock(error=None, last_text="已预约")
        reader.read.return_value = None
        matcher = module.ButtonMatcher(reader)
        self.assertIsNone(matcher.match(sample_label()))
        self.assertIsNone(matcher.match(sample_label((100, 35), subtitle=True)))
        self.assertEqual(matcher.method, "template")
        reader.read.assert_called_once()

    def test_unrecognized_text_is_retried_and_new_positive_can_release(self):
        reader = Mock(error=None, last_text=None)
        reader.read.side_effect = [None, "立即预订"]
        matcher = module.ButtonMatcher(reader)
        self.assertIsNone(matcher.match(sample_label()))
        reader.last_text = "立即预订"
        self.assertEqual(matcher.match(sample_label()), "立即预订")
        self.assertEqual(reader.read.call_count, 2)
        self.assertEqual(matcher.match(sample_label()), "立即预订")
        self.assertEqual(reader.read.call_count, 2)

    def test_ocr_typo_does_not_cache_same_image_as_permanently_unavailable(self):
        """同一画面首次识别错字，下一次准确识别仍应有机会放行。"""
        reader = Mock(error=None, last_text="立即预定")
        reader.read.side_effect = [None, "立即预订"]
        matcher = module.ButtonMatcher(reader)
        image = sample_label()
        self.assertIsNone(matcher.match(image))
        reader.last_text = "立即预订"
        self.assertEqual(matcher.match(image), "立即预订")
        self.assertEqual(reader.read.call_count, 2)
        self.assertEqual(matcher.method, "ocr")

    def test_ocr_failure_raises_without_caching_a_positive(self):
        reader = Mock(error="OCR disconnected", last_text="立即预订")
        reader.read.return_value = "立即预订"
        matcher = module.ButtonMatcher(reader)
        templates_before = list(matcher.templates)
        with self.assertRaisesRegex(RuntimeError, "OCR disconnected"):
            matcher.match(sample_label())
        self.assertEqual(matcher.templates, templates_before)

    def test_ocr_start_end_and_error_are_recorded_with_source_frame(self):
        """OCR 失败也留起止证据，不能只记录后面那次成功模板匹配。"""
        reader = Mock(error="OCR disconnected", last_text=None)
        reader.read.return_value = None
        matcher = module.ButtonMatcher(reader)
        with patch.object(module.run_log, 'emit') as log:
            with self.assertRaisesRegex(RuntimeError, 'OCR disconnected'):
                matcher.match(sample_label(), frame_sequence=37)
        self.assertEqual([c.args[0] for c in log.call_args_list],
                         ['button_ocr_started', 'button_ocr_finished'])
        self.assertTrue(all(c.kwargs['frame_sequence'] == 37 for c in log.call_args_list))
        self.assertEqual(log.call_args.kwargs['error'], 'OCR disconnected')
        self.assertGreaterEqual(matcher.last_match['ocr_ms'], 0)

    def test_learned_negative_frames_cannot_evict_calibrated_purchase_templates(self):
        """长时间候场产生新负样本时，保留所有购票/预订首击模板。"""
        reader = Mock(error=None, last_text="已预约")
        reader.read.return_value = None
        matcher = module.ButtonMatcher(reader)
        calibrated = list(matcher.templates)
        masks = [Image.new("L", (160, 40), value) for value in range(20, 220, 20)]
        with patch.object(module, "label_mask", side_effect=masks):
            for _ in masks:
                self.assertIsNone(matcher.match(sample_label()))
        self.assertEqual(matcher.templates[:len(calibrated)], calibrated)
        self.assertLessEqual(len(matcher.templates), 8)


class PurchaseBoundsTests(unittest.TestCase):
    def test_full_width_and_split_button_only_return_purchase_region(self):
        for left in (341, 486):
            with self.subTest(left=left):
                xml = DETAIL.replace("[341,2429]", f"[{left},2429]")
                self.assertEqual(module.purchase_bounds(xml), (left, 2429, 1168, 2576))

    def test_hidden_disabled_moved_duplicate_and_overlay_buttons_are_rejected(self):
        overlay = '<node package="com.android.permissioncontroller" bounds="[0,300][1200,1800]"/>'
        cases = [DETAIL.replace('enabled="true"', 'enabled="false"'),
                 DETAIL.replace('enabled="true"', 'enabled="true" visible-to-user="false"'),
                 DETAIL.replace('[341,2429]', '[300,2429]'),
                 DETAIL.replace('[341,2429]', '[950,2429]'),
                 DETAIL.replace('</hierarchy>', DETAIL.split('<hierarchy>')[1]),
                 DETAIL.replace('</hierarchy>', overlay + '</hierarchy>')]
        for xml in cases:
            with self.subTest(xml=xml), self.assertRaises(RuntimeError):
                module.purchase_bounds(xml)


class PurchaseWatchTests(unittest.TestCase):
    def watch(self, frames=(), labels=("立即预订",), *, deadline=20, warm_error=None,
              during_match=None, stopped=False, record=False, cleanup_seconds=0):
        """控制采集时间与取消时机，真实执行 watch；退出采集时记录是否已发放行信号。"""
        now = [10.0]
        ready, stop, state = threading.Event(), threading.Event(), Mock()
        if stopped:
            stop.set()
        reader = Mock(error=warm_error)
        reader_context = MagicMock()
        reader_context.__enter__.return_value = reader
        stream, stream_context = Mock(), MagicMock()
        stream_context.__enter__.return_value = stream
        stream.recent_frames.side_effect = lambda: [frame for frame in frames if frame[1] >= now[0] - 2]
        recorder = Mock() if record else None
        retained_after_ready = []
        if recorder is not None:
            recorder.keep_button_frames.side_effect = lambda *_: retained_after_ready.append(ready.is_set())
        cleaned = []
        def cleanup(*_):
            cleaned.append(ready.is_set())
            now[0] += cleanup_seconds

        stream_context.__exit__.side_effect = cleanup
        pending = iter(frames)

        def latest(*_args, **_kwargs):
            value = next(pending, None)
            if value is None:
                stop.set()
                return None
            if isinstance(value, Exception):
                raise value
            return value

        stream.latest.side_effect = latest
        matcher = Mock(method="template", last_match={})
        answers = iter(labels)

        def match(_image, **_kwargs):
            if during_match:
                during_match(now, stop)
            return next(answers)

        matcher.match.side_effect = match
        device = Mock()
        device.dump_hierarchy.return_value = DETAIL
        device.screenshot.return_value = Image.new("RGB", (1200, 2670))
        with ExitStack() as stack:
            stack.enter_context(patch.object(module.time, "perf_counter", side_effect=lambda: now[0]))
            stack.enter_context(patch.object(module, "ButtonTextReader", return_value=reader_context))
            stream_factory = stack.enter_context(patch.object(module, "ButtonStream", return_value=stream_context))
            stack.enter_context(patch.object(module, "ButtonMatcher", return_value=matcher))
            log = stack.enter_context(patch.object(module.run_log, "emit"))
            stack.enter_context(patch.object(module.run_log, "active", recorder))
            module.watch_purchase_button(device, Path("/test/adb"), "test", ready, stop, state, deadline)
        return SimpleNamespace(ready=ready, stop=stop, state=state, matcher=matcher,
                               stream=stream, stream_factory=stream_factory, cleaned=cleaned,
                               recorder=recorder, retained_after_ready=retained_after_ready, log=log)

    @staticmethod
    def frame(sequence=1, received=10):
        """大小与裁剪协议相同，内容交给假 matcher，避免测试触发真实图片识别。"""
        return sequence, received, Image.new("RGB", (828, 148), "orange")

    def test_ready_is_published_before_stream_cleanup(self):
        result = self.watch([self.frame()])
        self.assertTrue(result.ready.is_set())
        self.assertEqual(result.cleaned, [True])
        self.assertFalse(result.stop.is_set())
        result.state.finish.assert_not_called()
        self.assertEqual(result.matcher.match.call_args.args[0].size, (827, 147))

    def test_reserved_or_missing_frames_do_not_release(self):
        for frames in ([], [self.frame()]):
            with self.subTest(frames=len(frames)):
                result = self.watch(frames, labels=(None,))
                self.assertFalse(result.ready.is_set())

    def test_history_is_retained_only_after_ready_without_writing_images(self):
        """点击已放行才把缓存交给日志器；识别线程不编码或保存任何图片。"""
        frame = self.frame()
        with patch.object(Image.Image, 'save', side_effect=AssertionError('抢票途中不能写图')):
            result = self.watch([frame], record=True)
        self.assertEqual(result.retained_after_ready, [True])
        result.recorder.keep_button_frames.assert_called_once_with([frame], 1, (341, 2429, 1168, 2576))

    def test_stale_frame_is_skipped_but_next_fresh_positive_releases(self):
        result = self.watch([self.frame(received=9), self.frame(2)])
        self.assertTrue(result.ready.is_set())
        result.matcher.match.assert_called_once()
        self.assertEqual(result.stream.latest.call_args.args, (1,))

    def test_trace_records_stale_frame_and_latest_frame_gap(self):
        """过期帧和只处理最新帧导致的序号跳跃都必须可追溯。"""
        result = self.watch([self.frame(2, 9), self.frame(5, 10)])
        skipped = [c.kwargs for c in result.log.call_args_list if c.args[0] == 'button_frame_skipped']
        processed = [c.kwargs for c in result.log.call_args_list if c.args[0] == 'button_frame_processed']
        self.assertEqual(skipped[0]['reason'], 'stale_before_match')
        self.assertEqual(skipped[0]['skipped_frames'], 1)
        self.assertEqual(processed[0]['skipped_frames'], 2)
        self.assertEqual(processed[0]['decision'], 'ready')
        self.assertEqual(processed[0]['frame_sequence'], 5)

    def test_slow_positive_then_fast_match_keeps_both_attempts(self):
        """复现这次慢识别帧被丢弃、下一帧快速放行，日志必须保留前一轮耗时。"""
        result = self.watch([self.frame(1, 10), self.frame(19, 10.3)],
                            labels=('立即购票', '立即购票'),
                            during_match=lambda now, _stop: now.__setitem__(0, 10.3))
        processed = [c.kwargs for c in result.log.call_args_list if c.args[0] == 'button_frame_processed']
        self.assertEqual([r['decision'] for r in processed], ['stale_after_match', 'ready'])
        self.assertAlmostEqual(processed[0]['match_ms'], 300)
        self.assertEqual(processed[1]['skipped_frames'], 17)
        self.assertTrue(result.ready.is_set())

    def test_cancelled_watch_still_retains_recent_frames_without_ready_image(self):
        """没有等到放行就取消也保留最后画面，方便复盘未触发原因。"""
        frame = self.frame()
        result = self.watch([frame], labels=(None,), record=True, cleanup_seconds=3)
        self.assertFalse(result.ready.is_set())
        result.recorder.keep_button_frames.assert_called_once_with([frame], None, (341, 2429, 1168, 2576))

    def test_real_matcher_and_writer_keep_slow_ocr_before_learned_template(self):
        """串联真实 matcher/watch/异步文件日志，重现慢 OCR 丢帧后快匹配的完整证据。"""
        now = [10.0]
        ready, stop = threading.Event(), threading.Event()
        reader = SimpleNamespace(error=None, last_text=None)
        reads = []

        def read(_image):
            reads.append(now[0])
            if len(reads) == 1:
                reader.last_text = '已预约'
                return None  # 仅预热，随后用无内置模板的合成字形强制走 OCR。
            now[0] = 10.3
            reader.last_text = '立即购票'
            return reader.last_text

        reader.read = read
        reader_context, stream_context = MagicMock(), MagicMock()
        reader_context.__enter__.return_value = reader
        picture = Image.new('RGB', (828, 148), 'orange')
        picture.paste(sample_label(), (1, 1))
        frames = [(1, 10.0, picture), (19, 10.3, picture)]
        stream = Mock()
        stream.latest.side_effect = frames
        stream.recent_frames.return_value = frames
        stream_context.__enter__.return_value = stream
        device = Mock()
        device.dump_hierarchy.return_value = DETAIL
        device.screenshot.return_value = Image.new('RGB', (1200, 2670))
        with tempfile.TemporaryDirectory() as temporary:
            recorder = module.run_log.RunLog(Path(temporary) / 'run', datetime.now())
            try:
                with patch.object(module.run_log, 'active', recorder), \
                        patch.object(module.time, 'perf_counter', side_effect=lambda: now[0]), \
                        patch.object(module, 'ButtonTextReader', return_value=reader_context), \
                        patch.object(module, 'ButtonStream', return_value=stream_context):
                    module.watch_purchase_button(device, Path('/fake/adb'), 'test', ready, stop, Mock(), 20)
            finally:
                recorder.close()
            self.assertEqual(recorder.errors, [])
            events = [json.loads(line) for line in (recorder.directory / 'events.jsonl').read_text().splitlines()]
            attempts = [e for e in events if e['event'] == 'button_frame_processed']
            self.assertEqual([e['decision'] for e in attempts], ['stale_after_match', 'ready'])
            self.assertEqual(attempts[0]['ocr_ms'], 300)
            self.assertEqual(attempts[1]['template_source'], 'ocr_learned')
            self.assertEqual(attempts[1]['skipped_frames'], 17)
            self.assertEqual(sum(e['event'] == 'button_ocr_started' for e in events), 1)
            self.assertEqual(sum(e['event'] == 'button_ocr_finished' for e in events), 1)
            self.assertTrue(ready.is_set())

    def test_frame_aging_during_recognition_cannot_release(self):
        result = self.watch([self.frame()], during_match=lambda now, _stop: now.__setitem__(0, 10.3))
        self.assertFalse(result.ready.is_set())

    def test_cancel_before_or_during_recognition_cannot_release(self):
        before = self.watch([self.frame()], stopped=True)
        during = self.watch([self.frame()], during_match=lambda _now, stop: stop.set())
        self.assertFalse(before.ready.is_set())
        self.assertFalse(during.ready.is_set())
        before.matcher.match.assert_not_called()

    def test_deadline_stops_without_inspecting_or_releasing_frame(self):
        result = self.watch([self.frame()], deadline=10)
        self.assertFalse(result.ready.is_set())
        self.assertTrue(result.stop.is_set())
        result.state.finish.assert_called_once_with("timeout")
        result.matcher.match.assert_not_called()

    def test_deadline_crossed_during_recognition_cannot_release(self):
        result = self.watch([self.frame()], deadline=10.1,
                            during_match=lambda now, _stop: now.__setitem__(0, 10.15))
        self.assertFalse(result.ready.is_set())
        self.assertTrue(result.stop.is_set())
        result.state.finish.assert_called_once_with("timeout")

    def test_stream_error_and_warmup_error_stop_without_ready(self):
        for options in ({"frames": [RuntimeError("USB disconnected")]}, {"warm_error": "OCR failed"}):
            with self.subTest(options=options):
                result = self.watch(**options)
                self.assertFalse(result.ready.is_set())
                self.assertTrue(result.stop.is_set())
                self.assertEqual(result.state.finish.call_args.args[0], "error")
                if "warm_error" in options:
                    result.stream_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
