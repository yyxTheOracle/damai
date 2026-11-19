from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from typing import Callable
from xml.etree import ElementTree as ET

import uiautomator2 as u2
import run_log

from damai_android_tail import run_tail_probe_loop
from device_support import SCREEN_SIZES, XIAOMI_15_MODEL, XIAOMI_15_MAIN_ACTION_POINT, default_adb_path
from scoped_input import ScopedInput, TargetMismatch
from xiaomi_ui import ALIPAY_PACKAGE, parse_page, screen_size_matches_xml


# Device and safety configuration.
# REDMI K80（24117RK2CC）沿用 1440 x 3200 的标定坐标，无需缩放。
# 注意：K80 默认处于 FHD+（Override 1080x2400），必须切到 WQHD+（1440x3200）
# 预检才能通过，请先运行 tools\precheck_K80.py。
# 设备序列号：留空 = 自动选择唯一一台已授权的 adb 设备；
# 也可以用环境变量 DAMAI_DEVICE_SERIAL 指定（多台设备同时连接时必须指定）。
DEVICE_SERIAL = ""
EXPECTED_SCREEN_SIZE = (1440, 3200)
# None 会按当前操作系统查找项目内 Platform-Tools 或 PATH，也可指定绝对路径。
ADB_PATH = None
DAMAI_PACKAGE = "cn.damai"

# Calibrated/inferred tap points for 1440 x 3200 only.
# MAIN_ACTION_POINT is inside all three proven bottom-right actions:
# detail purchase entry, ticket-panel Confirm, and order-page Immediate Submit.
MAIN_ACTION_POINT = (1119, 3088)
# Inferred from the supplied photographed dialog. The primary retry button is
# [244,1891][1195,2041] in every Android retry-dialog capture under
# artifacts/8_14. Use the measured center, not the old photographed estimate.
# K80 修复后：点击「继续尝试」时不再使用这个固定坐标，而是用探测线程实时读取的
# 按钮 bounds 中心（见 RetryPointHolder）。12:20 实战证明库存不足弹窗的按钮
# [244,1975][1195,2125] 离此点仅 9px。此常量只保留给 tools\inspect_point_K80.py 参考。
RETRY_DIALOG_POINT = (720, 1966)

# All timing knobs are kept together for quick tuning.
PRECLICK_LEAD_S = 2.0
CLICK_INTERVAL_S = 0.150
FIRST_STATE_CHECK_AFTER_S = 0.0
STATE_CHECK_INTERVAL_S = 0.0
RISK_STATE_CHECK_INTERVAL_S = 0.25
MAX_RUNTIME_AFTER_SALE_S = 10 * 60.0
THREAD_JOIN_TIMEOUT_S = 3.0

# K80 修复（2026-10-14 11:30 实战）：第 2 次点「继续尝试」后弹窗没有消失，
# 原状态机对同一弹窗只点一次，导致空等 30 秒。现在弹窗持续存在超过
# RETRY_REPEAT_AFTER_S 秒就再点一次（只在探测到弹窗仍在时才点）。
RETRY_REPEAT_AFTER_S = 1.0
REFRESH_REPEAT_AFTER_S = 0.150  # 小米拥挤页「努力刷新」持续可点击时的重复间隔。
# 选座页「确认选座」按钮的重复点击间隔（按钮不消失就再点）。
SEAT_REPEAT_AFTER_S = 1.5
# 同一弹窗持续存在时，自动保存界面层级 + 截图到 artifacts\，最多保存几份。
DIALOG_SNAPSHOT_LIMIT = 3
# K80 修复（2026-10-14 13:00 实战）：确认页一直被判为 order_page_loading，
# 「立即提交」始终探测不到，空等 10 分钟且没有任何现场。现在这类非弹窗页面
# 停留超过 STUCK_PAGE_SNAPSHOT_AFTER_S 秒也会保存现场（只读，不点击）。
STUCK_PAGE_SNAPSHOT_AFTER_S = 3.0
STUCK_PAGE_SNAPSHOT_LIMIT = 3
STUCK_SNAPSHOT_PAGES = ("order_page_loading", "damai_other", "order_page", "seat_page")
# 现场里额外记录屏幕底部区域（按钮所在位置）的控件文字，便于看出「立即提交」变成了什么。
BOTTOM_AREA_TOP_RATIO = 0.80
# 页面无变化时，每隔多少秒输出一条 heartbeat 日志。
HEARTBEAT_INTERVAL_S = 1.0
ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"

# UI identifiers used only for in-memory state checks.
ORDER_TITLE_RESOURCE = "cn.damai:id/order_activity_title"
RETRY_RESOURCE = "cn.damai:id/damai_theme_dialog_confirm_btn"
BACK_REDIRECT_RESOURCE = "cn.damai:id/damai_theme_dialog_cancel_btn"
RISK_RESOURCE = "cn.damai:id/water_mark_view_id"
SUBMIT_TEXT = "立即提交"
# 选座票：进入选座页（TSeatActivity）后，要点底部「确认选座」才能进确认页。
# 2026-10-14 15:00 实战：选座票卡在这一页，盲点坐标 (1119,3088) 擦着按钮上边缘过不去。
SEAT_CONFIRM_RESOURCE = "cn.damai:id/seat_confirm_order"
SEAT_CONFIRM_TEXTS = ("确认选座", "确认选座并支付")
TIP_RESOURCE = "cn.damai:id/damai_theme_dialog_tip_content"
# K80 修复（2026-10-14 12:20 实战）：库存不足弹窗的「返回重新选购」与「继续尝试」
# 共用 damai_theme_dialog_confirm_btn，只看 resource-id 会把它误判成重试弹窗。
# 现在必须按钮文字精确等于「继续尝试」才算重试弹窗。
RETRY_TEXT = "继续尝试"
SOLD_OUT_BUTTON_KEYWORDS = ("返回", "重新选购")
SOLD_OUT_TIP_KEYWORDS = ("库存不足", "已售罄", "售罄")
# 连续几次探测到库存不足弹窗就停止（防止瞬时误判）。
SOLD_OUT_CONFIRM_COUNT = 2
# 「继续尝试」按钮中心点的有效期：超过这个时间没被探测重新确认就不点。
RETRY_POINT_MAX_AGE_S = 1.0

PAGE_RETRY_DIALOG = "retry_dialog"
PAGE_REFRESH = "refresh_page"
RETRY_PAGES = (PAGE_RETRY_DIALOG, PAGE_REFRESH)
PAGE_SEAT_PAGE = "seat_page"
PAGE_SOLD_OUT_DIALOG = "sold_out_dialog"
PAGE_UNKNOWN_DIALOG = "unknown_dialog"
PAGE_TERMINAL_DIALOG = "terminal_dialog"
PAGE_ORDER_PAGE = "order_page"
PAGE_ORDER_LOADING = "order_page_loading"
PAGE_PURCHASE_TRANSITION = "purchase_transition"
PAGE_RISK_VERIFICATION = "risk_verification"
PAGE_DAMAI_OTHER = "damai_other"


@dataclass(frozen=True)
class Mode0Schedule:
    sale_monotonic: float
    click_start_monotonic: float
    first_probe_monotonic: float
    deadline_monotonic: float


