"""首击前观察购买文案；确认后只发出一次唤醒信号，不执行任何手机输入。"""
from __future__ import annotations

from pathlib import Path
import re
import time
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops, ImageStat

import run_log
from button_stream import ButtonStream
from button_text import ButtonTextReader
from device_support import XIAOMI_15_MAIN_ACTION_POINT
from xiaomi_ui import DETAIL_RESOURCE, parse_page


def label_mask(image: Image.Image) -> Image.Image | None:
    """提取按钮最大字号的白色主行，排除橙色背景和较小的预约提示文字。"""
    red, green, blue = image.convert("RGB").split()
    mask = ImageChops.darker(ImageChops.darker(red, green), blue).point(lambda v: 255 if v >= 220 else 0)
    spans, start = [], None
    for y in range(mask.height + 1):
        occupied = y < mask.height and mask.crop((0, y, mask.width, y + 1)).histogram()[255] >= 3
        if occupied and start is None:
            start = y
        elif not occupied and start is not None:
            spans.append((start, y))
            start = None
    if not spans:
        return None
    top, bottom = max(spans, key=lambda span: span[1] - span[0])
    if bottom - top < 12:
        return None
    row = mask.crop((0, top, mask.width, bottom))
    return row.crop(row.getbbox()).resize((160, 40), Image.Resampling.BILINEAR)


class ButtonMatcher:
    """已校准文案用图片快匹配，新文案才交给预热后的 OCR 精确确认。"""

    def __init__(self, reader: ButtonTextReader) -> None:
        """加载实机标定字形；预订初始帧和稳定帧分开匹配，保留原阈值。"""
        self.reader = reader
        assets = Path(__file__).parent / "assets" / "button_text"
        calibrated = (("buy_now.png", "立即购票"),
                      ("book_now_first.png", "立即预订"),
                      ("book_now_stable.png", "立即预订"),
                      ("buy_now_xuezhiqian.png", "立即购票"))
        self.templates = [
            (Image.open(assets / name).convert("L"), label)
            for name, label in calibrated
        ]
        self._calibrated_names = [name for name, _ in calibrated]
        self._calibrated_count = len(self.templates)
        self.method = "none"
        self.last_match: dict = {}

    def match(self, image: Image.Image, *, frame_sequence: int | None = None) -> str | None:
        """识别一帧并保留该次诊断；OCR 起止单独留证，避免被后续快匹配掩盖。"""
        self.last_match = {"template_threshold": .04, "template_count": len(self.templates),
                           "best_template_distance": None}
        mask = label_mask(image)
        self.method = "none"
        if mask is None:
            return None
        for index, (template, label) in enumerate(self.templates):
            difference = ImageStat.Stat(ImageChops.difference(mask, template)).mean[0] / 255
            name = self._calibrated_names[index] if index < self._calibrated_count else f"learned:{index}"
            best = self.last_match["best_template_distance"]
            if best is None or difference < best:
                self.last_match.update(best_template_distance=difference, best_template=name)
            if difference <= .04:
                self.method = "template"
                self.last_match.update(matched_template=name,
                                       template_source="calibrated" if index < self._calibrated_count else "ocr_learned")
                return label
        self.method = "ocr"
        started = time.perf_counter()
        run_log.emit("button_ocr_started", frame_sequence=frame_sequence,
                     ocr_started_monotonic=started, **self.last_match)
        error = None
        try:
            label = self.reader.read(image)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            finished = time.perf_counter()
            self.last_match.update(ocr_started_monotonic=started, ocr_finished_monotonic=finished,
                                   ocr_ms=round((finished - started) * 1000, 3),
                                   ocr_text=self.reader.last_text, ocr_error=error or self.reader.error)
            run_log.emit("button_ocr_finished", frame_sequence=frame_sequence,
                         error=error or self.reader.error, **self.last_match)
        if self.reader.error:
            raise RuntimeError(self.reader.error)
        # 未知文字可能是购买文案的 OCR 错字，不能永久缓存成不可购买。
        if label is not None or self.reader.last_text == "已预约":
            self.templates.append((mask, label))
            if len(self.templates) > 8:
                # 动态负样本可以淘汰，首击依赖的已标定模板不能被挤掉。
                self.templates.pop(self._calibrated_count)
        return label


