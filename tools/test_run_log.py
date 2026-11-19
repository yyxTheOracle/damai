"""仅用临时文件验证实战日志，不连接手机。"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import run_log


class RunLogTests(unittest.TestCase):
    def test_concurrent_events_are_complete_and_ordered_after_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = run_log.RunLog(Path(temporary) / "run", datetime.now() + timedelta(seconds=30))
            workers = [threading.Thread(target=lambda: [log.emit("tap_dispatched", point=[930, 2502])
                                                        for _ in range(100)]) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            log.close()
            log.close()
            records = [json.loads(line) for line in (log.directory / "events.jsonl").read_text().splitlines()]
            self.assertEqual(len(records), 400)
            self.assertEqual([r["sequence"] for r in records], list(range(1, 401)))
            self.assertTrue(all(r["seconds_after_sale"] < 0 for r in records))
            self.assertTrue(all(datetime.fromisoformat(r["wall_time"]).tzinfo for r in records))
            self.assertEqual(log.errors, [])

    def test_xml_console_and_result_are_saved_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = run_log.RunLog(Path(temporary) / "run", datetime.now())
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr), log.capture_console():
                print("页面识别开始")
                print("模拟错误", file=sys.stderr)
            with patch.object(run_log, "active", log):
                run_log.emit("page_changed", xml_text="<hierarchy/>", page="order_page")
            log.close()
            result = {"status": "alipay_reached"}
            log.write_result(result)
            event = json.loads((log.directory / "events.jsonl").read_text())
            self.assertNotIn("xml_text", event)
            self.assertEqual((log.directory / event["xml_path"]).read_text(), "<hierarchy/>")
            console = (log.directory / "console.log").read_text()
            self.assertIn("页面识别开始", console)
            self.assertIn("模拟错误", console)
            self.assertEqual(stdout.getvalue(), "页面识别开始\n")
            self.assertEqual(stderr.getvalue(), "模拟错误\n")
            self.assertEqual(json.loads((log.directory / "result.json").read_text()), result)

    def test_write_and_close_failures_are_reported_without_raising_in_emit(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()) as stderr:
            log = run_log.RunLog(Path(temporary) / "run", datetime.now())
            actual = log._events
            broken = Mock(wraps=actual)
            broken.write.side_effect = OSError("disk full")
            broken.close.side_effect = OSError("close failed")
            log._events = broken
            try:
                log.emit("tap_dispatched")
                log.emit("tap_dispatched")
                log.close()
            finally:
                actual.close()
            self.assertEqual(len(log.errors), 2)
            self.assertIn("disk full", stderr.getvalue())
            self.assertIn("close failed", stderr.getvalue())

    def test_existing_directory_is_not_overwritten_and_inactive_emit_is_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileExistsError):
                run_log.RunLog(Path(temporary), datetime.now())
            with patch.object(run_log, "active", None):
                run_log.emit("tap_dispatched")

    def test_xml_failure_keeps_event_and_result_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()) as stderr:
            log = run_log.RunLog(Path(temporary) / "run", datetime.now())
            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                log.emit("page_changed", xml_text="<hierarchy/>")
                log.close()
                with self.assertRaises(OSError):
                    log.write_result({"status": "error"})
            event = json.loads((log.directory / "events.jsonl").read_text())
            self.assertIn("disk full", event["xml_error"])
            self.assertNotIn("xml_path", event)
            self.assertEqual(len(log.errors), 2)
            self.assertIn("XML:", stderr.getvalue())
            self.assertIn("result:", stderr.getvalue())

    def test_button_frames_are_deferred_until_close_with_manifest_and_ready_crop(self):
        """图像引用保留到收尾，触发截图使用原流中的购买框，不额外采集手机。"""
        with tempfile.TemporaryDirectory() as temporary:
            log = run_log.RunLog(Path(temporary) / "run", datetime.now())
            before, ready = Image.new("RGB", (8, 6), "orange"), Image.new("RGB", (8, 6), "red")
            bounds = (342, 2429, 346, 2433)
            log.keep_button_frames([(1, 10.0, before), (2, 10.02, ready)], 2, bounds)
            self.assertIs(log._button_frames[0][1][2], ready)
            self.assertFalse((log.directory / "button_frames").exists())
            self.assertFalse((log.directory / "purchase_button_ready.png").exists())
            log.emit("input_dispatched")
            log.close()
            index = json.loads((log.directory / "button_frames/index.json").read_text())
            self.assertEqual(index["ready_sequence"], 2)
            self.assertEqual(index["bounds"], list(bounds))
            self.assertIn("不是手机显示时间", index["time_basis"])
            self.assertEqual(index["frames"], [
                {"sequence": 1, "received_monotonic": 10.0, "path": "button_frames/1.png"},
                {"sequence": 2, "received_monotonic": 10.02, "path": "button_frames/2.png"}])
            with Image.open(log.directory / "purchase_button_ready.png") as image:
                self.assertEqual(image.size, (4, 4))
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
            self.assertIsNone(log._button_frames)
            with patch.object(Image.Image, "save") as save, patch.object(Path, "write_text") as write:
                log.keep_button_frames([(3, 11.0, before)], 3, bounds)
                log.close()
            save.assert_not_called()
            write.assert_not_called()
            self.assertIsNone(log._button_frames)

    def test_button_frame_failure_keeps_other_frames_and_normal_events(self):
        """图片写失败只记入错误，不中断事件收尾和其余帧的保存。"""
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            log = run_log.RunLog(Path(temporary) / "run", datetime.now())
            broken = Mock()
            broken.save.side_effect = OSError("frame disk full")
            image = Image.new("RGB", (8, 6), "red")
            log.keep_button_frames([(1, 10.0, broken), (2, 10.02, image)], 2, (340, 2428, 344, 2432))
            log.emit("run_finished", status="alipay_reached")
            log.close()
            self.assertEqual(json.loads((log.directory / "events.jsonl").read_text())["event"], "run_finished")
            index = json.loads((log.directory / "button_frames/index.json").read_text())
            self.assertIn("frame disk full", index["frames"][0]["error"])
            self.assertTrue((log.directory / "button_frames/2.png").exists())
            self.assertTrue((log.directory / "purchase_button_ready.png").exists())
            self.assertEqual(len(log.errors), 1)
            self.assertIsNone(log._button_frames)

    def test_untriggered_run_keeps_frames_without_claiming_a_ready_image(self):
        """取消或超时也保存末尾画面，manifest 的空放行序号不能伪装成功。"""
        with tempfile.TemporaryDirectory() as temporary:
            log = run_log.RunLog(Path(temporary) / 'run', datetime.now())
            log.keep_button_frames([(4, 10.0, Image.new('RGB', (8, 6)))], None, (340, 2428, 344, 2432))
            log.close()
            index = json.loads((log.directory / 'button_frames/index.json').read_text())
            self.assertIsNone(index['ready_sequence'])
            self.assertTrue((log.directory / 'button_frames/4.png').exists())
            self.assertFalse((log.directory / 'purchase_button_ready.png').exists())

    def test_button_manifest_failure_is_reported_and_does_not_retry_on_close(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stderr(io.StringIO()):
            log = run_log.RunLog(Path(temporary) / "run", datetime.now())
            log.keep_button_frames([(1, 10.0, Image.new("RGB", (8, 6)))], 1, (340, 2428, 344, 2432))
            log.emit("run_finished")
            with patch.object(Path, "write_text", side_effect=OSError("manifest disk full")) as write:
                log.close()
                log.close()
            write.assert_called_once()
            self.assertIn("manifest disk full", log.errors[0])
            self.assertEqual(json.loads((log.directory / "events.jsonl").read_text())["event"], "run_finished")
            self.assertIsNone(log._button_frames)


if __name__ == "__main__":
    unittest.main()
