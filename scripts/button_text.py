"""在准备期启动 macOS Vision，随后只识别调用方提供的按钮裁图，不操作手机。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import select
import subprocess
import time

from PIL import Image


class ButtonTextReader:
    """单调用方的持久 OCR 进程；首帧用于预热，识别失败始终不放行购买。"""

    def __init__(self, stop_event=None) -> None:
        """把所有外部进程创建留到进入上下文时，导入模块不会编译或启动工具。"""
        self.process: subprocess.Popen | None = None
        self.warmed = False
        self.last_text: str | None = None
        self.error: str | None = None
        self.stop_event = stop_event

    def __enter__(self) -> ButtonTextReader:
        self.start()
        return self

    def start(self) -> None:
        """按 Swift 源码哈希缓存编译结果，在开抢准备期建立进程和双向管道。"""
        if self.process is not None:
            return
        if self.stop_event is not None and self.stop_event.is_set():
            raise InterruptedError("按钮识别已取消")
        source = Path(__file__).with_name("button_ocr.swift")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        binary = source.parent.parent / "artifacts" / "tool_cache" / f"button_ocr_{digest}"
        binary.parent.mkdir(parents=True, exist_ok=True)
        if not binary.is_file():
            subprocess.run(["/usr/bin/swiftc", str(source), "-o", str(binary)],
                           check=True, capture_output=True, timeout=60)
        if self.stop_event is not None and self.stop_event.is_set():
            raise InterruptedError("按钮识别已取消")
        self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.warmed = False
        self.last_text = self.error = None
        for pipe in (self.process.stdin, self.process.stdout):
            os.set_blocking(pipe.fileno(), False)

    def __exit__(self, *_: object) -> None:
        """离开上下文时回收 OCR 进程，识别超时也不会留下后台任务。"""
        self.close()

    def close(self) -> None:
        """可重复关闭；必要时强制结束无响应的识别进程。"""
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for pipe in (process.stdin, process.stdout):
            pipe.close()

    def _exchange(self, payload: bytes, timeout: float) -> bytes:
        """对整次收发设截止时间，避免堵塞写管道或只有半行回复时无限等待。"""
        deadline = time.perf_counter() + timeout
        incoming, outgoing = self.process.stdout.fileno(), self.process.stdin.fileno()
        pending = memoryview(payload)
        result = bytearray()
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError("按钮识别已取消")
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError("按钮识别超时")
            readable, writable, _ = select.select([incoming] if not pending else [],
                                                  [outgoing] if pending else [], [], min(remaining, .1))
            # 等待期间也可能取消或超时，不能再接受刚到达的购买文字。
            if self.stop_event is not None and self.stop_event.is_set():
                raise InterruptedError("按钮识别已取消")
            if time.perf_counter() >= deadline:
                raise TimeoutError("按钮识别超时")
            if writable:
                pending = pending[os.write(outgoing, pending):]
            if readable:
                chunk = os.read(incoming, 65536)
                if not chunk:
                    raise EOFError("按钮识别进程已结束")
                result.extend(chunk)
                if len(result) > 65536:
                    raise ValueError("按钮识别返回异常")
                if b"\n" in result:
                    return bytes(result)

    def read_lines(self, image: Image.Image) -> list[str]:
        """按画面从上到下返回所有识别行，供验证提示跨行匹配；错误见 error。"""
        self.last_text = self.error = None
        if self.process is None or self.process.poll() is not None:
            self.error = "按钮识别进程未运行"
            return []
        try:
            png = io.BytesIO()
            image.save(png, format="PNG")
            payload = json.dumps(base64.b64encode(png.getvalue()).decode("ascii")).encode() + b"\n"
            texts = json.loads(self._exchange(payload, 2 if self.warmed else 60))
            if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
                raise ValueError("按钮识别返回格式不正确")
            self.warmed = True
            return texts
        except (OSError, ValueError, EOFError, subprocess.SubprocessError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.close()
            return []

    def read(self, image: Image.Image) -> str | None:
        """购买按钮只看主行，防止副标题中提到购买就放行。"""
        texts = self.read_lines(image)
        label = texts[0].strip() if texts else ""
        self.last_text = label or None
        return label if label in ("立即预订", "立即购票", "立即购买") else None
