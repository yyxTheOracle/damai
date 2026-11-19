"""将实战事件异步写入独立目录，点击线程不直接写文件。"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
import json
from pathlib import Path
import queue
import sys
import threading
import time


class _Tee:
    """终端输出同时留档；原始终端失效时仍尝试保存文件。"""

    def __init__(self, stream, file, lock, report_error) -> None:
        self.stream, self.file, self.lock, self.report_error = stream, file, lock, report_error

    def write(self, text: str) -> int:
        with self.lock:
            for destination in (self.stream, self.file):
                try:
                    destination.write(text)
                    destination.flush()
                except Exception as exc:
                    self.report_error("console", exc)
        return len(text)

    def flush(self) -> None:
        with self.lock:
            for destination in (self.stream, self.file):
                try:
                    destination.flush()
                except Exception as exc:
                    self.report_error("console flush", exc)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class RunLog:
    """一场运行一个目录；结束前调用 close，确保已入队事件全部处理。"""

    def __init__(self, directory: Path, sale_time: datetime) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        self._events = (self.directory / "events.jsonl").open("x", encoding="utf-8")
        self._stderr = sys.stderr
        self.errors: list[str] = []
        self._error_lock = threading.Lock()
        self._queue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        self._button_frames = None
        wall = datetime.now().astimezone()
        self._sale_monotonic = time.perf_counter() + (sale_time.astimezone() - wall).total_seconds()
        self._writer = threading.Thread(target=self._write_loop, name="damai-run-log", daemon=True)
        self._writer.start()

    def emit(self, event: str, *, xml_text: str | None = None, **fields) -> None:
        """只复制事件并入队；时间戳记录调用时刻，XML 写盘留给后台。"""
        with self._lock:
            if self._closed:
                return
            now = time.perf_counter()
            self._sequence += 1
            record = {
                **fields,
                "event": event,
                "sequence": self._sequence,
                "wall_time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "monotonic_s": round(now, 6),
                "seconds_after_sale": round(now - self._sale_monotonic, 6),
            }
            self._queue.put((record, xml_text))

    def _report_error(self, operation: str, exc: Exception) -> None:
        """记录持久化失败并提示终端，同一故障不重复刷屏。"""
        message = f"{operation}: {type(exc).__name__}: {exc}"
        with self._error_lock:
            if message in self.errors:
                return
            self.errors.append(message)
        try:
            print(f"[run_log] {message}", file=self._stderr, flush=True)
        except Exception:
            pass  # 终端关闭时，调用方仍可从 errors 读取故障。

    def keep_button_frames(self, frames, ready_sequence: int | None, bounds: tuple[int, int, int, int]) -> None:
        """保留末尾帧引用，未放行时序号为 None；收尾才写图，不额外读取手机。"""
        with self._lock:
            if not self._closed:
                self._button_frames = (list(frames), ready_sequence, bounds)

    def _save_button_frames(self, frames, ready_sequence, bounds) -> None:
        """收尾保存采集时间线和触发帧；单张写失败也保留其他帧及错误说明。"""
        directory = self.directory / "button_frames"
        index = {"ready_sequence": ready_sequence, "bounds": bounds,
                 "time_basis": "Mac 收到画面的 monotonic 时间，不是手机显示时间", "frames": []}
        try:
            directory.mkdir()
        except Exception as exc:
            self._report_error("button frames directory", exc)
            return
        for sequence, received, image in frames:
            entry = {"sequence": sequence, "received_monotonic": received,
                     "path": f"button_frames/{sequence}.png"}
            index["frames"].append(entry)
            try:
                image.save(self.directory / entry["path"])
                if sequence == ready_sequence:
                    left, top, right, bottom = bounds
                    image.crop((left - 340, top - 2428, right - 340, bottom - 2428)).save(
                        self.directory / "purchase_button_ready.png")
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                self._report_error(f"button frame {sequence}", exc)
        try:
            (directory / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
        except Exception as exc:
            self._report_error("button frames index", exc)

    def _write_loop(self) -> None:
        """单线程按入队顺序保存事件；单条失败不会阻止排空剩余记录。"""
        try:
            while (item := self._queue.get()) is not None:
                record, xml_text = item
                if xml_text is not None:
                    name = f"frame_{record['sequence']:06d}.xml"
                    try:
                        (self.directory / name).write_text(xml_text, encoding="utf-8")
                        record["xml_path"] = name
                    except Exception as exc:
                        record["xml_error"] = f"{type(exc).__name__}: {exc}"
                        self._report_error("XML", exc)
                try:
                    self._events.write(json.dumps(record, ensure_ascii=False) + "\n")
                    self._events.flush()
                except Exception as exc:
                    self._report_error("events", exc)
        finally:
            try:
                self._events.close()
            except Exception as exc:
                self._report_error("events close", exc)

    def close(self) -> None:
        """停止接收事件并等待后台排空；调用前应先结束产生事件的线程。"""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(None)
        self._writer.join()
        with self._lock:
            frames, self._button_frames = self._button_frames, None
        if frames is not None:
            self._save_button_frames(*frames)

    @contextmanager
    def capture_console(self):
        """保留现有终端显示，同时将 stdout 和 stderr 存进 console.log。"""
        lock = threading.Lock()
        with (self.directory / "console.log").open("a", encoding="utf-8") as file:
            with redirect_stdout(_Tee(sys.stdout, file, lock, self._report_error)), redirect_stderr(
                _Tee(sys.stderr, file, lock, self._report_error)
            ):
                yield

    def write_result(self, result: dict) -> None:
        """结束后独立保存结果，临时文件完整写入后才替换正式结果。"""
        temporary = self.directory / "result.json.tmp"
        try:
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.directory / "result.json")
        except Exception as exc:
            self._report_error("result", exc)
            raise


active: RunLog | None = None


def emit(event: str, *, xml_text: str | None = None, **fields) -> None:
    """未启用实战记录时不做任何事，方便原有入口与离线测试复用。"""
    recorder = active
    if recorder is not None:
        recorder.emit(event, xml_text=xml_text, **fields)