@dataclass
class RuntimeState:
    status: str = "waiting"
    last_package: str = ""
    last_page: str = "unknown"
    main_tap_count: int = 0
    retry_tap_count: int = 0
    probe_count: int = 0
    retry_dialog_hits: int = 0
    terminal_dialog_hits: int = 0
    risk_verification_hits: int = 0
    risk_back_count: int = 0
    risk_wait_seconds: float = 0.0
    state_transitions: list[dict[str, object]] = field(default_factory=list)
    foreground_check_errors: int = 0
    worker_error: str | None = None
    first_tap_wall_time: str | None = None
    risk_wait_started: float | None = None
    skipped_retry_count: int = 0
    seat_tap_count: int = 0
    skipped_seat_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update_package(self, package: str) -> None:
        with self.lock:
            self.last_package = package

    def record_tap(self, retry: bool) -> None:
        with self.lock:
            if self.first_tap_wall_time is None:
                self.first_tap_wall_time = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
            if retry:
                self.retry_tap_count += 1
            else:
                self.main_tap_count += 1

    def record_risk_back(self) -> None:
        with self.lock:
            self.risk_back_count += 1

    def record_skipped_retry(self) -> None:
        with self.lock:
            self.skipped_retry_count += 1

    def record_seat_tap(self) -> None:
        with self.lock:
            if self.first_tap_wall_time is None:
                self.first_tap_wall_time = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
            self.seat_tap_count += 1

    def record_skipped_seat(self) -> None:
        with self.lock:
            self.skipped_seat_count += 1

    def record_probe(self, page: str) -> None:
        with self.lock:
            self.probe_count += 1
            self.last_page = page

    def record_dialog(self, page: str) -> None:
        with self.lock:
            if page in RETRY_PAGES:
                self.retry_dialog_hits += 1
            elif page == PAGE_TERMINAL_DIALOG:
                self.terminal_dialog_hits += 1

    def transition(self, previous: str, current: str, page: str) -> None:
        if previous == current:
            return
        with self.lock:
            self.state_transitions.append(
                {
                    "from": previous,
                    "to": current,
                    "page": page,
                    "wall_time": datetime.now().astimezone().isoformat(
                        timespec="milliseconds"
                    ),
                }
            )

    def enter_risk_wait(self, now: float) -> None:
        with self.lock:
            if self.risk_wait_started is None:
                self.risk_verification_hits += 1
                self.risk_wait_started = now

    def leave_risk_wait(self, now: float) -> None:
        with self.lock:
            if self.risk_wait_started is not None:
                self.risk_wait_seconds += now - self.risk_wait_started
                self.risk_wait_started = None

    def record_foreground_error(self) -> None:
        with self.lock:
            self.foreground_check_errors += 1

    def finish(self, status: str, error: str | None = None) -> None:
        with self.lock:
            # 支付宝已经出现时，其他线程稍晚返回的启动、超时或错误不能覆盖停机原因。
            if self.status == "alipay_reached":
                return
            self.status = status
            if error is not None:
                self.worker_error = error

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            risk_wait_seconds = self.risk_wait_seconds
            if self.risk_wait_started is not None:
                risk_wait_seconds += time.perf_counter() - self.risk_wait_started
            return {
                "status": self.status,
                "last_package": self.last_package,
                "last_page": self.last_page,
                "main_tap_count": self.main_tap_count,
                "retry_tap_count": self.retry_tap_count,
                "probe_count": self.probe_count,
                "retry_dialog_hits": self.retry_dialog_hits,
                "terminal_dialog_hits": self.terminal_dialog_hits,
                "risk_verification_hits": self.risk_verification_hits,
                "risk_back_count": self.risk_back_count,
                "risk_wait_seconds": round(risk_wait_seconds, 3),
                "state_transitions": list(self.state_transitions),
                "foreground_check_errors": self.foreground_check_errors,
                "foreground_query_errors": self.foreground_check_errors,
                "worker_error": self.worker_error,
                "first_tap_wall_time": self.first_tap_wall_time,
            }


@dataclass(frozen=True)
class StateDecision:
    previous_phase: str
    phase: str
    action: str | None = None
    status: str | None = None
    clear_pending: bool = False
    dialog_hit: str | None = None
    entered_risk: bool = False
    left_risk: bool = False


class Mode0StateController:
    """Pure state machine for the post-submit retry loop."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter, *, continuous_main: bool = False,
                 stop_on_verification: bool = False) -> None:
        # 小米正常购票页只观察异常，订单加载和提交按钮不再阻塞固定点连点。
        self.continuous_main = continuous_main
        self.stop_on_verification = stop_on_verification
        self.phase = "blind"
        self.terminal_streak = 0
        self.clock = clock
        self.last_retry_at: float | None = None
        self.retry_repeat_count = 0
        self.last_seat_at: float | None = None
        self.seat_click_count = 0
        self.sold_out_streak = 0
        self.unknown_dialog_hits = 0

    @property
    def controlled(self) -> bool:
        return self.phase != "blind"

    def observe(self, page: str) -> StateDecision:
        previous = self.phase
        entered_risk = False
        left_risk = False

        if self.continuous_main:
            if page == "alipay_reached":
                self.phase = "paused"
                return StateDecision(previous, self.phase, status=page, clear_pending=True)
            if page in (PAGE_DAMAI_OTHER, PAGE_ORDER_LOADING, PAGE_ORDER_PAGE, PAGE_PURCHASE_TRANSITION):
                if previous == "risk_back_requested" and page == PAGE_DAMAI_OTHER:
                    return StateDecision(previous, self.phase, clear_pending=True)
                self.phase = "blind"
                self.sold_out_streak = self.terminal_streak = 0
                return StateDecision(previous, self.phase, clear_pending=previous != "blind",
                                     left_risk=previous == "risk_back_requested")
            if page == PAGE_TERMINAL_DIALOG:
                self.phase = "await_result"
            elif page not in RETRY_PAGES and page != PAGE_RISK_VERIFICATION:
                # 未知页、读失败和选座都必须显式暂停，不能再靠主点位过期兜底。
                if self.phase != "risk_back_requested":
                    self.phase = "paused"
                if page.startswith("probe_error"):
                    return StateDecision(previous, self.phase, clear_pending=True)

        if page == PAGE_RISK_VERIFICATION:
            if self.stop_on_verification:
                # 固定入口在验证页结束，让用户在原页面接手。
                self.phase = "paused"
                return StateDecision(previous, self.phase, status="verification_required",
                                     clear_pending=True, entered_risk=True)
            action = None
            if self.phase != "risk_back_requested":
                self.phase = "risk_back_requested"
                entered_risk = True
                action = "back"
            self.terminal_streak = 0
            return StateDecision(
                previous,
                self.phase,
                action=action,
                clear_pending=True,
                entered_risk=entered_risk,
            )

        # 选座页：点「确认选座」进入确认页。座位由大麦已分配/已锁定，脚本不挑座位。
        # 每 SEAT_REPEAT_AFTER_S 秒最多点一次，按钮由探测线程实时定位。
        if page == PAGE_SEAT_PAGE:
            self.sold_out_streak = 0
            self.terminal_streak = 0
            now = self.clock()
            if (
                self.last_seat_at is None
                or now - self.last_seat_at >= SEAT_REPEAT_AFTER_S
            ):
                self.last_seat_at = now
                self.seat_click_count += 1
                return StateDecision(previous, self.phase, action="seat")
            return StateDecision(previous, self.phase)

        # 库存不足弹窗：绝不点击，连续确认 SOLD_OUT_CONFIRM_COUNT 次后停止。
        if page == PAGE_SOLD_OUT_DIALOG:
            self.sold_out_streak += 1
            self.terminal_streak = 0
            status = (
                "terminal_no_retry_option"
                if self.sold_out_streak >= SOLD_OUT_CONFIRM_COUNT
                else None
            )
            return StateDecision(
                previous,
                self.phase,
                status=status,
                clear_pending=True,
                dialog_hit=PAGE_TERMINAL_DIALOG if self.sold_out_streak == 1 else None,
                left_risk=self.phase == "risk_back_requested",
            )
        self.sold_out_streak = 0

        # 文字无法识别的弹窗：绝不点击，只清空待点请求并等待。
        if page == PAGE_UNKNOWN_DIALOG:
            self.unknown_dialog_hits += 1
            return StateDecision(previous, self.phase, clear_pending=True)

        if self.phase == "risk_back_requested":
            if page not in (
                PAGE_RETRY_DIALOG,
                PAGE_REFRESH,
                PAGE_TERMINAL_DIALOG,
                PAGE_ORDER_PAGE,
                PAGE_ORDER_LOADING,
            ):
                return StateDecision(previous, self.phase, clear_pending=True)
            left_risk = True
            self.phase = (
                "await_result"
                if page == PAGE_TERMINAL_DIALOG
                else "await_submit"
            )

        if page in RETRY_PAGES:
            self.terminal_streak = 0
            now = self.clock()
            if self.phase != "retry_clicked":
                self.phase = "retry_clicked"
                self.last_retry_at = now
                return StateDecision(
                    previous,
                    self.phase,
                    action="retry",
                    dialog_hit=page,
                    left_risk=left_risk,
                )
            # 每次按当前可见按钮选择间隔，「继续尝试」仍需等满一秒。
            interval = REFRESH_REPEAT_AFTER_S if page == PAGE_REFRESH else RETRY_REPEAT_AFTER_S
            if self.last_retry_at is None or now >= self.last_retry_at + interval:
                self.last_retry_at = now
                self.retry_repeat_count += 1
                return StateDecision(
                    previous,
                    self.phase,
                    action="retry",
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if page == PAGE_TERMINAL_DIALOG:
            if self.phase == "await_result":
                self.terminal_streak += 1
                status = (
                    "terminal_no_retry_option"
                    if self.terminal_streak >= 2
                    else None
                )
                return StateDecision(
                    previous,
                    self.phase,
                    status=status,
                    clear_pending=True,
                    dialog_hit=(
                        PAGE_TERMINAL_DIALOG if self.terminal_streak == 1 else None
                    ),
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        self.terminal_streak = 0
        if page == PAGE_ORDER_PAGE:
            if self.phase != "await_result":
                self.phase = "await_result"
                return StateDecision(
                    previous,
                    self.phase,
                    action="submit",
                    left_risk=left_risk,
                )
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if page == PAGE_ORDER_LOADING:
            if self.phase in ("blind", "retry_clicked"):
                self.phase = "await_submit"
            return StateDecision(previous, self.phase, left_risk=left_risk)

        if self.phase == "retry_clicked":
            self.phase = "await_submit"
        return StateDecision(previous, self.phase, left_risk=left_risk)


class TapRequestQueue:
    """Single-slot request queue; the ADB worker is its only consumer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: str | None = None
        self._restored = False
        self.changed = threading.Event()

    def request(self, action: str) -> bool:
        if action not in ("main", "submit", "retry", "back", "seat"):
            raise ValueError(f"未知点击动作：{action}")
        with self._lock:
            if self._pending is not None and not self._restored:
                return False
            self._pending = action
            self._restored = False
            self.changed.set()
            return True

    def pop(self) -> str | None:
        with self._lock:
            action = self._pending
            self._pending = None
            self._restored = False
            self.changed.clear()
            return action

    def clear(self) -> None:
        with self._lock:
            self._pending = None
            self._restored = False
            self.changed.clear()

    def restore(self, action: str) -> None:
        """坐标暂不可用时保留请求，让新产生的页面操作优先执行。"""
        with self._lock:
            if self._pending is None:
                self._pending = action
                self._restored = True
                self.changed.set()


