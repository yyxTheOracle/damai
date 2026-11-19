"""离线验证按钮文字白名单、持久进程协议和取消边界，不编译 Swift 或连接手机。"""

import base64
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import button_text


class ButtonTextTests(unittest.TestCase):
    def reader(self, stop_event=None):
        """只替换外部子进程，read/_exchange 的生产逻辑仍真实执行。"""
        reader = button_text.ButtonTextReader(stop_event=stop_event)
        process = Mock()
        process.poll.return_value = None
        process.stdin.fileno.return_value = 11
        process.stdout.fileno.return_value = 12
        reader.process = process
        self.addCleanup(reader.close)
        return reader, process

    def test_only_exact_primary_line_purchase_labels_are_allowed(self):
        """拒绝副标题、相似文案和拆开的 OCR 片段，不能靠包含购买关键词放行。"""
        cases = (
            (["立即预订"], "立即预订"), (["立即购票"], "立即购票"),
            (["立即购买"], "立即购买"), ([" 立即购票\n", "其他说明"], "立即购票"),
            (["已预约", "立即购票"], None), (["立即购票更多"], None),
            (["准备立即预订"], None), (["立即", "购票"], None),
            (["立即 购买"], None), (["立即預訂"], None), ([""], None), ([], None),
        )
        for texts, expected in cases:
            with self.subTest(texts=texts):
                reader, _ = self.reader()
                with patch.object(reader, "_exchange", return_value=json.dumps(texts).encode()):
                    self.assertEqual(reader.read(Image.new("RGB", (2, 2))), expected)
                self.assertEqual(reader.last_text, (texts[0].strip() or None) if texts else None)
                self.assertIsNone(reader.error)

    def test_first_read_warms_process_and_next_read_uses_short_timeout(self):
        """首次无购买文案也完成预热，后续识别不能再使用 60 秒冷启动等待。"""
        reader, _ = self.reader()
        with patch.object(reader, "_exchange", side_effect=[b'[]\n', '["立即购票"]\n'.encode()]) as exchange:
            self.assertIsNone(reader.read(Image.new("RGB", (2, 2))))
            self.assertTrue(reader.warmed)
            self.assertEqual(reader.read(Image.new("RGB", (2, 2))), "立即购票")
        self.assertEqual([call.args[1] for call in exchange.call_args_list], [60, 2])
        payload = exchange.call_args_list[0].args[0]
        self.assertTrue(payload.endswith(b"\n"))
        self.assertTrue(base64.b64decode(json.loads(payload)).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_verification_reader_returns_both_lines(self):
        """苏州验证页的两行文案都需要保留，且不能成为按钮放行结果。"""
        reader, _ = self.reader()
        texts = ["操作过于频繁", "完成验证后可继续购票"]
        with patch.object(reader, "_exchange", return_value=json.dumps(texts).encode()):
            self.assertEqual(reader.read_lines(Image.new("RGB", (2, 2))), texts)
            self.assertIsNone(reader.read(Image.new("RGB", (2, 2))))

    def test_protocol_and_transport_errors_close_process_and_clear_previous_text(self):
        """坏协议或管道异常不能沿用上一次购买文案，并必须回收进程与管道。"""
        cases = (b'{"text":"ready"}', b'[null]', b'[1]', b'broken json',
                 TimeoutError("timeout"), InterruptedError("cancelled"),
                 BrokenPipeError("closed"), EOFError("ended"))
        for reply in cases:
            with self.subTest(reply=reply):
                reader, process = self.reader()
                reader.last_text = "立即购票"
                kwargs = {"side_effect": reply} if isinstance(reply, Exception) else {"return_value": reply}
                with patch.object(reader, "_exchange", **kwargs):
                    self.assertIsNone(reader.read(Image.new("RGB", (2, 2))))
                self.assertIsNone(reader.last_text)
                self.assertTrue(reader.error)
                self.assertIsNone(reader.process)
                process.terminate.assert_called_once()
                process.stdin.close.assert_called_once()
                process.stdout.close.assert_called_once()

    def test_close_is_idempotent_and_kills_unresponsive_process(self):
        """进程拒绝 terminate 时强制结束，重复关闭不能重复操作旧句柄。"""
        reader, process = self.reader()
        process.wait.side_effect = [button_text.subprocess.TimeoutExpired("ocr", 1), None]
        reader.close()
        reader.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_partial_writes_and_fragmented_reply_complete_in_order(self):
        """真实管道可能只收发部分字节，必须等完整 JSON 行而不是丢失片段。"""
        reader, _ = self.reader()
        readiness = [([], [11], []), ([], [11], []), ([12], [], []), ([12], [], [])]
        with patch.object(button_text.time, "perf_counter", return_value=0), \
                patch.object(button_text.select, "select", side_effect=readiness) as select, \
                patch.object(button_text.os, "write", side_effect=[2, 2]) as write, \
                patch.object(button_text.os, "read", side_effect=[b'["text"', b']\n']):
            self.assertEqual(reader._exchange(b"abc\n", 2), b'["text"]\n')
        self.assertEqual([bytes(call.args[1]) for call in write.call_args_list], [b"abc\n", b"c\n"])
        self.assertTrue(all(0 < call.args[3] <= .1 for call in select.call_args_list))

    def test_cancellation_before_or_during_wait_closes_without_reading_or_writing(self):
        """已取消时立即退出；等待期间取消最迟在下一次不超过 100ms 的轮询退出。"""
        for already_cancelled in (True, False):
            with self.subTest(already_cancelled=already_cancelled):
                stop = threading.Event()
                if already_cancelled:
                    stop.set()
                reader, _ = self.reader(stop)

                def cancel(*args):
                    self.assertLessEqual(args[3], .1)
                    stop.set()
                    return [], [], []

                with patch.object(button_text.select, "select", side_effect=cancel) as select, \
                        patch.object(button_text.os, "write") as write, \
                        patch.object(button_text.os, "read") as read:
                    self.assertIsNone(reader.read(Image.new("RGB", (2, 2))))
                self.assertEqual(select.call_count, 0 if already_cancelled else 1)
                write.assert_not_called()
                read.assert_not_called()
                self.assertIsNone(reader.process)
                self.assertTrue(reader.error.startswith("InterruptedError:"))

    def test_blocked_write_or_partial_reply_obeys_whole_exchange_deadline(self):
        """无可写空间和仅半行回复都必须在统一截止时间退出，不能卡在 write/readline。"""
        for partial_reply in (False, True):
            with self.subTest(partial_reply=partial_reply):
                reader, _ = self.reader()
                reader.warmed = True
                now, delivered = [0.0], [False]

                def poll(readers, writers, _, timeout):
                    self.assertGreater(timeout, 0)
                    self.assertLessEqual(timeout, .1)
                    if partial_reply and writers:
                        return [], writers, []
                    if partial_reply and not delivered[0]:
                        delivered[0] = True
                        return readers, [], []
                    now[0] += timeout
                    return [], [], []

                with patch.object(button_text.time, "perf_counter", side_effect=lambda: now[0]), \
                        patch.object(button_text.select, "select", side_effect=poll), \
                        patch.object(button_text.os, "write", side_effect=lambda _, value: len(value)), \
                        patch.object(button_text.os, "read", return_value=b"["):
                    self.assertIsNone(reader.read(Image.new("RGB", (2, 2))))
                self.assertAlmostEqual(now[0], 2)
                self.assertTrue(reader.error.startswith("TimeoutError:"))
                self.assertIsNone(reader.process)

    def test_cancel_or_deadline_when_reply_becomes_readable_never_releases_purchase(self):
        """可读信号与取消/截止同时发生时，不能让刚返回的购买文字越过停止条件。"""
        for reason in ("cancel", "deadline"):
            with self.subTest(reason=reason):
                stop, now = threading.Event(), [0.0]
                reader, _ = self.reader(stop)
                reader.warmed = True

                def poll(readers, writers, _, timeout):
                    if writers:
                        return [], writers, []
                    if reason == "cancel":
                        stop.set()
                    else:
                        now[0] = 2.0
                    return readers, [], []

                with patch.object(button_text.time, "perf_counter", side_effect=lambda: now[0]), \
                        patch.object(button_text.select, "select", side_effect=poll), \
                        patch.object(button_text.os, "write", side_effect=lambda _, value: len(value)), \
                        patch.object(button_text.os, "read", return_value='["立即购票"]\n'.encode()):
                    self.assertIsNone(reader.read(Image.new("RGB", (2, 2))))
                self.assertIsNone(reader.process)
                self.assertTrue(reader.error.startswith("InterruptedError:" if reason == "cancel" else "TimeoutError:"))


if __name__ == "__main__":
    unittest.main()