def purchase_bounds(xml: str) -> tuple[int, int, int, int]:
    """兼容全宽和左右分栏购买按钮；不把左侧「添加想看」混进识别区域。"""
    page, point = parse_page(xml, (1200, 2670))
    nodes = [node for node in ET.fromstring(xml).iter("node")
             if node.get("resource-id") == DETAIL_RESOURCE and node.get("visible-to-user") != "false"]
    if page != "damai_other" or point != XIAOMI_15_MAIN_ACTION_POINT or len(nodes) != 1:
        raise RuntimeError("请停在小米 15 的演出详情页，且购买栏未被弹窗遮挡")
    values = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", nodes[0].get("bounds", ""))
    if values is None:
        raise RuntimeError("无法读取购买栏位置")
    left, top, right, bottom = map(int, values.groups())
    if not (340 <= left < right <= 1168 and 2428 <= top < bottom <= 2576):
        raise RuntimeError("购买栏布局变化，需重新校准视频裁剪区域")
    return left, top, right, bottom


def watch_purchase_button(device, adb_path: Path, serial: str, ready, stop, state, deadline: float,
                          *, initial_xml: str | None = None) -> None:
    """逐次记录识别和丢帧原因；确认后先放行点击，图片仍留到运行结束再写。"""
    try:
        if stop.is_set():
            return
        bounds = purchase_bounds(initial_xml if initial_xml is not None else device.dump_hierarchy())
        with ButtonTextReader(stop) as reader:
            # 冷启动模型可能耗时数十秒，准备期完成，不能留到文字变更后才加载。
            warmup_started = time.perf_counter()
            run_log.emit("button_ocr_warmup_started", started_monotonic=warmup_started)
            reader.read(device.screenshot().crop(bounds))
            run_log.emit("button_ocr_warmup_finished", duration_ms=round((time.perf_counter() - warmup_started) * 1000, 3),
                         error=reader.error)
            if reader.error:
                raise RuntimeError(reader.error)
            if stop.is_set():
                return
            matcher = ButtonMatcher(reader)
            with ButtonStream(adb_path, serial) as stream:
                ready_sequence = None
                try:
                    run_log.emit("purchase_button_watching", bounds=bounds, max_frame_age_ms=250,
                                 calibrated_templates=matcher._calibrated_names, template_threshold=.04)
                    sequence = 0
                    while not stop.is_set():
                        if time.perf_counter() >= deadline:
                            state.finish("timeout")
                            stop.set()
                            return
                        frame = stream.latest(sequence, timeout=.1)
                        if frame is None:
                            continue
                        previous_sequence = sequence
                        sequence, received, image = frame
                        received_at = time.perf_counter()
                        frame_fields = {"frame_sequence": sequence, "frame_received_monotonic": received,
                                        "frame_age_before_ms": round((received_at - received) * 1000, 3),
                                        "skipped_frames": max(0, sequence - previous_sequence - 1)}
                        if received_at - received > .25:
                            run_log.emit("button_frame_skipped", reason="stale_before_match", **frame_fields)
                            continue
                        local_bounds = (bounds[0] - 340, bounds[1] - 2428, bounds[2] - 340, bounds[3] - 2428)
                        started = time.perf_counter()
                        button = image.crop(local_bounds)
                        try:
                            label = matcher.match(button, frame_sequence=sequence)
                        except Exception as exc:
                            run_log.emit("button_frame_processed", decision="recognition_error", error=str(exc),
                                         recognition_started_monotonic=started,
                                         recognition_finished_monotonic=time.perf_counter(),
                                         method=matcher.method, **frame_fields, **matcher.last_match)
                            raise
                        finished = time.perf_counter()
                        decision = ("cancelled" if stop.is_set() else "deadline" if finished >= deadline
                                    else "stale_after_match" if finished - received > .25
                                    else "not_purchase_text" if label is None else "ready")
                        if decision == "ready":
                            released = time.perf_counter()
                            ready_sequence = sequence
                            ready.set()
                        trace = {**frame_fields, **matcher.last_match, "text": label, "method": matcher.method,
                                 "recognition_started_monotonic": started, "recognition_finished_monotonic": finished,
                                 "match_ms": round((finished - started) * 1000, 3),
                                 "frame_age_after_ms": round((finished - received) * 1000, 3)}
                        # 放行先于事件入队，日志线程负责序列化和写盘。
                        if decision == "ready":
                            run_log.emit("purchase_button_ready", ready_set_monotonic=released, **trace)
                        run_log.emit("button_frame_processed", decision=decision, **trace)
                        if decision != "ready":
                            continue
                        return
                finally:
                    # 清理视频流可能超过两秒，先留住帧引用，避免最近画面在清理时过期。
                    if run_log.active is not None:
                        run_log.active.keep_button_frames(stream.recent_frames(), ready_sequence, bounds)
    except Exception as exc:
        if not stop.is_set():
            state.finish("error", f"购买按钮检测失败：{exc}")
            run_log.emit("worker_error", worker="purchase_button", error=str(exc))
            stop.set()
