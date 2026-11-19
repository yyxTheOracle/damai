"""离线验证 Mac 路径、小米固定坐标与页面识别失败时的暂停。"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import device_support as support
import damai_mode0_K80 as app
from precheck_K80 import load_constants


class DeviceSupportTests(unittest.TestCase):
    def test_mac_uses_native_adb_from_path(self):
        with patch.object(support.sys, "platform", "darwin"), patch.object(
            support.Path, "is_file", return_value=False
        ), patch.object(support.shutil, "which", return_value="/opt/homebrew/bin/adb"):
            self.assertEqual(support.default_adb_path(), Path("/opt/homebrew/bin/adb"))

    def test_windows_keeps_project_platform_tools(self):
        with patch.object(support.sys, "platform", "win32"), patch.object(
            support.Path, "is_file", return_value=True
        ):
            self.assertEqual(support.default_adb_path().name, "adb.exe")

    def test_precheck_uses_model_size_and_retains_k80(self):
        self.assertEqual(load_constants()["EXPECTED_SCREEN_SIZE"], (1440, 3200))
        self.assertEqual(load_constants()["MAIN_ACTION_POINT"], (1119, 3088))
        self.assertEqual(load_constants(support.XIAOMI_15_MODEL)["EXPECTED_SCREEN_SIZE"], (1200, 2670))
        self.assertEqual(load_constants(support.XIAOMI_15_MODEL)["MAIN_ACTION_POINT"], support.XIAOMI_15_MAIN_ACTION_POINT)

    def test_runtime_rejects_wrong_size(self):
        device = Mock()
        device.shell.return_value.output = support.XIAOMI_15_MODEL
        device.window_size.return_value = (2670, 1200)
        with patch.object(app, "query_foreground_once") as foreground:
            with self.assertRaisesRegex(RuntimeError, "1200×2670"):
                app.validate_preflight(device, Path("adb"))
            foreground.assert_not_called()

    def test_runtime_accepts_xiaomi_without_injecting_input(self):
        device = Mock()
        device.shell.return_value.output = support.XIAOMI_15_MODEL
        device.window_size.return_value = (1200, 2670)
        with patch.object(app, "query_foreground_once", return_value="cn.damai"), patch.object(
            app.subprocess, "run"
        ) as command:
            self.assertEqual(app.validate_preflight(device, Path("adb")), support.XIAOMI_15_MODEL)
            command.assert_not_called()

    def test_submit_request_restore_preserves_newer_action(self):
        queue = app.TapRequestQueue()
        queue.request("back")
        queue.restore("submit")
        self.assertEqual(queue.pop(), "back")
        queue.restore("submit")
        self.assertEqual(queue.pop(), "submit")
        queue.restore("submit")
        self.assertTrue(queue.request("back"))
        self.assertEqual(queue.pop(), "back")


class XiaomiIntegrationTests(unittest.TestCase):
    def test_xml_geometry_skips_queries_but_unknown_or_changed_size_is_checked(self):
        """完整 XML 免去额外查询；缺证据或尺寸变化仍回退，并撤销旧点位。"""
        xml = '''<hierarchy rotation="0"><node package="cn.damai" bounds="[0,0][1200,2670]">
          <node package="cn.damai" enabled="true" clickable="false"
            resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
            bounds="[341,2429][1168,2576]"/></node>
          <node package="com.android.systemui" bounds="[0,0][1200,140]"/></hierarchy>'''
        for name, frame, size, calls in (
            ("complete", xml, (1200, 2670), 0),
            ("unknown", xml.replace('rotation="0"', ''), (1200, 2670), 1),
            ("changed", xml.replace('[1200,2670]', '[1080,2400]'), (1080, 2400), 1),
            ("rotated", xml.replace('rotation="0"', 'rotation="1"'), (2670, 1200), 1),
        ):
            with self.subTest(name=name):
                device = Mock()
                device.dump_hierarchy.return_value = frame
                device.window_size.return_value = size
                probe = app.XiaomiPageProbe(device)
                probe.main_points.set(support.XIAOMI_15_MAIN_ACTION_POINT)
                if size == (1200, 2670):
                    self.assertEqual(probe.read("blind"), app.PAGE_DAMAI_OTHER)
                    self.assertEqual(probe.main_points.fresh(), support.XIAOMI_15_MAIN_ACTION_POINT)
                else:
                    with self.assertRaisesRegex(RuntimeError, "屏幕尺寸发生变化"):
                        probe.read("blind")
                    self.assertIsNone(probe.main_points.fresh())
                self.assertEqual(device.window_size.call_count, calls)
                device.dump_hierarchy.assert_called_once()

    def test_normal_run_selects_xiaomi_probe_and_passes_its_points(self):
        device = Mock()
        with patch.object(app, "resolve_adb", return_value=Path("adb")), patch.object(
            app, "resolve_serial", return_value="test-device"
        ), patch.object(app.u2, "connect", return_value=device), patch.object(
            app, "validate_preflight", return_value=support.XIAOMI_15_MODEL
        ), patch.object(app, "ScopedInput") as scoped_input, patch.object(app, "PersistentTapShell") as legacy_input, patch.object(
            app, "PersistentQueryShell"
        ), patch.object(app.threading, "Thread") as thread, patch.object(
            app, "run_tail_probe_loop"
        ) as tail, patch.object(app, "DEVICE_SERIAL", ""):
            result = app.run(app.datetime.now())
            inner = tail.call_args.kwargs["page_probe"].inner
            self.assertIsInstance(inner, app.XiaomiPageProbe)
            worker_args = next(call.kwargs["args"] for call in thread.call_args_list
                               if call.kwargs.get("name") == "damai-mode0-k80-tap-loop")
            schedule = worker_args[0]
            # 小米立即开始后台检查，首击交给文字事件；电脑到点不直接放行。
            self.assertLess(abs(schedule.first_probe_monotonic - schedule.sale_monotonic), 1)
            self.assertEqual(schedule.click_start_monotonic, schedule.sale_monotonic)
            self.assertIs(worker_args[-4], inner.main_points)
            self.assertIs(worker_args[-3], inner.submit_points)
            self.assertEqual(worker_args[-2], support.XIAOMI_15_MAIN_ACTION_POINT)
            self.assertIs(worker_args[-1], tail.call_args.kwargs["purchase_ready"])
            self.assertFalse(worker_args[-1].is_set())
            scoped_input.assert_called_once_with(Path("adb"), "test-device")
            legacy_input.assert_not_called()
            self.assertIs(worker_args[5], scoped_input.return_value)
            scoped_input.return_value.close.assert_called_once()
            self.assertTrue(tail.call_args.kwargs["controller"].continuous_main)
            self.assertTrue(tail.call_args.kwargs["controller"].stop_on_verification)
            self.assertTrue(callable(tail.call_args.kwargs["check_unknown_verification"]))
            self.assertTrue(tail.call_args.kwargs["controlled_event"].is_set())
            self.assertEqual(result["main_action_point"], list(support.XIAOMI_15_MAIN_ACTION_POINT))

    def test_worker_start_failure_stops_and_closes_input_and_query_connections(self):
        """线程未启动不能 join；任一启动失败仍撤销点击并关闭已经建立的连接。"""
        for failing_worker in ("tap", "button"):
            with self.subTest(failing_worker=failing_worker):
                tap_worker, button_worker = Mock(ident=None), Mock(ident=None)
                if failing_worker == "tap":
                    tap_worker.start.side_effect = RuntimeError("tap start failed")
                else:
                    tap_worker.start.side_effect = lambda: setattr(tap_worker, "ident", 123)
                    button_worker.start.side_effect = RuntimeError("button start failed")
                with patch.object(app, "resolve_adb", return_value=Path("adb")), patch.object(
                    app, "resolve_serial", return_value="test-device"
                ), patch.object(app.u2, "connect", return_value=Mock()), patch.object(
                    app, "validate_preflight", return_value=support.XIAOMI_15_MODEL
                ), patch.object(app, "ScopedInput") as scoped_input, patch.object(
                    app, "PersistentQueryShell"
                ) as query, patch.object(app.threading, "Thread", side_effect=[tap_worker, button_worker]) as thread, \
                        patch.object(app, "run_tail_probe_loop") as tail, patch.object(
                            app, "capture_boundary"
                        ), patch.object(app, "DEVICE_SERIAL", ""):
                    with self.assertRaisesRegex(RuntimeError, f"{failing_worker} start failed"):
                        app.run(app.datetime.now())
                self.assertTrue(thread.call_args_list[0].kwargs["args"][2].is_set())
                scoped_input.return_value.tap.assert_not_called()
                scoped_input.return_value.back.assert_not_called()
                scoped_input.return_value.close.assert_called_once()
                query.return_value.close.assert_called_once()
                tail.assert_not_called()
                button_worker.join.assert_not_called()
                if failing_worker == "tap":
                    tap_worker.join.assert_not_called()
                    button_worker.start.assert_not_called()
                else:
                    tap_worker.join.assert_called_once_with(timeout=app.THREAD_JOIN_TIMEOUT_S)

    def test_read_failure_clears_all_previous_points(self):
        device = Mock()
        device.window_size.return_value = (1200, 2670)
        device.dump_hierarchy.side_effect = RuntimeError("device lost")
        probe = app.XiaomiPageProbe(device)
        holders = (probe.main_points, probe.submit_points, probe.retry_points, probe.seat_points)
        for holder in holders:
            holder.set((754, 2502), wake=True)
        with self.assertRaises(RuntimeError):
            probe.read("blind")
        self.assertTrue(all(holder.fresh() is None for holder in holders))
        self.assertTrue(all(not holder.changed.is_set() for holder in holders))

    def test_submit_point_is_separate_from_detail_point(self):
        device = Mock()
        device.window_size.return_value = (1200, 2670)
        device.dump_hierarchy.return_value = "<hierarchy/>"
        probe = app.XiaomiPageProbe(device)
        with patch.object(app, "parse_page", return_value=("order_page", (900, 2500))):
            self.assertEqual(probe.read("await_submit"), "order_page")
        self.assertIsNone(probe.main_points.fresh())
        self.assertEqual(probe.submit_points.fresh(), (900, 2500))
        with patch.object(app, "parse_page", return_value=("unknown_dialog", None)):
            probe.read("await_result")
        self.assertIsNone(probe.submit_points.fresh())

    def test_tap_worker_uses_fixed_point_from_xiaomi_probe(self):
        stop = threading.Event()
        device = Mock()
        device.window_size.return_value = (1200, 2670)
        device.dump_hierarchy.return_value = '''<hierarchy><node package="cn.damai"
          resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
          enabled="true" clickable="false" bounds="[341,2429][1168,2576]"/></hierarchy>'''
        probe = app.XiaomiPageProbe(device)
        probe.read("blind")
        shell = Mock()
        shell.tap.side_effect = lambda _: stop.set()
        query = Mock()
        query.query_package.return_value = "cn.damai"
        now = app.time.perf_counter()
        schedule = app.Mode0Schedule(now, now, now, now + 1)
        app.tap_loop(schedule, app.RuntimeState(), stop, threading.Event(),
                     app.TapRequestQueue(), shell, query, main_points=probe.main_points)
        shell.tap.assert_called_once_with(support.XIAOMI_15_MAIN_ACTION_POINT)
        device.dump_hierarchy.assert_called_once()

    def test_expired_point_never_falls_back_to_k80_coordinates(self):
        stop = threading.Event()
        point = app.RetryPointHolder(clock=lambda: 0)
        point.set((754, 2502), wake=True)
        point.clock = lambda: 2
        shell = Mock()
        query = Mock()
        query.query_package.side_effect = ["cn.damai", "com.eg.android.AlipayGphone"]
        now = app.time.perf_counter()
        schedule = app.Mode0Schedule(now, now, now, now + 1)
        app.tap_loop(schedule, app.RuntimeState(), stop, threading.Event(),
                     app.TapRequestQueue(), shell, query, main_points=point)
        shell.tap.assert_not_called()

    def test_prepared_point_waits_for_click_start(self):
        """即便已提前获得坐标，也必须等到调度指定的首击时刻。"""
        now = [0.0]
        stop = threading.Event()
        point = app.RetryPointHolder(clock=lambda: now[0])
        point.set((754, 2502), wake=True)
        clicks = []

        def wait_until(target, event):
            self.assertEqual(clicks, [])
            now[0] = target
            return True

        def tap(position):
            clicks.append((now[0], position))
            stop.set()

        shell = Mock()
        shell.tap.side_effect = tap
        query = Mock()
        query.query_package.return_value = "cn.damai"
        schedule = app.Mode0Schedule(3.0, 1.0, 0.0, 603.0)
        with patch.object(app.time, "perf_counter", side_effect=lambda: now[0]), patch.object(
            app, "wait_until", side_effect=wait_until
        ):
            app.tap_loop(schedule, app.RuntimeState(), stop, threading.Event(),
                         app.TapRequestQueue(), shell, query, main_points=point)
        self.assertEqual(clicks, [(1.0, (754, 2502))])


if __name__ == "__main__":
    unittest.main()
