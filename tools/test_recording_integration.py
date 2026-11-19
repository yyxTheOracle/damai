"""验证实战入口、点击循环和页面留证的接线，不连接手机。"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import damai_mode0_K80 as app
import run_log


def read_events(directory: Path) -> list[dict]:
    """等记录器关闭后读取完整事件，避免依赖后台写盘时序。"""
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


class RecordingIntegrationTests(unittest.TestCase):
    def test_new_main_page_clicks_before_next_tick_without_repeating_each_frame(self):
        """详情切到票档应立即点击；重复帧不加速连点，订单提交仍优先且支付宝停止。"""
        detail = '''<hierarchy><node package="cn.damai" enabled="true" clickable="false"
          resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
          bounds="[341,2429][1168,2576]"/></hierarchy>'''
        sku = detail.replace('clickable="false"', 'clickable="true"').replace(
            'resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"',
            'text="确定"')
        order = sku.replace('text="确定"', 'text="立即提交"')
        device = Mock()
        device.window_size.return_value = (1200, 2670)
        probe = app.XiaomiPageProbe(device)
        now = [0.0]
        probe.main_points.clock = probe.submit_points.clock = lambda: now[0]
        frames = iter(((0.0, detail), (0.04, detail), (0.06, sku), (0.07, sku),
                       (0.21, sku), (0.23, order)))
        stop, controlled = threading.Event(), threading.Event()
        requests = app.TapRequestQueue()
        controller = app.Mode0StateController()
        taps = []

        def query_package():
            """在前台检查期间送入新页面，重放识别线程与点击线程的交接。"""
            frame = next(frames, None)
            if frame is None:
                return app.ALIPAY_PACKAGE
            now[0], device.dump_hierarchy.return_value = frame
            decision = controller.observe(probe.read(controller.phase))
            if controller.controlled:
                controlled.set()
            if decision.action:
                requests.request(decision.action)
            return app.DAMAI_PACKAGE

        shell, query = Mock(), Mock()
        shell.tap.side_effect = lambda point: taps.append((now[0], point))
        query.query_package.side_effect = query_package
        with patch.object(app.time, "perf_counter", side_effect=lambda: now[0]):
            app.tap_loop(app.Mode0Schedule(2.0, 0.0, 0.0, 10.0), app.RuntimeState(),
                         stop, controlled, requests, shell, query,
                         main_points=probe.main_points, submit_points=probe.submit_points)
        self.assertEqual(taps, [(t, app.XIAOMI_15_MAIN_ACTION_POINT) for t in (0.0, 0.06, 0.21, 0.23)])
        self.assertTrue(stop.is_set())

    def test_main_saves_success_error_and_interrupt_and_restores_globals(self):
        """成功、普通异常和 Ctrl-C 都应留下结果，并恢复下一次运行需要的全局状态。"""
        cases = ((None, "alipay_reached"), (RuntimeError("模拟失败"), "error"),
                 (KeyboardInterrupt(), "interrupted"))
        original_directory = app.ARTIFACTS_DIR
        for failure, expected_status in cases:
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sale_time = datetime.now()

                def fake_run(_):
                    """模拟业务输出及退出方式，不进入任何设备操作。"""
                    print("模拟业务输出")
                    print("模拟 stderr", file=sys.stderr)
                    if failure is not None:
                        raise failure
                    return {"status": "alipay_reached"}

                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch.object(
                    app, "parse_args", return_value=SimpleNamespace(sale_time=sale_time)
                ), patch.object(app, "run", side_effect=fake_run), patch.object(
                    app, "ARTIFACTS_DIR", root
                ), patch.object(run_log, "active", None):
                    if failure is None:
                        app.main()
                    else:
                        with self.assertRaises(type(failure)):
                            app.main()
                    self.assertIsNone(run_log.active)
                    self.assertEqual(app.ARTIFACTS_DIR, root)

                directories = list(root.glob("run_*"))
                self.assertEqual(len(directories), 1)
                directory = directories[0]
                console = (directory / "console.log").read_text()
                self.assertIn("模拟业务输出", console)
                self.assertIn("模拟 stderr", console)
                result = json.loads((directory / "result.json").read_text())
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["recording_errors"], [])
                self.assertEqual(result["recording_directory"], str(directory))
                events = read_events(directory)
                self.assertEqual(events[0]["event"], "run_started")
                self.assertEqual(events[-1]["event"], "run_finished")
                self.assertEqual(events[-1]["status"], expected_status)
                self.assertEqual(any(event["event"] == "run_error" for event in events), failure is not None)
            self.assertEqual(app.ARTIFACTS_DIR, original_directory)
            self.assertIsNone(run_log.active)

    def test_main_and_submit_dispatch_are_recorded_and_stop_on_alipay(self):
        """记录的是发送动作；观察到支付宝后，既不能继续点击，也不能再记发送成功。"""
        for action in ("main", "submit"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temporary:
                log = run_log.RunLog(Path(temporary) / "run", datetime.now())
                stop, controlled = threading.Event(), threading.Event()
                requests = app.TapRequestQueue()
                if action == "submit":
                    controlled.set()
                    requests.request("submit")
                main_points, submit_points = app.RetryPointHolder(), app.RetryPointHolder()
                point = app.XIAOMI_15_MAIN_ACTION_POINT
                main_points.set(point)
                submit_points.set(point)
                shell, query = Mock(), Mock()
                query.query_package.side_effect = [app.DAMAI_PACKAGE, app.ALIPAY_PACKAGE]
                now = app.time.perf_counter()
                schedule = app.Mode0Schedule(now, now, now, now + 10)
                state = app.RuntimeState()
                try:
                    with patch.object(run_log, "active", log):
                        app.tap_loop(schedule, state, stop, controlled, requests, shell, query,
                                     main_points=main_points, submit_points=submit_points)
                finally:
                    log.close()

                shell.tap.assert_called_once_with(point)
                shell.back.assert_not_called()
                self.assertEqual(query.query_package.call_count, 2)
                self.assertEqual(state.status, "alipay_reached")
                self.assertTrue(stop.is_set())
                events = read_events(log.directory)
                dispatched = [event for event in events if event["event"] == "input_dispatched"]
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(dispatched[0]["action"], action)
                self.assertEqual(dispatched[0]["point"], list(point))
                self.assertGreaterEqual(dispatched[0]["query_ms"], 0)
                foreground = [event for event in events if event["event"] == "foreground_changed"]
                self.assertEqual([event["package"] for event in foreground],
                                 [app.DAMAI_PACKAGE, app.ALIPAY_PACKAGE])
                self.assertLess(dispatched[0]["sequence"], foreground[-1]["sequence"])
                self.assertEqual(events[-1]["event"], "stop_requested")
                self.assertEqual(events[-1]["reason"], "alipay_reached")

    def test_xiaomi_probe_reuses_xml_and_only_saves_changed_stages(self):
        """连续两帧详情只留一份 XML，切到票档再留证，不能为了日志额外读取手机。"""
        detail = '''<hierarchy><node package="cn.damai" enabled="true" clickable="false"
          resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
          bounds="[341,2429][1168,2576]"/></hierarchy>'''
        sku = '''<hierarchy><node package="cn.damai" enabled="true" clickable="false"
          resource-id="cn.damai:id/id_sku_bottom_bar_container" bounds="[0,2239][1200,2605]">
          <node package="cn.damai" enabled="true" clickable="false" text="1张"
            resource-id="cn.damai:id/tv_num" bounds="[895,2272][972,2341]"/>
          <node package="cn.damai" enabled="true" clickable="false" text="588"
            resource-id="cn.damai:id/tv_price" bounds="[91,2470][196,2548]"/>
          <node package="cn.damai" enabled="true" clickable="true"
            resource-id="cn.damai:id/btn_buy_view" bounds="[736,2436][1132,2579]"/>
          </node></hierarchy>'''
        device = Mock()
        device.window_size.return_value = (1200, 2670)
        device.dump_hierarchy.side_effect = [detail, detail, sku]
        probe = app.XiaomiPageProbe(device)
        with tempfile.TemporaryDirectory() as temporary:
            log = run_log.RunLog(Path(temporary) / "run", datetime.now())
            try:
                with patch.object(run_log, "active", log):
                    for _ in range(3):
                        self.assertEqual(probe.read("blind"), app.PAGE_DAMAI_OTHER)
            finally:
                log.close()
            self.assertEqual(device.dump_hierarchy.call_count, 3)
            device.screenshot.assert_not_called()
            events = read_events(log.directory)
            self.assertEqual([event["event"] for event in events], ["page_probe"] * 3)
            self.assertEqual([event["stage"] for event in events], ["detail", "detail", "ticket_panel"])
            self.assertTrue(all(event["duration_ms"] >= 0 for event in events))
            self.assertEqual(["xml_path" in event for event in events], [True, False, True])
            self.assertEqual(len(list(log.directory.glob("frame_*.xml"))), 2)
            self.assertEqual((log.directory / events[0]["xml_path"]).read_text(), detail)
            self.assertEqual((log.directory / events[2]["xml_path"]).read_text(), sku)

    def test_unknown_page_keeps_changed_xml_without_reading_phone_twice(self):
        """空窗口变成 WebView 仍属 unknown，但新 XML 必须留下以便复盘验证页。"""
        blank = '<hierarchy><node package="cn.damai" class="android.widget.FrameLayout" bounds="[0,769][1200,1975]"/></hierarchy>'
        webview = blank.replace('android.widget.FrameLayout', 'com.uc.webview.export.WebView').replace(
            'bounds=', 'text="WVUCWebView" bounds=')
        device = Mock()
        device.window_size.return_value = (1200, 2670)
        device.dump_hierarchy.side_effect = [blank, webview, webview]
        probe = app.XiaomiPageProbe(device)
        with tempfile.TemporaryDirectory() as temporary:
            log = run_log.RunLog(Path(temporary) / 'run', datetime.now())
            try:
                with patch.object(run_log, 'active', log):
                    for _ in range(3):
                        self.assertEqual(probe.read('paused'), app.PAGE_UNKNOWN_DIALOG)
            finally:
                log.close()
            events = read_events(log.directory)
            self.assertEqual(['xml_path' in event for event in events], [True, True, False])
            self.assertEqual((log.directory / events[1]['xml_path']).read_text(), webview)
        self.assertEqual(device.dump_hierarchy.call_count, 3)
        device.screenshot.assert_not_called()

    def test_xiaomi_unknown_snapshots_continue_after_global_quota(self):
        """同分类的验证页不能耗尽三张额度后永远无图，持续未知每五秒补一张。"""
        device = Mock()
        inner = app.XiaomiPageProbe(device)
        inner.read = Mock(return_value=app.PAGE_UNKNOWN_DIALOG)
        inner.last_xml = '<hierarchy/>'
        now = [0.0]
        thread = Mock()
        thread.is_alive.return_value = False
        probe = app.DiagnosticProbe(inner, device, 0)
        probe.dialog_snapshots = app.DIALOG_SNAPSHOT_LIMIT
        with redirect_stdout(io.StringIO()), patch.object(app.time, 'perf_counter', side_effect=lambda: now[0]), \
                patch.object(app.threading, 'Thread', return_value=thread), patch.object(run_log, 'emit') as log:
            for now[0] in (0.0, 1.0, 4.9, 5.0, 10.0, 15.0):
                probe.read('paused')
        self.assertEqual(thread.start.call_count, 4)
        self.assertEqual(probe.dialog_snapshots, app.DIALOG_SNAPSHOT_LIMIT + 4)
        self.assertEqual(len([c for c in log.call_args_list if c.args[0] == 'snapshot_started']), 4)
        device.dump_hierarchy.assert_not_called()

    def test_snapshot_skip_reason_is_recorded_without_repeating_every_attempt(self):
        """未知页来回跳变时仍遵守截图间隔；忙和间隔原因只在变化时留证。"""
        device = Mock()
        inner = app.XiaomiPageProbe(device)
        now = [0.0]
        thread = Mock()
        thread.is_alive.return_value = False
        probe = app.DiagnosticProbe(inner, device, 0)
        with patch.object(app.time, 'perf_counter', side_effect=lambda: now[0]), \
                patch.object(app.threading, 'Thread', return_value=thread), patch.object(run_log, 'emit') as log:
            probe._start_snapshot(app.PAGE_UNKNOWN_DIALOG, 0)
            for now[0] in (0.1, 0.2, 0.3):
                probe._start_snapshot(app.PAGE_UNKNOWN_DIALOG, now[0])
            now[0] = 5.0
            thread.is_alive.return_value = True
            probe._start_snapshot(app.PAGE_UNKNOWN_DIALOG, 5)
            probe._start_snapshot(app.PAGE_UNKNOWN_DIALOG, 5)
            thread.is_alive.return_value = False
            probe._start_snapshot(app.PAGE_UNKNOWN_DIALOG, 5)
        skips = [c.kwargs['reason'] for c in log.call_args_list if c.args[0] == 'snapshot_skipped']
        self.assertEqual(skips, ['capture_interval', 'capture_busy'])
        self.assertEqual(thread.start.call_count, 2)

    def test_snapshot_failure_keeps_xml_and_reports_capture_timing(self):
        """截图失败仍保存已有 XML，并记录错误与采集起点，不再额外请求 XML。"""
        device = Mock()
        device.screenshot.side_effect = RuntimeError('capture disconnected')
        inner = app.XiaomiPageProbe(device)
        probe = app.DiagnosticProbe(inner, device, 0)
        xml = '<hierarchy><node package="cn.damai" text="WVUCWebView"/></hierarchy>'
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()), \
                patch.object(app, 'ARTIFACTS_DIR', Path(temporary)), patch.object(run_log, 'emit') as log:
            base = Path(temporary) / 'capture'
            probe._snapshot(base, app.PAGE_UNKNOWN_DIALOG, 1, xml)
            self.assertEqual(base.with_suffix('.xml').read_text(), xml)
        device.dump_hierarchy.assert_not_called()
        event = next(c.kwargs for c in log.call_args_list if c.args[0] == 'dialog_snapshot')
        self.assertEqual(event['files'], ['capture.xml'])
        self.assertIn('capture disconnected', event['error'])
        self.assertIsInstance(event['capture_started_monotonic'], float)
        self.assertGreaterEqual(event['duration_ms'], 0)


if __name__ == "__main__":
    unittest.main()