class PersistentTapShell:
    """Dispatch low-overhead ADB taps through one persistent device shell."""

    def __init__(self, adb_path: Path, serial: str) -> None:
        self.process = subprocess.Popen(
            [str(adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None:
            raise RuntimeError("无法创建持久 ADB shell")

    def tap(self, point: tuple[int, int]) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.process.stdin is not None
        self.process.stdin.write(f"input tap {point[0]} {point[1]}\n")
        self.process.stdin.flush()

    def back(self) -> None:
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB shell 已意外结束")
        assert self.process.stdin is not None
        self.process.stdin.write("input keyevent 4\n")
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()


# Lightweight foreground query: parse only the mCurrentFocus / mFocusedApp
# lines instead of the full `dumpsys window windows` output that
# app_current() transfers back over adb.
FOCUS_LINE_RE = re.compile(r"mCurrentFocus=Window\{[^}]*\s+u\d+\s+([^\s/]+)/")
FOCUSED_APP_RE = re.compile(r"mFocusedApp=ActivityRecord\{[^}]*\s+u\d+\s+([^\s/]+)/")


def parse_foreground_package(line: str) -> str | None:
    """Extract the foreground package from one dumpsys window line."""
    match = FOCUS_LINE_RE.search(line)
    if match:
        return match.group(1)
    match = FOCUSED_APP_RE.search(line)
    if match:
        return match.group(1)
    return None


class PersistentQueryShell:
    """One persistent adb shell for lightweight foreground queries.

    Runs `dumpsys window | grep -E "mCurrentFocus|mFocusedApp"` on the device
    and reads back only the matched lines (terminated by a __EOT__ marker),
    avoiding the full dumpsys round trip of app_current(). Independent of the
    tap shell: taps are fire-and-forget, this shell owns its stdout stream.
    """

    def __init__(self, adb_path: Path, serial: str) -> None:
        self.process = subprocess.Popen(
            [str(adb_path), "-s", serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("无法创建持久 ADB 查询 shell")

    def query_package(self) -> str | None:
        """Return the current foreground package, or None when unparseable."""
        if self.process.poll() is not None:
            raise RuntimeError("持久 ADB 查询 shell 已意外结束")
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'; echo __EOT__\n"
        )
        self.process.stdin.flush()
        package: str | None = None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("ADB 前台查询通道已断开")
            if line.strip() == "__EOT__":
                return package
            parsed = parse_foreground_package(line)
            if parsed is not None and package is None:
                package = parsed

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()


def parse_sale_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "时间格式应为 YYYY-MM-DD HH:MM:SS"
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone Damai mode 0: repeatedly tap the shared bottom action "
            "and stop immediately when Alipay becomes foreground."
        )
    )
    parser.add_argument(
        "sale_time",
        nargs="?",
        type=parse_sale_time,
        help='Local start time, for example: "2026-08-18 13:00:00"',
    )
    parser.add_argument("--from-detail", action="store_true", help="从当前小米详情页一次核对并启动")
    parser.add_argument("--show", action="append", default=[], help="本场标题应包含的艺人或城市，可重复传入")
    parser.add_argument("--at", help="本场预期开售时间，HH:MM（今天）或 YYYY-MM-DD HH:MM")
    args = parser.parse_args()
    if args.from_detail == (args.sale_time is not None):
        parser.error("请使用 --from-detail 或提供开售日期时间，二选一")
    if (args.show or args.at) and not args.from_detail:
        parser.error("--show 和 --at 只能用于 --from-detail")
    return args


def make_schedule(
    sale_time: datetime,
    now_wall: datetime,
    now_monotonic: float,
) -> Mode0Schedule:
    seconds_to_sale = (sale_time - now_wall).total_seconds()
    sale_monotonic = now_monotonic + seconds_to_sale
    return Mode0Schedule(
        sale_monotonic=sale_monotonic,
        click_start_monotonic=sale_monotonic - PRECLICK_LEAD_S,
        first_probe_monotonic=sale_monotonic + FIRST_STATE_CHECK_AFTER_S,
        deadline_monotonic=sale_monotonic + MAX_RUNTIME_AFTER_SALE_S,
    )


def classify_fast_state(
    package: str,
    order_page_visible: bool = False,
) -> str:
    if package == ALIPAY_PACKAGE:
        return "alipay"
    if package != DAMAI_PACKAGE:
        return "other_app"
    if order_page_visible:
        return "order_page"
    return "damai_other"


def classify_hierarchy_xml(xml_text: str) -> str:
    """Classify a saved Android hierarchy without talking to a device."""
    root = ET.fromstring(xml_text)
    nodes = list(root.iter("node"))

    def has_resource(resource_id: str, *, clickable: bool | None = None) -> bool:
        for node in nodes:
            if node.attrib.get("resource-id") != resource_id:
                continue
            if clickable is None or node.attrib.get("clickable") == str(clickable).lower():
                return True
        return False

    confirm = next(
        (
            node
            for node in nodes
            if node.attrib.get("resource-id") == RETRY_RESOURCE
            and node.attrib.get("clickable") == "true"
        ),
        None,
    )
    if confirm is not None:
        tip = next(
            (
                node.attrib.get("text", "")
                for node in nodes
                if node.attrib.get("resource-id") == TIP_RESOURCE
            ),
            "",
        )
        return classify_confirm_button(confirm.attrib.get("text", ""), tip)
    seat = next(
        (
            node
            for node in nodes
            if node.attrib.get("resource-id") == SEAT_CONFIRM_RESOURCE
            and node.attrib.get("text", "").strip() in SEAT_CONFIRM_TEXTS
        ),
        None,
    )
    if seat is not None:
        return PAGE_SEAT_PAGE
    if has_resource(BACK_REDIRECT_RESOURCE, clickable=True):
        return PAGE_TERMINAL_DIALOG
    if any(
        node.attrib.get("package") == DAMAI_PACKAGE
        and node.attrib.get("text") == SUBMIT_TEXT
        and node.attrib.get("clickable") == "true"
        for node in nodes
    ):
        return PAGE_ORDER_PAGE
    if has_resource(ORDER_TITLE_RESOURCE):
        return PAGE_ORDER_LOADING
    if has_resource(RISK_RESOURCE):
        return PAGE_RISK_VERIFICATION
    return PAGE_DAMAI_OTHER


class RetryPointHolder:
    """在线程间传递新鲜点位；主按钮刚可用时，额外唤醒点击线程。"""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._lock = threading.Lock()
        self._point: tuple[int, int] | None = None
        self._at = 0.0
        self.clock = clock
        self.changed = threading.Event()

    def set(self, point: tuple[int, int], *, wake: bool = False) -> None:
        """刷新点位有效期；仅首次可用或切换主页面时请求跳过连点节拍。"""
        with self._lock:
            self._point = point
            self._at = self.clock()
            if wake:
                self.changed.set()

    def clear(self) -> None:
        """页面不可点击时，同时撤销尚未消费的唤醒信号。"""
        with self._lock:
            self._point = None
            self.changed.clear()

    def fresh(self, max_age_s: float = RETRY_POINT_MAX_AGE_S) -> tuple[int, int] | None:
        with self._lock:
            if self._point is None or self.clock() - self._at > max_age_s:
                return None
            return self._point


def classify_confirm_button(text: str, tip: str) -> str:
    """按按钮文字 / 提示文字区分重试弹窗与库存不足弹窗。"""
    text = text.strip()
    if text == RETRY_TEXT:
        return PAGE_RETRY_DIALOG
    if any(k in text for k in SOLD_OUT_BUTTON_KEYWORDS) or any(
        k in tip for k in SOLD_OUT_TIP_KEYWORDS
    ):
        return PAGE_SOLD_OUT_DIALOG
    return PAGE_UNKNOWN_DIALOG


def bounds_center(bounds: dict | None) -> tuple[int, int] | None:
    if not bounds:
        return None
    try:
        return (
            (int(bounds["left"]) + int(bounds["right"])) // 2,
            (int(bounds["top"]) + int(bounds["bottom"])) // 2,
        )
    except (KeyError, TypeError, ValueError):
        return None


class FastPageProbe:
    """Probe verified selectors only; never dump or save the UI tree."""

    def __init__(self, device: object, retry_points: RetryPointHolder | None = None) -> None:
        self.device = device
        self.retry_points = retry_points or RetryPointHolder()
        self.seat_points = RetryPointHolder()
        self.last_dialog_text = ""
        self.order_title = device(resourceId=ORDER_TITLE_RESOURCE)
        self.tip = device(resourceId=TIP_RESOURCE)
        self.retry = device(resourceId=RETRY_RESOURCE, clickable=True)
        self.back_redirect = device(
            resourceId=BACK_REDIRECT_RESOURCE,
            clickable=True,
        )
        self.submit = device(
            packageName=DAMAI_PACKAGE,
            text=SUBMIT_TEXT,
            clickable=True,
        )
        self.risk = device(resourceId=RISK_RESOURCE)
        self.seat_confirm = device(
            resourceId=SEAT_CONFIRM_RESOURCE,
            clickable=True,
            textContains="确认选座",
        )

    def read(self, phase: str) -> str:
        submit_checked = False
        if phase in ("retry_clicked", "await_submit"):
            submit_checked = True
            if self.submit.exists(timeout=0):
                self.retry_points.clear()
                self.seat_points.clear()
                return PAGE_ORDER_PAGE
        if self.retry.exists(timeout=0):
            dialog = self._classify_dialog()
            if dialog is not None:
                self.seat_points.clear()
                return dialog
        self.retry_points.clear()
        # 选座页：识别「确认选座」按钮，记录它的实测位置供点击线程使用。
        if self.seat_confirm.exists(timeout=0):
            seat = self._classify_seat()
            if seat is not None:
                return seat
        self.seat_points.clear()
        if phase in ("await_result", "risk_back_requested") and self.back_redirect.exists(
            timeout=0
        ):
            return PAGE_TERMINAL_DIALOG
        if not submit_checked and self.submit.exists(timeout=0):
            return PAGE_ORDER_PAGE
        if phase in ("await_result", "risk_back_requested") and self.risk.exists(timeout=0):
            return PAGE_RISK_VERIFICATION
        if self.order_title.exists(timeout=0):
            return PAGE_ORDER_LOADING
        return PAGE_DAMAI_OTHER

    def _classify_dialog(self) -> str | None:
        """读取 confirm_btn 的文字和位置；按钮在读取前消失时返回 None。"""
        try:
            info = self.retry.info
        except Exception:
            return None
        text = str(info.get("text") or "")
        tip = ""
        try:
            if self.tip.exists(timeout=0):
                tip = str(self.tip.info.get("text") or "")
        except Exception:
            tip = ""
        self.last_dialog_text = f"{tip} | {text}".strip(" |")
        dialog = classify_confirm_button(text, tip)
        if dialog == PAGE_RETRY_DIALOG:
            center = bounds_center(info.get("visibleBounds") or info.get("bounds"))
            if center is None:
                self.retry_points.clear()
                return PAGE_UNKNOWN_DIALOG
            self.retry_points.set(center)
        else:
            self.retry_points.clear()
        return dialog

    def _classify_seat(self) -> str | None:
        """识别选座页的「确认选座」按钮，记录实测中心点。按钮消失时返回 None。"""
        try:
            info = self.seat_confirm.info
        except Exception:
            return None
        text = str(info.get("text") or "").strip()
        if text not in SEAT_CONFIRM_TEXTS:
            self.seat_points.clear()
            return PAGE_SEAT_PAGE
        center = bounds_center(info.get("visibleBounds") or info.get("bounds"))
        if center is None:
            self.seat_points.clear()
            return PAGE_SEAT_PAGE
        self.seat_points.set(center)
        return PAGE_SEAT_PAGE


def visible_verification_prompt(device, reader) -> bool:
    """小米 WebView 可能只暴露空节点；在暂停期间只读屏幕上方的公开提示。"""
    from xiaomi_ui import has_frequency_verification

    reader.start()  # 首次遇到未知页才启动，进程随本场运行关闭。
    screen = device.screenshot()
    width, height = screen.size
    message_area = screen.crop((0, height // 6, width, height // 3))
    lines = reader.read_lines(message_area)
    if reader.error:
        raise RuntimeError(f"验证提示识别失败：{reader.error}")
    matched = has_frequency_verification("".join(lines))
    if matched:
        run_log.emit("verification_prompt_detected", source="screen_text", message="操作过于频繁，完成验证后可继续购票")
    return matched


class XiaomiPageProbe:
    """后台读取 XML 判断是否允许点固定主按钮，兼容 selector.exists 的假阴性。"""

    def __init__(self, device: object) -> None:
        self.device = device
        self.screen_size = SCREEN_SIZES[XIAOMI_15_MODEL]
        self.main_points = RetryPointHolder()
        self.submit_points = RetryPointHolder()
        self.retry_points = RetryPointHolder()
        self.seat_points = RetryPointHolder()
        self.last_dialog_text = ""
        self.last_xml = ""
        self.last_observation: tuple[object, ...] | None = None

    def read(self, phase: str) -> str:
        """缓存实测点；已知正常转场延续连点，未知页/遮挡/读取失败仍暂停。"""
        started = time.perf_counter()
        previous_xml = self.last_xml
        self.last_xml = ""
        try:
            self.last_xml = self.device.dump_hierarchy()
            # 完整 XML 已包含尺寸证据时省去两次 ADB 往返；弹窗等缺证据时仍实查。
            if not screen_size_matches_xml(self.last_xml, self.screen_size) and tuple(
                self.device.window_size()
            ) != self.screen_size:
                raise RuntimeError("手机屏幕尺寸发生变化，请恢复竖屏并重新预检")
            page, point = parse_page(self.last_xml, self.screen_size)
            if page == PAGE_PURCHASE_TRANSITION and not (
                phase == "blind" and self.last_observation is not None
                and self.last_observation[0] in (
                    PAGE_DAMAI_OTHER, PAGE_ORDER_LOADING, PAGE_ORDER_PAGE, PAGE_PURCHASE_TRANSITION
                )
            ):
                # 空树不能证明已进入购票流程，也不能解除此前的异常暂停。
                page = PAGE_UNKNOWN_DIALOG
            if page == PAGE_DAMAI_OTHER and point is None:
                page = PAGE_UNKNOWN_DIALOG
        except Exception as exc:
            self.main_points.clear()
            self.submit_points.clear()
            self.retry_points.clear()
            self.seat_points.clear()
            run_log.emit(
                "page_probe_failed", phase=phase, error_type=type(exc).__name__, error=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                xml_text=self.last_xml or None,
            )
            raise
        # 复用这一帧 XML 留证，不为日志再次读取手机；详情和票档虽同状态，也单独记录。
        stage = page
        if page == PAGE_DAMAI_OTHER:
            if 'resource-id="cn.damai:id/btn_buy_view"' in self.last_xml:
                stage = "ticket_panel"
            elif 'resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"' in self.last_xml:
                stage = "detail"
        observation = (page, stage, point)
        holder = None
        if point is not None:
            holder = self.main_points
            if page == PAGE_ORDER_PAGE:
                holder = self.submit_points
            elif page in RETRY_PAGES:
                holder = self.retry_points
            elif page == PAGE_SEAT_PAGE:
                holder = self.seat_points
        for cached in (self.main_points, self.submit_points, self.retry_points, self.seat_points):
            if cached is not holder:
                cached.clear()
        if holder is not None:
            # 同一帧只续期；新主页面立即唤醒，避免购买/确认额外等待 150 ms 节拍。
            wake = holder is self.main_points and phase == "blind" and (
                observation != self.last_observation or holder.fresh() is None
            )
            holder.set(point, wake=wake)
        run_log.emit(
            "page_probe", page=page, stage=stage, phase=phase, allowed_point=point,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            # 同属 unknown 的空窗、WebView 和验证页也要保留内容变化，不额外读取手机。
            xml_text=self.last_xml if observation != self.last_observation or self.last_xml != previous_xml else None,
        )
        self.last_observation = observation
        return page


def summarize_bottom_controls(
    xml_text: str,
    screen_height: int = EXPECTED_SCREEN_SIZE[1],
    limit: int = 12,
) -> list[dict[str, object]]:
    """列出屏幕底部区域带文字的控件（文字、resource-id、clickable、enabled、bounds）。"""
    root = ET.fromstring(xml_text)
    top_limit = screen_height * BOTTOM_AREA_TOP_RATIO
    found: list[dict[str, object]] = []
    for node in root.iter("node"):
        text = (node.attrib.get("text") or node.attrib.get("content-desc") or "").strip()
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
        if not text or not match or int(match.group(2)) < top_limit:
            continue
        found.append(
            {
                "text": text[:40],
                "id": node.attrib.get("resource-id", ""),
                "clickable": node.attrib.get("clickable") == "true",
                "enabled": node.attrib.get("enabled") == "true",
                "bounds": node.attrib.get("bounds"),
            }
        )
        if len(found) >= limit:
            break
    return found


class DiagnosticProbe:
    """包装页面探测，增加 heartbeat 日志和弹窗卡住时的现场快照。

    不改变探测结果，也不发出任何点击；只读 + 写 artifacts\\ 文件。
    快照在后台线程里做，不阻塞探测/重试闭环。
    """

    def __init__(self, inner: FastPageProbe, device: object, sale_monotonic: float) -> None:
        self.inner = inner
        self.device = device
        self.sale_monotonic = sale_monotonic
        self.last_page: str | None = None
        self.page_since = 0.0
        self.last_heartbeat = 0.0
        self.snapshots: list[str] = []
        self.stuck_page_snapshots = 0
        self.dialog_snapshots = 0
        self._snapshot_thread: threading.Thread | None = None
        self._snapshot_due_at: float | None = None
        self._last_unknown_snapshot_at: float | None = None
        self._last_snapshot_skip: tuple[str, str] | None = None
        self._lock = threading.Lock()

    def read(self, phase: str) -> str:
        page = self.inner.read(phase)
        now = time.perf_counter()
        if page != self.last_page:
            self.last_page = page
            self.page_since = now
            self.last_heartbeat = now
            if page in (PAGE_SOLD_OUT_DIALOG, PAGE_UNKNOWN_DIALOG):
                self._emit(
                    {
                        "event": "dialog_text",
                        "page": page,
                        "text": getattr(self.inner, "last_dialog_text", ""),
                    }
                )
                self._start_snapshot(page, 0.0)  # 新类型弹窗立即留证
                # 小米 WebView 验证往往一直归为 unknown，不能等分类变化才补图。
                self._snapshot_due_at = now + 5.0 if isinstance(self.inner, XiaomiPageProbe) and page == PAGE_UNKNOWN_DIALOG else None
            elif page in RETRY_PAGES:
                self._snapshot_due_at = now + RETRY_REPEAT_AFTER_S
            elif page in STUCK_SNAPSHOT_PAGES and now >= self.sale_monotonic:
                self._snapshot_due_at = now + STUCK_PAGE_SNAPSHOT_AFTER_S
            else:
                self._snapshot_due_at = None
            return page

        stuck_for = now - self.page_since
        if now - self.last_heartbeat >= HEARTBEAT_INTERVAL_S:
            self.last_heartbeat = now
            self._emit(
                {
                    "event": "heartbeat",
                    "page": page,
                    "phase": phase,
                    "same_page_seconds": round(stuck_for, 3),
                }
            )
        # 开售前就停在详情页属于正常等待，开售后才开始计时。
        if (
            self._snapshot_due_at is None
            and page in STUCK_SNAPSHOT_PAGES
            and now >= self.sale_monotonic + STUCK_PAGE_SNAPSHOT_AFTER_S
            and now - max(self.page_since, self.sale_monotonic) >= STUCK_PAGE_SNAPSHOT_AFTER_S
            and self.stuck_page_snapshots < STUCK_PAGE_SNAPSHOT_LIMIT
        ):
            self._snapshot_due_at = now
        if self._snapshot_due_at is not None and now >= self._snapshot_due_at:
            # 同一页面持续卡住时，每 2 秒（弹窗）/ 5 秒（非弹窗）最多再存一份
            self._snapshot_due_at = now + (2.0 if page in RETRY_PAGES else 5.0)
            self._start_snapshot(page, stuck_for)
        return page

    def _emit(self, record: dict[str, object]) -> None:
        run_log.emit(str(record["event"]), **{key: value for key, value in record.items() if key != "event"})
        record["wall_time"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        record["seconds_after_sale"] = round(time.perf_counter() - self.sale_monotonic, 3)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def _start_snapshot(self, page: str, stuck_for: float) -> None:
        """异步截图保持单任务；小米持续未知页每五秒可补图，避免三张额度提前耗尽。"""
        with self._lock:
            now = time.perf_counter()
            repeat_unknown = isinstance(self.inner, XiaomiPageProbe) and page == PAGE_UNKNOWN_DIALOG
            reason = None
            # 弹窗现场与非弹窗卡页现场分别计数，互不挤占名额。
            is_stuck_page = page in STUCK_SNAPSHOT_PAGES
            if is_stuck_page and self.stuck_page_snapshots >= STUCK_PAGE_SNAPSHOT_LIMIT:
                reason = "stuck_page_limit"
            elif not is_stuck_page and not repeat_unknown and self.dialog_snapshots >= DIALOG_SNAPSHOT_LIMIT:
                reason = "dialog_limit"
            elif repeat_unknown and self._last_unknown_snapshot_at is not None and now - self._last_unknown_snapshot_at < 5.0:
                reason = "capture_interval"
            elif self._snapshot_thread is not None and self._snapshot_thread.is_alive():
                reason = "capture_busy"
            if reason is not None:
                # 高频页面跳变只记录一次相同跳过原因，不能让诊断日志占满点击热路径。
                if self._last_snapshot_skip != (page, reason):
                    run_log.emit("snapshot_skipped", page=page, reason=reason)
                    self._last_snapshot_skip = (page, reason)
                return
            self._last_snapshot_skip = None
            if repeat_unknown:
                self._last_unknown_snapshot_at = now
            if is_stuck_page:
                self.stuck_page_snapshots += 1
            else:
                self.dialog_snapshots += 1
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            base = ARTIFACTS_DIR / f"stuck_{page}_{stamp}"
            self.snapshots.append(str(base))
            run_log.emit("snapshot_started", page=page, base=str(base), requested_monotonic=now,
                         xml_reused=bool(getattr(self.inner, "last_xml", None)))
            self._snapshot_thread = threading.Thread(
                target=self._snapshot,
                args=(base, page, stuck_for, getattr(self.inner, "last_xml", None)),
                name="damai-k80-snapshot",
                daemon=True,
            )
            self._snapshot_thread.start()

    def _snapshot(self, base: Path, page: str, stuck_for: float, xml_text: str | None = None) -> None:
        """小米复用触发快照时的 XML；截图稍后采集，时间可能与 XML 有差异。"""
        started = time.perf_counter()
        screenshot_started = None
        saved: list[str] = []
        error = None
        bottom: list[dict[str, object]] = []
        try:
            ARTIFACTS_DIR.mkdir(exist_ok=True)
            xml_text = xml_text or self.device.dump_hierarchy()
            xml_path = base.with_suffix(".xml")
            xml_path.write_text(xml_text, encoding="utf-8")
            saved.append(xml_path.name)
            screen_size = getattr(self.inner, "screen_size", EXPECTED_SCREEN_SIZE)
            bottom = summarize_bottom_controls(xml_text, screen_height=screen_size[1])
            png_path = base.with_suffix(".png")
            screenshot_started = time.perf_counter()
            self.device.screenshot(str(png_path))
            saved.append(png_path.name)
        except Exception as exc:  # 快照失败不影响抢票
            error = f"{type(exc).__name__}: {exc}"
        self._emit(
            {
                "event": "dialog_snapshot" if page not in STUCK_SNAPSHOT_PAGES else "stuck_page_snapshot",
                "page": page,
                "same_page_seconds": round(stuck_for, 3),
                "files": saved,
                "bottom_controls": bottom,
                "capture_started_monotonic": screenshot_started,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": error,
            }
        )

    def join(self, timeout: float) -> None:
        thread = self._snapshot_thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                run_log.emit("snapshot_incomplete", reason="capture_still_running_at_shutdown")


def wait_until(target: float, stop_event: threading.Event) -> bool:
    while not stop_event.is_set():
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return True
        stop_event.wait(min(remaining, 0.1))
    return False


def tap_loop(
    schedule: Mode0Schedule,
    state: RuntimeState,
    stop_event: threading.Event,
    controlled_event: threading.Event,
    requests: TapRequestQueue,
    tap_shell: PersistentTapShell | ScopedInput,
    query_shell: PersistentQueryShell,
    retry_points: RetryPointHolder | None = None,
    seat_points: RetryPointHolder | None = None,
    main_points: RetryPointHolder | None = None,
    submit_points: RetryPointHolder | None = None,
    continuous_main_point: tuple[int, int] | None = None,
    purchase_ready: threading.Event | None = None,
) -> None:
    """等待首击信号后发送输入；小米主流程不等点位续期，异常由后台接管。"""
    last_package = None
    wait_reason = None
    wait_started = 0.0

    def record_wait(reason: str | None) -> None:
        """只记录等待原因的变化，避免每轮空等都产生重复日志。"""
        nonlocal wait_reason, wait_started
        if reason == wait_reason:
            return
        now = time.perf_counter()
        if wait_reason is not None:
            run_log.emit("click_wait_end", reason=wait_reason, duration_ms=round((now - wait_started) * 1000, 3))
        wait_reason, wait_started = reason, now
        if reason is not None:
            run_log.emit("click_wait_start", reason=reason)

    def may_dispatch(action: str) -> bool:
        """查询和选动作期间可能收到停止信号；紧邻输入发送复查，不增加 ADB 往返。"""
        if stop_event.is_set():
            return False
        if time.perf_counter() >= schedule.deadline_monotonic:
            state.finish("timeout")
            run_log.emit("stop_requested", reason="timeout")
            stop_event.set()
            return False
        if action == "main" and controlled_event.is_set():
            record_wait("main_paused")
            return False
        return True

    try:
        if purchase_ready is None:
            if not wait_until(schedule.click_start_monotonic, stop_event):
                return
        else:
            # 图像线程只负责首击放行；文字提前变更也立即响应，不再等 Mac 到点。
            while not purchase_ready.wait(.01):
                if stop_event.is_set():
                    return
                if time.perf_counter() >= schedule.deadline_monotonic:
                    state.finish("timeout")
                    stop_event.set()
                    return
            if stop_event.is_set():
                return

        next_blind_tap = time.perf_counter() if purchase_ready is not None else max(
            schedule.click_start_monotonic, time.perf_counter()
        )
        state.finish("clicking")
        scoped = isinstance(tap_shell, ScopedInput)
        first_ready = purchase_ready is not None and continuous_main_point is not None and scoped
        while not stop_event.is_set():
            now = time.perf_counter()
            if now >= schedule.deadline_monotonic:
                state.finish("timeout")
                run_log.emit("stop_requested", reason="timeout")
                stop_event.set()
                return

            # 首次看到可购文案时，Android 已在投递端绑定大麦 UID；暂停和排队动作仍走常规检查。
            direct_main = first_ready and not controlled_event.is_set() and not requests.changed.is_set()
            first_ready = False
            query_started = query_finished = query_ms = None
            if not direct_main:
                try:
                    query_started = time.perf_counter()
                    package = query_shell.query_package()
                    query_finished = time.perf_counter()
                    query_ms = round((query_finished - query_started) * 1000, 3)
                except Exception as exc:
                    # A failed foreground check means no coordinate tap is safe.
                    state.record_foreground_error()
                    record_wait(f"foreground_query_error:{type(exc).__name__}:{exc}")
                    stop_event.wait(0.01)
                    continue
                if package is None:
                    # Unparseable foreground means unknown: still no tap.
                    state.record_foreground_error()
                    record_wait("foreground_unknown")
                    stop_event.wait(0.01)
                    continue

                state.update_package(package)
                if package != last_package:
                    run_log.emit("foreground_changed", previous=last_package, package=package, query_ms=query_ms)
                    last_package = package
                if package == ALIPAY_PACKAGE:
                    requests.clear()
                    state.finish("alipay_reached")
                    run_log.emit("stop_requested", reason="alipay_reached")
                    stop_event.set()
                    return
                if package != DAMAI_PACKAGE:
                    record_wait(f"foreground_other:{package}")
                    stop_event.wait(0.01)
                    continue

            action = None if direct_main else requests.pop()
            now = time.perf_counter()
            if action is None and not controlled_event.is_set():
                immediate = continuous_main_point is None and main_points is not None and main_points.changed.is_set()
                if immediate or now >= next_blind_tap:
                    action = "main"
                    if main_points is not None:
                        main_points.changed.clear()
                    # 新页面首击立即执行，后续连点从本次首击重新计时，避免紧接着补一击。
                    next_blind_tap = now + CLICK_INTERVAL_S if immediate else next_blind_tap + CLICK_INTERVAL_S
                    if next_blind_tap <= now:
                        next_blind_tap = now + CLICK_INTERVAL_S

            if action is not None:
                if action == "back":
                    record_wait(None)
                    if not may_dispatch(action):
                        continue
                    tap_shell.back()
                    run_log.emit("input_dispatched", action=action, query_ms=query_ms, page=state.last_page)
                    state.record_risk_back()
                    continue
                retry = action == "retry"
                if action == "seat":
                    # 只点探测线程刚实测到的「确认选座」按钮中心；拿不到就不点。
                    point = seat_points.fresh() if seat_points is not None else None
                    if point is None:
                        state.record_skipped_seat()
                        record_wait("seat_point_unavailable")
                        continue
                    record_wait(None)
                    if not may_dispatch(action):
                        continue
                    tap_shell.tap(point)
                    run_log.emit("input_dispatched", action=action, point=point, query_ms=query_ms, page=state.last_page)
                    state.record_seat_tap()
                    continue
                if retry:
                    # 只点探测线程刚实测到的「继续尝试」按钮中心；拿不到就不点。
                    point = retry_points.fresh() if retry_points is not None else None
                    if point is None:
                        state.record_skipped_retry()
                        record_wait("retry_point_unavailable")
                        continue
                else:
                    # 小米主流程直用固定坐标；异常暂停由 controlled_event 独立控制。
                    holder = submit_points if action == "submit" else main_points
                    point = continuous_main_point if continuous_main_point is not None else (
                        holder.fresh() if holder is not None else MAIN_ACTION_POINT
                    )
                    if point is None:
                        record_wait(f"{action}_point_unavailable_or_stale")
                        if action == "submit":
                            requests.restore(action)
                        stop_event.wait(0.01)
                        continue
                record_wait(None)
                if not may_dispatch(action):
                    continue
                dispatch_started = time.perf_counter()
                tap_shell.tap(point)
                dispatch_finished = time.perf_counter()
                # 小米的结束时间包含 Android 分发回执；业务页面完成响应要看后续记录。
                run_log.emit("input_dispatched", action=action, point=point, query_ms=query_ms, page=state.last_page,
                             input_guard="android_target_uid" if scoped else "foreground_query",
                             query_started_monotonic=query_started, query_finished_monotonic=query_finished,
                             dispatch_started_monotonic=dispatch_started,
                             dispatch_finished_monotonic=dispatch_finished)
                state.record_tap(retry=retry)
                continue

            if controlled_event.is_set():
                requests.changed.wait(0.01)
            else:
                wake_event = main_points.changed if main_points is not None else stop_event
                wake_event.wait(min(max(next_blind_tap - now, 0.0), 0.01))
    except TargetMismatch:
        # 查前台只用来说明停机原因。先停输入，确保查询慢或失败也不会重试。
        state.finish("input_target_mismatch")
        stop_event.set()
        requests.clear()
        package = None
        try:
            package = query_shell.query_package()
        except Exception:
            state.record_foreground_error()
        if package:
            state.update_package(package)
        reason = "alipay_reached" if package == ALIPAY_PACKAGE else "input_target_mismatch"
        state.finish(reason)
        run_log.emit("stop_requested", reason=reason, input_guard="android_target_uid", package=package)
    except BaseException as exc:
        state.finish("error", f"{type(exc).__name__}: {exc}")
        run_log.emit("worker_error", error_type=type(exc).__name__, error=str(exc))
        stop_event.set()
    finally:
        record_wait(None)


def query_foreground_once(adb_path: Path) -> str | None:
    """One-shot foreground query using the same command as PersistentQueryShell.

    K80 (HyperOS) does not print mCurrentFocus in `dumpsys window windows`, so
    uiautomator2's app_current() falls back to `dumpsys activity top` and
    reports the *last* task (e.g. QQ Mail) instead of the real foreground app.
    `dumpsys window` does contain mCurrentFocus, and it is exactly what the
    runtime tap gate uses, so the preflight now checks the same source.
    """
    proc = subprocess.run(
        [
            str(adb_path),
            "-s",
            DEVICE_SERIAL,
            "shell",
            "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    for line in proc.stdout.splitlines():
        package = parse_foreground_package(line)
        if package is not None:
            return package
    return None


def validate_preflight(device: object, adb_path: Path) -> str:
    """按实际机型校验屏幕与前台，返回后才能创建点击线程。"""
    model = device.shell("getprop ro.product.model").output.strip()
    expected = SCREEN_SIZES.get(model, EXPECTED_SCREEN_SIZE)
    actual_size = tuple(device.window_size())
    if actual_size != expected:
        raise RuntimeError(
            f"手机分辨率为 {actual_size[0]}×{actual_size[1]}，"
            f"但机型 {model} 需要 {expected[0]}×{expected[1]}，请检查分辨率与竖屏方向。"
        )
    current_package = query_foreground_once(adb_path)
    if current_package != DAMAI_PACKAGE:
        raise RuntimeError(
            f"当前前台为 {current_package or '未知'}，请先将大麦置于前台并停在目标演唱会详情页"
        )
    return model


def resolve_adb() -> Path:
    """使用显式路径或当前系统的 adb，保持 Mac 与预检工具的解析一致。"""
    path = ADB_PATH if ADB_PATH is not None else default_adb_path()
    if not path.is_file():
        raise FileNotFoundError(f"ADB 不存在：{path}，请安装 Android Platform-Tools")
    return path


def resolve_serial(adb_path: Path) -> str:
    """DEVICE_SERIAL 常量 > 环境变量 DAMAI_DEVICE_SERIAL > 唯一一台已授权设备。"""
    import os

    explicit = DEVICE_SERIAL or os.environ.get("DAMAI_DEVICE_SERIAL", "").strip()
    if explicit:
        return explicit
    proc = subprocess.run(
        [str(adb_path), "devices"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    ready = [
        parts[0]
        for parts in (line.split() for line in proc.stdout.splitlines()[1:])
        if len(parts) >= 2 and parts[1] == "device"
    ]
    if len(ready) != 1:
        raise RuntimeError(
            f"检测到 {len(ready)} 台已授权的 adb 设备 {ready}；"
            "请只连接一台，或设置环境变量 DAMAI_DEVICE_SERIAL"
        )
    return ready[0]


def capture_boundary(device: object, label: str, xml_text: str | None = None) -> None:
    """仅在点击开始前或停止后采集截图和 XML，采集失败不会改变购票结果。"""
    recorder = run_log.active
    if recorder is None:
        return
    started = time.perf_counter()
    errors = []
    png_path = recorder.directory / f"{label}.png"
    if xml_text is None:
        try:
            xml_text = device.dump_hierarchy()
        except Exception as exc:
            errors.append(f"XML: {type(exc).__name__}: {exc}")
    try:
        device.screenshot(str(png_path))
    except Exception as exc:
        errors.append(f"PNG: {type(exc).__name__}: {exc}")
    run_log.emit(
        "boundary_capture", label=label, xml_text=xml_text,
        png_path=png_path.name if png_path.is_file() else None,
        errors=errors, duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )


@dataclass(frozen=True)
class CheckedDetail:
    """把核对过的设备和页面交给正式流程，避免重新选设备或再抓一次详情页。"""
    serial: str
    device: object
    xml: str
    title: str


def run(sale_time: datetime, detail: CheckedDetail | None = None) -> dict[str, object]:
    """小米以购买文案放行首击，之后固定点连续点击；支付宝出现即停。"""
    global DEVICE_SERIAL
    adb_path = resolve_adb()
    DEVICE_SERIAL = detail.serial if detail else resolve_serial(adb_path)
    probe_device = detail.device if detail else u2.connect(DEVICE_SERIAL)
    model = validate_preflight(probe_device, adb_path)
    if detail and model != XIAOMI_15_MODEL:
        raise RuntimeError("详情页启动入口仅适配小米 15")
    schedule = make_schedule(sale_time, datetime.now(), time.perf_counter())
    if model == XIAOMI_15_MODEL:
        # Mac 时间只保留作日志和截止时间；首击等待按钮真正出现可购买文案。
        schedule = replace(
            schedule, click_start_monotonic=schedule.sale_monotonic,
            first_probe_monotonic=time.perf_counter(),
        )
    retry_points = RetryPointHolder()
    inner_probe = (
        XiaomiPageProbe(probe_device) if model == XIAOMI_15_MODEL
        else FastPageProbe(probe_device, retry_points)
    )
    retry_points = inner_probe.retry_points
    page_probe = DiagnosticProbe(
        inner_probe,
        probe_device,
        schedule.sale_monotonic,
    )
    if time.perf_counter() >= schedule.deadline_monotonic:
        return {
            "status": "timeout_before_start",
            "sale_time": sale_time.isoformat(sep=" "),
            "safety": "no_tap_dispatched",
        }

    run_log.emit(
        "device_ready", serial=DEVICE_SERIAL, model=model,
        screen_size=SCREEN_SIZES.get(model, EXPECTED_SCREEN_SIZE),
        main_action_point=XIAOMI_15_MAIN_ACTION_POINT if model == XIAOMI_15_MODEL else MAIN_ACTION_POINT,
        click_start_monotonic=schedule.click_start_monotonic,
        first_probe_monotonic=schedule.first_probe_monotonic,
        start_trigger="purchase_button_text" if model == XIAOMI_15_MODEL else "preclick",
        input_backend="android_target_uid" if model == XIAOMI_15_MODEL else "adb_shell",
        continuous_main=model == XIAOMI_15_MODEL,
    )
    # 临近开抢直接跳过开场截图，避免采集挤占首击准备时间。
    if schedule.click_start_monotonic - time.perf_counter() >= 10:
        capture_boundary(probe_device, "initial", detail.xml if detail else None)
    else:
        run_log.emit("boundary_capture_skipped", label="initial", reason="less_than_10_seconds_before_clicking")

    state = RuntimeState()
    stop_event = threading.Event()
    controlled_event = threading.Event()
    requests = TapRequestQueue()
    controller = Mode0StateController(continuous_main=model == XIAOMI_15_MODEL, stop_on_verification=model == XIAOMI_15_MODEL)
    purchase_ready = threading.Event() if model == XIAOMI_15_MODEL else None
    button_worker = None
    verification_reader = None
    if model == XIAOMI_15_MODEL:
        from button_text import ButtonTextReader
        verification_reader = ButtonTextReader(stop_event)
        # 首次后台读取完成前不盲点未知页面；后续正常切页不再等待逐页许可。
        controlled_event.set()
    query_shell = PersistentQueryShell(adb_path, DEVICE_SERIAL)
    try:
        if model == XIAOMI_15_MODEL:
            # 预检已确认大麦在前台，开售前将触摸通道绑定到它独占的 Android UID。
            tap_shell = ScopedInput(adb_path, DEVICE_SERIAL)
            run_log.emit("input_target_ready", package=DAMAI_PACKAGE, uid=tap_shell.uid)
        else:
            tap_shell = PersistentTapShell(adb_path, DEVICE_SERIAL)
    except BaseException:
        query_shell.close()
        raise
    worker = threading.Thread(
        target=tap_loop,
        args=(
            schedule,
            state,
            stop_event,
            controlled_event,
            requests,
            tap_shell,
            query_shell,
            retry_points,
            page_probe.inner.seat_points,
            getattr(inner_probe, "main_points", None),
            getattr(inner_probe, "submit_points", None),
            XIAOMI_15_MAIN_ACTION_POINT if model == XIAOMI_15_MODEL else None,
            purchase_ready,
        ),
        name="damai-mode0-k80-tap-loop",
        daemon=True,
    )
    try:
        worker.start()
        if purchase_ready is not None:
            from button_ready import watch_purchase_button
            button_worker = threading.Thread(
                target=watch_purchase_button,
                args=(u2.connect(DEVICE_SERIAL), adb_path, DEVICE_SERIAL, purchase_ready,
                      stop_event, state, schedule.deadline_monotonic),
                kwargs={"initial_xml": detail.xml} if detail else {},
                name="damai-purchase-button", daemon=False,
            )
            button_worker.start()
        run_tail_probe_loop(
            schedule=schedule,
            state=state,
            stop_event=stop_event,
            controlled_event=controlled_event,
            requests=requests,
            controller=controller,
            page_probe=page_probe,
            wait_until=wait_until,
            state_check_interval_s=STATE_CHECK_INTERVAL_S,
            risk_state_check_interval_s=RISK_STATE_CHECK_INTERVAL_S,
            purchase_ready=purchase_ready,
            check_unknown_verification=(lambda: visible_verification_prompt(probe_device, verification_reader))
            if verification_reader is not None else None,
        )
    except BaseException as exc:
        run_log.emit("stop_requested", reason="interrupted" if isinstance(exc, KeyboardInterrupt) else "probe_loop_error")
        raise
    finally:
        requests.clear()
        state.leave_risk_wait(time.perf_counter())
        stop_event.set()
        try:
            if verification_reader is not None:
                verification_reader.close()
            if worker.ident is not None:
                worker.join(timeout=THREAD_JOIN_TIMEOUT_S)
            if button_worker is not None and button_worker.ident is not None:
                # 采集持有手机 server/forward；必须等有超时的清理完成再退出。
                button_worker.join()
            page_probe.join(timeout=THREAD_JOIN_TIMEOUT_S)
        finally:
            try:
                tap_shell.close()
            finally:
                query_shell.close()
        # 收尾截图的耗时不能计入抢票结束时间，否则会误判流程速度。
        finished_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        finished_monotonic = time.perf_counter()
        capture_boundary(probe_device, "final")

    result = state.snapshot()
    result.update(
        {
            "retry_repeat_count": controller.retry_repeat_count,
            "seat_click_count": controller.seat_click_count,
            "seat_tap_count": state.seat_tap_count,
            "skipped_seat_count": state.skipped_seat_count,
            "skipped_retry_count": state.skipped_retry_count,
            "sold_out_dialog_seen": controller.sold_out_streak > 0
            or result.get("last_page") == PAGE_SOLD_OUT_DIALOG,
            "unknown_dialog_hits": controller.unknown_dialog_hits,
            "dialog_snapshots": page_probe.snapshots,
            "sale_time": sale_time.isoformat(sep=" "),
            "finished_at": finished_at,
            "seconds_after_sale": round(
                finished_monotonic - schedule.sale_monotonic,
                3,
            ),
            "main_action_point": list(XIAOMI_15_MAIN_ACTION_POINT if model == XIAOMI_15_MODEL else MAIN_ACTION_POINT),
            "retry_dialog_point": "measured_from_retry_button_bounds",
            "safety": (
                "stops_on_alipay_foreground_or_terminal_or_sold_out_dialog; "
                "clicks_seat_confirm_when_on_seat_page; "
                "clicks_retry_only_when_button_text_is_继续尝试_or_努力刷新; "
                "never_clicks_unknown_dialog; "
                + ("stops_on_risk_verification; " if controller.stop_on_verification else "backs_once_from_risk_verification; ")
                + "never_clicks_alipay_pay; never_clicks_back_redirect"
            ),
        }
    )
    return result


def main() -> None:
    """每次手动启动都自动归档记录；成功、异常和 Ctrl-C 均尽量留下最终结果。"""
    global ARTIFACTS_DIR
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args()
    detail = None
    sale_time = args.sale_time
    if sale_time is None:
        from sale_detail import check_sale_detail
        serial = resolve_serial(resolve_adb())
        device = u2.connect(serial)
        xml = device.dump_hierarchy()
        try:
            title, sale_time = check_sale_detail(xml, args.show, args.at, datetime.now())
        except ValueError as exc:
            raise SystemExit(f"未启动：{exc}") from None
        detail = CheckedDetail(serial, device, xml, title)
    original_dir = ARTIFACTS_DIR
    directory = original_dir / f"run_{datetime.now():%Y%m%d_%H%M%S_%f}"
    recorder = run_log.RunLog(directory, sale_time)
    run_log.active = recorder
    ARTIFACTS_DIR = directory
    result = {"status": "initialization_error"}
    try:
        with recorder.capture_console():
            print(f"实战记录目录：{directory}", flush=True)
            recorder.emit(
                "run_started", sale_time=sale_time.isoformat(), python=sys.version,
                click_interval_s=CLICK_INTERVAL_S,
                max_runtime_after_sale_s=MAX_RUNTIME_AFTER_SALE_S,
            )
            try:
                if detail:
                    print(f"核对通过：{detail.title}，{sale_time:%Y-%m-%d %H:%M} 开抢，预填已完成。正在启动。", flush=True)
                    recorder.emit("sale_detail_confirmed", title=detail.title, sale_time=sale_time.isoformat(),
                                  prefill="已完成", serial=detail.serial)
                    result = run(sale_time, detail)
                else:
                    result = run(sale_time)
            except BaseException as exc:
                result = {
                    "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                    "error_type": type(exc).__name__, "error": str(exc),
                }
                recorder.emit("run_error", **result)
                traceback.print_exc()
                raise
            finally:
                recorder.emit("run_finished", status=result["status"])
                recorder.close()
                result["recording_directory"] = str(directory)
                result["recording_errors"] = list(recorder.errors)
                recorder.write_result(result)
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        run_log.active = None
        ARTIFACTS_DIR = original_dir
        recorder.close()


if __name__ == "__main__":
    main()
