"""用可控时钟串联真实页面探测、状态循环和点击循环；绝不连接手机。"""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import damai_mode0_K80 as app


DETAIL = '''<hierarchy><node package="cn.damai" enabled="true" clickable="false"
  resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
  bounds="[341,2429][1168,2576]"/></hierarchy>'''
SKU = DETAIL.replace('clickable="false"', 'clickable="true"').replace(
    'resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"', 'text="确定"')
ORDER = SKU.replace('text="确定"', 'text="立即提交"')
LOADING = '<hierarchy><node package="cn.damai" resource-id="cn.damai:id/order_activity_title"/></hierarchy>'
UNKNOWN = '<hierarchy><node package="cn.damai" text="未知页面"/></hierarchy>'
RETRY = '''<hierarchy><node package="cn.damai" enabled="true" clickable="true"
  text="继续尝试" resource-id="cn.damai:id/damai_theme_dialog_confirm_btn"
  bounds="[341,1600][1168,1800]"/></hierarchy>'''
REFRESH = RETRY.replace('继续尝试', '努力刷新').replace('damai_theme_dialog_confirm_btn', 'state_view_btn')
SOLD_OUT = RETRY.replace('继续尝试', '返回重新选购')
RISK = '<hierarchy><node package="cn.damai" text="请完成安全验证"/></hierarchy>'
RECORDED = Path(__file__).parent / 'fixtures' / 'sale_1600'


def recorded(name):
    """读取本次实战的脱敏快照，保留导致误暂停的真实节点与窗口边界。"""
    return (RECORDED / f'{name}.xml').read_text()


