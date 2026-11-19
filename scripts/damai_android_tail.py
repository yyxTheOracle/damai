from __future__ import annotations

from datetime import datetime
import json
import time
from typing import Callable

import run_log


def run_tail_probe_loop(
    *,
    schedule: object,
    state: object,
    stop_event: object,
    controlled_event: object,
    requests: object,
    controller: object,
    page_probe: object,
    wait_until: Callable[[float, object], bool],
    state_check_interval_s: float,
    risk_state_check_interval_s: float,
    purchase_ready: object | None = None,
    check_unknown_verification: Callable[[], bool] | None = None,
) -> None:
    """Run the shared Android order-page/retry state machine.

    The collaborators are deliberately passed in so mode 0 keeps its existing
    low-level worker, timings, result fields, and tests. Mode 10 enters with its
    blind-tap worker disabled and uses this exact same tail from the order page.
    """
    next_probe = max(schedule.first_probe_monotonic, time.perf_counter())
    while not stop_event.is_set():
        now = time.perf_counter()
        if now >= schedule.deadline_monotonic:
            requests.clear()
            state.finish("timeout")
            run_log.emit("stop_requested", reason="timeout")
            stop_event.set()
            break
        if not wait_until(next_probe, stop_event):
            break

        try:
            page = page_probe.read(controller.phase)
            if page == "unknown_dialog" and check_unknown_verification is not None:
                # 先暂停输入并清空旧动作，再读可见提示；检查返回前不能因页面跳转恢复。
                controlled_event.set()
                requests.clear()
                if check_unknown_verification():
                    page = "risk_verification"
        except Exception as exc:
            page = f"probe_error:{type(exc).__name__}"
            run_log.emit("probe_error", error=f"{type(exc).__name__}: {exc}", phase=controller.phase)
        state.record_probe(page)
        decision = controller.observe(page)
        if decision.status is not None:
            # 终态先撤销输入，后面的日志和终端输出可能比一次点击还慢。
            controlled_event.set()
            requests.clear()
            state.finish(decision.status)
            stop_event.set()
        if (
            decision.action is not None
            or decision.clear_pending
            or decision.status is not None
            or decision.previous_phase != decision.phase
        ):
            # 这里只把决策交给后台记录，不增加页面读取或同步写盘。
            run_log.emit(
                "state_decision",
                page=page,
                previous_phase=decision.previous_phase,
                phase=decision.phase,
                action=decision.action,
                clear_pending=decision.clear_pending,
                status=decision.status,
            )

        if controller.controlled:
            controlled_event.set()
        state.transition(decision.previous_phase, decision.phase, page)
        if decision.dialog_hit is not None:
            state.record_dialog(decision.dialog_hit)
        if decision.entered_risk:
            state.enter_risk_wait(time.perf_counter())
        if decision.left_risk:
            state.leave_risk_wait(time.perf_counter())
        if decision.clear_pending:
            requests.clear()
        if decision.action is not None:
            requests.request(decision.action)
        if getattr(controller, "continuous_main", False) and not controller.controlled:
            # 先清掉旧异常请求再恢复主连点，避免恢复后误点旧弹窗位置。
            controlled_event.clear()

        if (
            decision.previous_phase != decision.phase
            or decision.action is not None
            or decision.status is not None
            or decision.dialog_hit is not None
            or page.startswith("probe_error")
        ):
            probe_record = {
                "event": "state_probe",
                "wall_time": datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                ),
                "seconds_after_sale": round(
                    time.perf_counter() - schedule.sale_monotonic,
                    3,
                ),
                "package": state.snapshot()["last_package"],
                "page": page,
                "phase": decision.phase,
                "action": decision.action,
            }
            print(json.dumps(probe_record, ensure_ascii=False), flush=True)

        if decision.status is not None:
            run_log.emit("stop_requested", reason=decision.status)
            if decision.status == "verification_required":
                print("已停止自动输入，请在手机上手动处理安全验证。", flush=True)
            break

        completed_at = time.perf_counter()
        interval = (
            risk_state_check_interval_s
            if controller.phase == "risk_back_requested"
            else state_check_interval_s
        )
        if purchase_ready is not None and not purchase_ready.is_set():
            interval = max(interval, .25)
        next_probe = completed_at + interval