class ContinuousFlowTests(unittest.TestCase):
    def replay(self, steps, initial=DETAIL, *, stop_on_verification=False, check_unknown=None):
        """每次前台查询交接一帧后台观察，执行真实 tail 循环而非复制其决策代码。"""
        now = [7.0]
        schedule = app.Mode0Schedule(10.0, 10.0, 7.0, 30.0)
        stop, controlled = threading.Event(), threading.Event()
        controlled.set()
        requests, state = app.TapRequestQueue(), app.RuntimeState()
        controller = app.Mode0StateController(clock=lambda: now[0], continuous_main=True,
                                              stop_on_verification=stop_on_verification)
        device = Mock()
        device.window_size.return_value = (1200, 2670)
        probe = app.XiaomiPageProbe(device)
        for holder in (probe.main_points, probe.submit_points, probe.retry_points, probe.seat_points):
            holder.clock = lambda: now[0]
        inputs = []
        shell = Mock()
        shell.tap.side_effect = lambda point: inputs.append((round(now[0] - 10, 3), point))
        shell.back.side_effect = lambda: inputs.append((round(now[0] - 10, 3), "back"))

        def observe(frame):
            """驱动一轮后台循环；异常也由真实探测器捕获并交给状态机。"""
            device.dump_hierarchy.side_effect = [frame]
            once = iter((True, False))
            app.run_tail_probe_loop(
                schedule=schedule, state=state, stop_event=stop, controlled_event=controlled,
                requests=requests, controller=controller, page_probe=probe,
                wait_until=lambda *_: next(once), state_check_interval_s=0,
                risk_state_check_interval_s=0,
                check_unknown_verification=(lambda: check_unknown(device, controlled, requests))
                if check_unknown else None,
            )

        remaining = iter(steps)

        def query_package():
            """None 表示这段时间没有新页面，不能因主点位过期而中断连点。"""
            step = next(remaining, None)
            if step is None:
                return app.ALIPAY_PACKAGE
            offset, frame, package = step
            now[0] = 10 + offset
            if frame is not None:
                observe(frame)
            if isinstance(package, Exception):
                raise package
            return package

        def await_start(target, event):
            self.assertEqual(inputs, [])
            self.assertEqual(target, schedule.sale_monotonic)
            now[0] = target
            return not event.is_set()

        with redirect_stdout(io.StringIO()), patch.object(app.time, "perf_counter", side_effect=lambda: now[0]), \
                patch.object(app, "wait_until", side_effect=await_start), patch.object(app.run_log, "emit") as log:
            observe(initial)
            app.tap_loop(schedule, state, stop, controlled, requests, shell,
                         Mock(query_package=query_package), retry_points=probe.retry_points,
                         seat_points=probe.seat_points, main_points=probe.main_points,
                         submit_points=probe.submit_points,
                         continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT)
        self.assertTrue(stop.is_set())
        self.assertFalse(state.worker_error, state.worker_error)
        return inputs, state, log

    def test_sale_time_then_continuous_taps_across_pages_without_point_refresh(self):
        """预检在开售前三秒完成；准点首击，切页/订单加载/超过一秒无新帧仍连点。"""
        inputs, state, log = self.replay([
            (0, None, app.DAMAI_PACKAGE), (0.04, SKU, app.DAMAI_PACKAGE),
            (0.15, LOADING, app.DAMAI_PACKAGE), (0.30, ORDER, app.DAMAI_PACKAGE),
            (1.50, None, app.DAMAI_PACKAGE),
        ])
        self.assertEqual(inputs, [(t, app.XIAOMI_15_MAIN_ACTION_POINT) for t in (0, .15, .30, 1.50)])
        actions = [call.kwargs['action'] for call in log.call_args_list if call.args[0] == 'input_dispatched']
        self.assertEqual(actions, ['main'] * 4)
        self.assertEqual(state.status, 'alipay_reached')

    def test_recorded_purchase_transitions_do_not_interrupt_main_taps(self):
        """复现本次骨架页、空树和提交中误暂停，走实际探测到点击的完整链路。"""
        frames = ('detail_transition', 'sku', 'blank_transition', 'order',
                  'submitting', 'blank_fullscreen', 'order')
        offsets = (.16, .32, .48, .64, .80, .96, 1.12)
        inputs, state, _ = self.replay(
            [(0, None, app.DAMAI_PACKAGE)] +
            [(t, recorded(name), app.DAMAI_PACKAGE) for t, name in zip(offsets, frames)],
            initial=recorded('detail'),
        )
        self.assertEqual(inputs, [(t, app.XIAOMI_15_MAIN_ACTION_POINT) for t in (0, *offsets)])
        self.assertEqual(state.status, 'alipay_reached')

    def test_recorded_unknown_windows_and_unavailable_sku_still_pause(self):
        """空的居中弹窗、缺货和屏幕外 SKU 都不能借转场规则继续主点击。"""
        for name in ('blank_dialog', 'sold_out', 'offscreen_sku'):
            with self.subTest(frame=name):
                inputs, _, _ = self.replay([
                    (0, None, app.DAMAI_PACKAGE), (.16, recorded(name), app.DAMAI_PACKAGE),
                    (.32, None, app.DAMAI_PACKAGE), (.48, ORDER, app.DAMAI_PACKAGE),
                ])
                self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT),
                                         (.48, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_recorded_transition_never_starts_or_recovers_an_unknown_flow(self):
        """空转场只延续正常连点，不作为启动证据，也不能解除此前异常。"""
        for name in ('detail_transition', 'blank_transition', 'blank_fullscreen'):
            with self.subTest(frame=name):
                inputs, _, _ = self.replay([
                    (0, recorded(name), app.DAMAI_PACKAGE), (.16, DETAIL, app.DAMAI_PACKAGE),
                    (.32, UNKNOWN, app.DAMAI_PACKAGE), (.48, recorded(name), app.DAMAI_PACKAGE),
                    (.64, ORDER, app.DAMAI_PACKAGE),
                ], initial=recorded(name))
                self.assertEqual(inputs, [(.16, app.XIAOMI_15_MAIN_ACTION_POINT),
                                         (.64, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_transition_exceptions_do_not_hide_real_overlays(self):
        """给实战转场/提交中叠加异常，验证先暂停而非透过弹窗继续点击。"""
        for name in ('detail_transition', 'blank_transition', 'submitting'):
            for overlay in (
                '<node package="com.android.permissioncontroller" text="允许" bounds="[0,0][1200,2670]"/>',
                '<node package="cn.damai" resource-id="cn.damai:id/other_dialog"/>',
                '<node package="cn.damai" class="android.widget.FrameLayout" clickable="false" bounds="[100,700][1100,1900]"/>',
                '<node package="cn.damai" class="android.widget.FrameLayout" clickable="false" bounds="[0,0][1200,2670]"/>',
                '<node package="cn.damai" text="我知道了" clickable="true" bounds="[100,700][1100,1900]"/>',
            ):
                with self.subTest(frame=name, overlay=overlay):
                    root = ET.fromstring(recorded(name))
                    root.append(ET.fromstring(overlay))
                    inputs, _, _ = self.replay([
                        (0, None, app.DAMAI_PACKAGE),
                        (.16, ET.tostring(root, encoding='unicode'), app.DAMAI_PACKAGE),
                        (.32, None, app.DAMAI_PACKAGE),
                    ])
                    self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_known_progress_requires_exact_text_and_no_new_controls(self):
        """已知进度层变成新提示或出现操作按钮时，不能继续按普通加载处理。"""
        for change in ('text', 'clickable', 'nested_notice'):
            root = ET.fromstring(recorded('submitting' if change != 'nested_notice' else 'detail_transition'))
            if change == 'nested_notice':
                next(n for n in root if n.get('package') == app.DAMAI_PACKAGE).append(
                    ET.Element('node', package=app.DAMAI_PACKAGE, text='未知提示'))
            else:
                label = next(n for n in root.iter('node')
                             if n.get('resource-id') == 'cn.damai:id/tv_progress_dialog_label')
                label.set(change, '未知提示' if change == 'text' else 'true')
            with self.subTest(change=change):
                inputs, _, _ = self.replay([
                    (0, None, app.DAMAI_PACKAGE),
                    (.16, ET.tostring(root, encoding='unicode'), app.DAMAI_PACKAGE),
                ])
                self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_recorded_transition_keeps_verification_and_alipay_handling(self):
        """转场中的验证仍只返回一次，前台进入支付宝后不再发下一次点击。"""
        root = ET.fromstring(recorded('detail_transition'))
        root.append(ET.fromstring(RISK)[0])
        inputs, state, _ = self.replay([
            (0, None, app.DAMAI_PACKAGE), (.16, ET.tostring(root, encoding='unicode'), app.DAMAI_PACKAGE),
            (.32, recorded('blank_transition'), app.DAMAI_PACKAGE), (.48, ORDER, app.ALIPAY_PACKAGE),
        ])
        self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT), (.16, 'back')])
        self.assertEqual(state.status, 'alipay_reached')

    def test_recorded_congestion_still_takes_over_normal_clicking(self):
        """实战拥挤页和重试弹窗仍接管为实测点，主流程恢复后才继续点右下角。"""
        for name, point, times in (('refresh', (1066, 1822), (.16, .32)), ('retry', (600, 1607), (.16,))):
            with self.subTest(frame=name):
                inputs, _, _ = self.replay([
                    (0, None, app.DAMAI_PACKAGE), (.16, recorded(name), app.DAMAI_PACKAGE),
                    (.32, recorded(name), app.DAMAI_PACKAGE), (.48, recorded('order'), app.DAMAI_PACKAGE),
                ])
                self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT), *((t, point) for t in times),
                                         (.48, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_refresh_and_retry_switch_intervals_and_points(self):
        """两种重试按钮连续切换时，按当前按钮的时间门槛和实测位置派发。"""
        refresh, retry = recorded('refresh'), recorded('retry')
        frames = [(0, refresh), (.149, refresh), (.150, refresh),
                  (.300, retry), (1.149, retry), (1.150, retry),
                  (1.299, refresh), (1.301, refresh)]
        inputs, _, _ = self.replay([(t, frame, app.DAMAI_PACKAGE) for t, frame in frames])
        self.assertEqual(inputs, [(0, (1066, 1822)), (.150, (1066, 1822)),
                                  (1.150, (600, 1607)), (1.301, (1066, 1822))])

    def test_refresh_cannot_tap_after_button_disabled_or_verification(self):
        """按钮失效先暂停，安全验证结束本场；不沿用上次读到的刷新坐标。"""
        disabled = REFRESH.replace('enabled="true"', 'enabled="false"')
        frames = [(0, REFRESH), (.16, disabled), (.32, UNKNOWN), (.48, RISK), (.64, REFRESH)]
        inputs, state, _ = self.replay([(t, frame, app.DAMAI_PACKAGE) for t, frame in frames],
                                       stop_on_verification=True)
        self.assertEqual(inputs, [(0, (754, 1700))])
        self.assertEqual(state.status, 'verification_required')

    def test_unknown_and_probe_failure_pause_until_normal_page_returns(self):
        """未知画面/USB 读取失败都暂停；一次异常不会让 controlled 永久卡住。"""
        for frame in (UNKNOWN, RuntimeError('device disconnected')):
            with self.subTest(frame=frame):
                inputs, _, _ = self.replay([
                    (0, None, app.DAMAI_PACKAGE), (.15, frame, app.DAMAI_PACKAGE),
                    (.30, None, app.DAMAI_PACKAGE), (.45, ORDER, app.DAMAI_PACKAGE),
                ])
                self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT),
                                         (.45, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_sold_out_stops_without_clicking_return_to_selection(self):
        inputs, state, _ = self.replay([
            (0, None, app.DAMAI_PACKAGE), (.15, SOLD_OUT, app.DAMAI_PACKAGE),
            (.30, SOLD_OUT, app.DAMAI_PACKAGE),
        ])
        self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT)])
        self.assertEqual(state.status, 'terminal_no_retry_option')

    def test_detail_launch_stops_on_verification_for_manual_handoff(self):
        """固定入口在验证页原地结束，不派发返回或后续输入。"""
        inputs, state, _ = self.replay([
            (0, None, app.DAMAI_PACKAGE), (.15, RISK, app.DAMAI_PACKAGE), (.30, DETAIL, app.DAMAI_PACKAGE),
        ], stop_on_verification=True)
        self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT)])
        self.assertEqual(state.status, "verification_required")

    @unittest.skipUnless(sys.platform == "darwin", "验证提示 OCR 使用 macOS Vision")
    def test_suzhou_blank_webview_then_visible_warning_stops_before_retry(self):
        """用公开提示文字复现空 WebView 后的验证页，等待 OCR 时暂停，命中即停。"""
        from PIL import Image, ImageDraw, ImageFont
        from button_text import ButtonTextReader

        blank = Image.new("RGB", (1200, 2670), "white")
        warning = blank.copy()
        font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 44)
        ImageDraw.Draw(warning).multiline_text(
            (80, blank.height // 6 + 80), "操作过于频繁，\n完成验证后可继续购票",
            fill="black", font=font, spacing=12,
        )
        screens = iter((blank, warning))
        webview = '<hierarchy><node package="cn.damai" class="android.webkit.WebView" text=""/></hierarchy>'
        with ButtonTextReader() as reader:
            def check(device, controlled, requests):
                self.assertTrue(controlled.is_set())
                self.assertIsNone(requests.pop())
                device.screenshot.return_value = next(screens)
                return app.visible_verification_prompt(device, reader)

            taps, state, _ = self.replay([
                (0, None, app.DAMAI_PACKAGE), (.15, webview, app.DAMAI_PACKAGE),
                (.30, webview, app.DAMAI_PACKAGE), (.45, RETRY, app.DAMAI_PACKAGE),
            ], stop_on_verification=True, check_unknown=check)
        self.assertEqual(taps, [(0, app.XIAOMI_15_MAIN_ACTION_POINT)])
        self.assertEqual(state.status, "verification_required")
        self.assertEqual(state.last_page, app.PAGE_RISK_VERIFICATION)
        self.assertEqual(state.risk_verification_hits, 1)
        self.assertEqual(state.risk_back_count, 0)

    def test_verification_only_backs_once_even_with_intermediate_unknown_or_error(self):
        for frame in (UNKNOWN, RuntimeError('device disconnected')):
            with self.subTest(frame=frame):
                inputs, _, _ = self.replay([
                    (0, None, app.DAMAI_PACKAGE), (.15, RISK, app.DAMAI_PACKAGE),
                    (.30, frame, app.DAMAI_PACKAGE), (.45, RISK, app.DAMAI_PACKAGE),
                    (.60, DETAIL, app.DAMAI_PACKAGE), (.75, ORDER, app.DAMAI_PACKAGE),
                ])
                self.assertEqual(inputs, [(0, app.XIAOMI_15_MAIN_ACTION_POINT), (.15, 'back'),
                                         (.75, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_foreground_error_unknown_or_other_app_never_receives_input(self):
        for package in (None, 'com.android.settings', RuntimeError('adb closed')):
            with self.subTest(package=package):
                inputs, _, _ = self.replay([
                    (0, None, package), (.15, None, app.DAMAI_PACKAGE),
                    (.30, None, app.ALIPAY_PACKAGE),
                ])
                self.assertEqual(inputs, [(.15, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_stop_or_deadline_during_foreground_query_prevents_dispatch(self):
        """查询可以阻塞：返回时若已停止或超时，任何输入动作都不能再发送。"""
        for reason in ('cancel', 'deadline'):
            for action in ('main', 'retry', 'back', 'seat'):
                with self.subTest(reason=reason, action=action):
                    now = [10.0]
                    stop, controlled = threading.Event(), threading.Event()
                    queue, state, shell = app.TapRequestQueue(), app.RuntimeState(), Mock()
                    retry = app.RetryPointHolder(clock=lambda: now[0])
                    retry.set((500, 1800))
                    if action != 'main':
                        controlled.set()
                        queue.request(action)

                    def query():
                        if reason == 'cancel':
                            stop.set()
                        else:
                            now[0] = 11.0
                        return app.DAMAI_PACKAGE

                    with patch.object(app.time, 'perf_counter', side_effect=lambda: now[0]):
                        app.tap_loop(app.Mode0Schedule(10, 10, 7, 11), state, stop, controlled,
                                     queue, shell, Mock(query_package=query), retry_points=retry,
                                     seat_points=retry,
                                     continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT)
                    shell.tap.assert_not_called()
                    shell.back.assert_not_called()
                    self.assertTrue(stop.is_set())
                    if reason == 'deadline':
                        self.assertEqual(state.status, 'timeout')

    def test_background_pause_after_selecting_main_cancels_that_input(self):
        """主点击已被选中后才发现异常，也要在最终发送前再检查暂停信号。"""
        stop, controlled = threading.Event(), threading.Event()
        holder, shell = Mock(), Mock()
        holder.changed.clear.side_effect = controlled.set
        query = Mock()
        query.query_package.side_effect = [app.DAMAI_PACKAGE, app.ALIPAY_PACKAGE]
        now = app.time.perf_counter()
        app.tap_loop(app.Mode0Schedule(now, now, now, now + 10), app.RuntimeState(),
                     stop, controlled, app.TapRequestQueue(), shell, query,
                     main_points=holder, continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT)
        shell.tap.assert_not_called()

    def test_cancel_before_sale_or_alipay_with_pending_retry_sends_nothing(self):
        for reason in ('before_sale', 'alipay'):
            with self.subTest(reason=reason):
                stop, controlled = threading.Event(), threading.Event()
                queue, shell, query = app.TapRequestQueue(), Mock(), Mock()
                queue.request('retry')
                query.query_package.return_value = app.ALIPAY_PACKAGE
                if reason == 'before_sale':
                    stop.set()
                now = app.time.perf_counter()
                app.tap_loop(app.Mode0Schedule(now, now, now, now + 10), app.RuntimeState(),
                             stop, controlled, queue, shell, query,
                             continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT)
                shell.tap.assert_not_called()
                shell.back.assert_not_called()
                if reason == 'before_sale':
                    query.query_package.assert_not_called()
                else:
                    self.assertIsNone(queue.pop())

    def test_failed_initial_probe_does_not_release_clicking_at_sale_time(self):
        """到点不覆盖预检异常，确认回到正常页面才允许启动。"""
        inputs, _, _ = self.replay([
            (0, None, app.DAMAI_PACKAGE), (.15, DETAIL, app.DAMAI_PACKAGE),
        ], initial=RuntimeError('initial probe failed'))
        self.assertEqual(inputs, [(.15, app.XIAOMI_15_MAIN_ACTION_POINT)])

    def test_real_wait_until_never_returns_before_target_and_can_be_cancelled(self):
        """只替换等待和时钟，验证实际定时等待函数不会提前返回或忽略取消。"""
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                now, cancelled = [0.0], [False]
                event = Mock()
                event.is_set.side_effect = lambda: cancelled[0]

                def wait(duration):
                    self.assertGreater(duration, 0)
                    self.assertLessEqual(duration, .1)
                    now[0] += duration
                    cancelled[0] = cancel

                event.wait.side_effect = wait
                with patch.object(app.time, 'perf_counter', side_effect=lambda: now[0]):
                    self.assertEqual(app.wait_until(.35, event), not cancel)
                self.assertAlmostEqual(now[0], .1 if cancel else .35)

    def test_input_failure_stops_the_worker(self):
        """持久点击 shell 写入失败时退出，不能吞掉错误继续排队输入。"""
        now = app.time.perf_counter()
        stop, state, shell = threading.Event(), app.RuntimeState(), Mock()
        shell.tap.side_effect = RuntimeError('input shell closed')
        app.tap_loop(app.Mode0Schedule(now, now, now, now + 10), state, stop,
                     threading.Event(), app.TapRequestQueue(), shell,
                     Mock(query_package=lambda: app.DAMAI_PACKAGE),
                     continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT)
        self.assertTrue(stop.is_set())
        self.assertEqual(state.status, 'error')
        self.assertIn('input shell closed', state.worker_error)
        self.assertEqual(shell.tap.call_count, 1)

    def test_visual_gate_ignores_mac_sale_time_and_keeps_continuous_flow(self):
        """文案可以早于或晚于电脑到点：事件前零输入，放行后立即连点，支付宝停。"""
        for release in (9.5, 10.5):
            with self.subTest(release=release):
                now, calls = [9.0], []
                stop, ready = threading.Event(), Mock()
                shell, query = Mock(), Mock()

                def wait(_):
                    self.assertEqual(calls, [])
                    now[0] += .5
                    return now[0] >= release

                ready.wait.side_effect = wait
                packages = iter((app.DAMAI_PACKAGE, app.DAMAI_PACKAGE, app.ALIPAY_PACKAGE))
                query.query_package.side_effect = lambda: next(packages)
                def tap(point):
                    calls.append((now[0], point))
                    now[0] += .16
                shell.tap.side_effect = tap
                state = app.RuntimeState()
                with patch.object(app.time, 'perf_counter', side_effect=lambda: now[0]):
                    app.tap_loop(app.Mode0Schedule(10, 10, 9, 20), state, stop,
                                 threading.Event(), app.TapRequestQueue(), shell, query,
                                 continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT,
                                 purchase_ready=ready)
                self.assertEqual(calls, [(release, app.XIAOMI_15_MAIN_ACTION_POINT),
                                         (release + .16, app.XIAOMI_15_MAIN_ACTION_POINT)])
                self.assertEqual(state.status, 'alipay_reached')

    def test_visual_gate_timeout_cancel_and_alipay_never_send_input(self):
        """等待文案超时/取消，以及放行时已在支付宝，都不能发出首击。"""
        for reason in ('timeout', 'cancel', 'alipay'):
            with self.subTest(reason=reason):
                stop, ready = threading.Event(), Mock()
                shell, query, state = Mock(), Mock(), app.RuntimeState()
                def wait(_):
                    if reason == 'cancel':
                        stop.set()
                    return reason == 'alipay'
                ready.wait.side_effect = wait
                query.query_package.return_value = app.ALIPAY_PACKAGE
                with patch.object(app.time, 'perf_counter', return_value=10):
                    app.tap_loop(app.Mode0Schedule(9, 9, 9, 10 if reason == 'timeout' else 20),
                                 state, stop, threading.Event(), app.TapRequestQueue(), shell, query,
                                 continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT,
                                 purchase_ready=ready)
                shell.tap.assert_not_called()
                shell.back.assert_not_called()
                self.assertTrue(stop.is_set())


class ScopedFirstTapTests(unittest.TestCase):
    def setUp(self):
        """只模拟 Android 输入回执和前台查询，使用真实视觉放行事件和点击循环。"""
        self.queue, self.state = app.TapRequestQueue(), app.RuntimeState()
        self.stop, self.controlled, self.ready = threading.Event(), threading.Event(), threading.Event()
        self.ready.set()
        self.input = Mock(spec=app.ScopedInput)
        self.input.uid = 10123
        self.query = Mock()
        self.query.query_package.return_value = app.ALIPAY_PACKAGE

    def run_loop(self, **points):
        """驱动一次已看到购买文案的首击，最长一秒后自动退出。"""
        now = app.time.perf_counter()
        app.tap_loop(app.Mode0Schedule(now, now, now, now + 1), self.state, self.stop,
                     self.controlled, self.queue, self.input, self.query,
                     continuous_main_point=app.XIAOMI_15_MAIN_ACTION_POINT, purchase_ready=self.ready, **points)

    def test_cashier_before_visual_trigger_stops_without_waiting_for_a_tap(self):
        """已有后台探测在等待购买文案期间也能发现支付宝、清空动作并停止。"""
        self.ready.clear()
        self.queue.request("retry")
        page_probe = Mock()
        page_probe.read.return_value = "alipay_reached"
        now = app.time.perf_counter()
        app.run_tail_probe_loop(schedule=app.Mode0Schedule(now, now, now, now + 2),
            state=self.state, stop_event=self.stop, controlled_event=self.controlled, requests=self.queue,
            controller=app.Mode0StateController(continuous_main=True), page_probe=page_probe,
            wait_until=app.wait_until, state_check_interval_s=0, risk_state_check_interval_s=.25,
            purchase_ready=self.ready)
        self.assertTrue(self.stop.is_set())
        self.assertTrue(self.controlled.is_set())
        self.assertIsNone(self.queue.pop())
        self.state.finish("clicking")  # 点击线程可能正好也收到了先前排队的购买帧。
        self.assertEqual(self.state.status, "alipay_reached")

    def test_scoped_first_tap_precedes_query_and_alipay_stops_next_action(self):
        """首击即时派发，随后进支付宝，即使已有重试请求也不再点。"""
        def tap(_):
            self.query.query_package.assert_not_called()
            self.queue.request('retry')

        self.input.tap.side_effect = tap
        with patch.object(app.run_log, 'emit') as log:
            self.run_loop()
        self.input.tap.assert_called_once_with(app.XIAOMI_15_MAIN_ACTION_POINT)
        self.query.query_package.assert_called_once()
        self.assertEqual(self.state.status, 'alipay_reached')
        self.assertIsNone(self.queue.pop())
        event, = [call.kwargs for call in log.call_args_list if call.args[0] == 'input_dispatched']
        self.assertEqual(event['input_guard'], 'android_target_uid')
        self.assertIsNone(event['query_ms'])

    def test_android_rejection_stops_even_if_window_changes_again(self):
        """拒收后的前台查询只解释停止原因。即使这时已切回大麦，也不重发。"""
        for package, status in ((app.ALIPAY_PACKAGE, 'alipay_reached'), (app.DAMAI_PACKAGE, 'input_target_mismatch'),
                                (None, 'input_target_mismatch')):
            with self.subTest(package=package):
                self.state = app.RuntimeState()  # 各场独立启动，已进支付宝的上一场保持终态。
                self.stop.clear()
                self.input.tap.reset_mock()
                def diagnose():
                    self.assertTrue(self.stop.is_set())
                    return package
                self.query.query_package.side_effect = diagnose

                def reject(_):
                    self.queue.request('submit')
                    raise app.TargetMismatch('target window changed')

                self.input.tap.side_effect = reject
                self.run_loop()
                self.input.tap.assert_called_once_with(app.XIAOMI_15_MAIN_ACTION_POINT)
                self.assertTrue(self.stop.is_set())
                self.assertEqual(self.state.status, status)
                self.assertEqual(self.state.main_tap_count, 0)
                self.assertIsNone(self.queue.pop())

    def test_switch_to_alipay_between_retry_check_and_delivery_stops(self):
        """前台刚确认是大麦，重试触摸落下前已切支付宝，也不能透传或重发。"""
        retry_points = app.RetryPointHolder()
        retry_points.set((600, 1607))
        def tap(point):
            if point == app.XIAOMI_15_MAIN_ACTION_POINT:
                self.controlled.set()
                self.queue.request('retry')
                return
            self.assertEqual(point, (600, 1607))
            self.queue.request('submit')
            raise app.TargetMismatch('window switched before retry')

        self.input.tap.side_effect = tap
        self.query.query_package.side_effect = [app.DAMAI_PACKAGE, app.ALIPAY_PACKAGE]
        self.run_loop(retry_points=retry_points)
        self.assertEqual(self.input.tap.call_count, 2)
        self.assertEqual(self.state.main_tap_count, 1)
        self.assertEqual(self.state.retry_tap_count, 0)
        self.assertIsNone(self.queue.pop())
        self.assertEqual(self.state.status, 'alipay_reached')

    def test_ready_while_paused_never_sends_first_tap(self):
        """购买文案与后台暂停信号同时到达时，以暂停为准。"""
        self.controlled.set()
        self.run_loop()
        self.input.tap.assert_not_called()
        self.assertEqual(self.state.status, 'alipay_reached')


if __name__ == '__main__':
    unittest.main()
