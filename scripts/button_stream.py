"""小米 15 按钮只读流；识别只取最新帧，短期留证只存内存引用，不建立输入通道。"""
from __future__ import annotations

from collections import deque
import hashlib
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import tempfile
import threading
import time

import av
from PIL import Image


SERVER = Path(__file__).resolve().parent.parent / 'artifacts/dependencies/scrcpy-server-v5.0.1'
SERVER_SHA256 = '764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536'


class ButtonStream:
    """提前连接裁剪流，latest 取最新画面；离开 with 时清理本实例的所有设备资源。"""

    def __init__(self, adb_path: Path, serial: str, crop=(340, 2428, 828, 148)) -> None:
        x, y, width, height = crop
        if (width, height) != (828, 148) or not (0 <= x <= 372 and 0 <= y <= 2522):
            raise ValueError('按钮流仅支持小米 15 的 828×148 裁剪区域')
        self.adb = [str(adb_path), '-s', serial]
        self.crop = crop
        self.scid = f'{secrets.randbelow(0x7ffffffe) + 1:08x}'
        self.remote = f'/data/local/tmp/damai-button-{self.scid}.jar'
        self.error: str | None = None
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._last: tuple[int, float, Image.Image] | None = None
        self._recent: deque[tuple[int, float, Image.Image]] = deque(maxlen=120)
        self._thread = self._socket = self._process = self._port = self._log = None
        self._pushed = self._closed = False

    def _adb(self, *args: str) -> str:
        """所有调用绑定本机 serial；清理只使用本实例端口和随机命名文件。"""
        return subprocess.check_output(self.adb + list(args), text=True,
                                       stderr=subprocess.STDOUT, timeout=10).strip()

    def __enter__(self) -> ButtonStream:
        if self._closed or self._process is not None:
            raise RuntimeError('ButtonStream 不能重复启动')
        try:
            if hashlib.sha256(SERVER.read_bytes()).hexdigest() != SERVER_SHA256:
                raise RuntimeError('scrcpy server 校验失败，不能使用未知版本的帧协议')
            # push 中途失败也可能留下部分文件，因此先记下清理责任。
            self._pushed = True
            self._adb('push', str(SERVER), self.remote)
            self._port = self._adb('forward', 'tcp:0', f'localabstract:scrcpy_{self.scid}')
            if not self._port.isdecimal():
                raise RuntimeError('ADB 没有返回有效的视频端口')
            self._log = tempfile.TemporaryFile()
            x, y, width, height = self.crop
            self._process = subprocess.Popen(self.adb + [
                'shell', f'CLASSPATH={self.remote}', 'app_process', '/',
                'com.genymobile.scrcpy.Server', '5.0.1', f'scid={self.scid}',
                'tunnel_forward=true', 'audio=false', 'control=false', 'cleanup=false', 'power_on=false',
                'send_device_meta=false', 'send_dummy_byte=false', 'send_stream_meta=false',
                'send_frame_meta=true', 'video_codec=h264', 'max_fps=60', 'video_bit_rate=4000000',
                f'crop={width}:{height}:{x}:{y}',
            ], stdout=self._log, stderr=subprocess.STDOUT)
            self._thread = threading.Thread(target=self._read, name='damai-button-video', daemon=True)
            self._thread.start()
            return self
        except BaseException as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            try:
                self.close()
            except RuntimeError:
                pass  # 清理错误留在 error，启动方仍收到最初的失败原因。
            raise

    def _receive(self, size: int, deadline: float | None = None) -> bytes:
        """短读和超时不丢掉半包；停止或服务退出时结束等待。"""
        data = bytearray()
        while len(data) < size:
            if self._stop.is_set():
                raise EOFError('视频流已停止')
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('等待视频首包超时')
            if self._process.poll() is not None:
                self._log.seek(0)
                raise RuntimeError('视频服务已退出: ' + self._log.read(2048).decode(errors='replace'))
            try:
                chunk = self._socket.recv(size-len(data))
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError('视频连接已关闭')
            data.extend(chunk)
        return bytes(data)

    def _read(self) -> None:
        """按帧头立即解码，不等下一帧、不写 PNG，慢消费者只会拿到最新画面。"""
        try:
            deadline = time.monotonic() + 5
            while not self._stop.is_set():
                try:
                    self._socket = socket.create_connection(('127.0.0.1', int(self._port)), timeout=.5)
                    self._socket.settimeout(.5)
                    header = self._receive(12, deadline)
                    break
                except (OSError, EOFError):
                    if self._socket is not None:
                        self._socket.close()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('无法建立视频连接')
                    self._stop.wait(.05)
            else:
                return
            codec = av.CodecContext.create('h264', 'r')
            codec.thread_count = 1
            config = b''
            sequence = 0
            while not self._stop.is_set():
                flags, size = struct.unpack('>QI', header)
                if flags & (1 << 63) or not 0 < size <= 8*1024*1024:
                    raise ValueError('scrcpy 帧头不符合已校验版本协议')
                payload = self._receive(size)
                received = time.monotonic()
                if flags & (1 << 62):
                    config = payload
                else:
                    for frame in codec.decode(av.Packet(config + payload)):
                        if (frame.width, frame.height) != (828, 148):
                            raise ValueError('按钮视频尺寸/方向发生变化')
                        picture = frame.to_image()
                        sequence += 1
                        with self._condition:
                            self._last = (sequence, received, picture)
                            self._recent.append(self._last)  # 复用图像引用，不复制或写盘。
                            self._condition.notify_all()
                    config = b''
                header = self._receive(12)
        except Exception as exc:
            if not self._stop.is_set():
                with self._condition:
                    self.error = f'{type(exc).__name__}: {exc}'
                    self._condition.notify_all()
        finally:
            if self._socket is not None:
                self._socket.close()

    def latest(self, after_sequence: int = 0, timeout: float = 1) -> tuple[int, float, Image.Image] | None:
        """返回比指定序号更新的一帧；空闲返回 None，断流不能回退使用旧画面。"""
        if after_sequence < 0 or timeout < 0:
            raise ValueError('序号和等待时间不能为负数')
        with self._condition:
            self._condition.wait_for(lambda: self.error or self._stop.is_set()
                                     or self._last is not None and self._last[0] > after_sequence, timeout)
            if self.error:
                raise RuntimeError(self.error)
            if self._stop.is_set() or self._last is None or self._last[0] <= after_sequence:
                return None
            return self._last

    def recent_frames(self) -> list[tuple[int, float, Image.Image]]:
        """按接收顺序返回最近 2 秒、最多 120 帧；列表独立，PIL 图像共享并须只读使用。"""
        cutoff = time.monotonic() - 2
        with self._condition:
            return [frame for frame in self._recent if frame[1] >= cutoff]

    def close(self) -> None:
        """先停接收，再回收本实例的 server、forward 和 jar；一项失败也继续清理其他项。"""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        errors = []
        with self._condition:
            self._condition.notify_all()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._socket.close()
            except OSError:
                pass
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2)
        if self._process is not None:
            try:
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=1)
            except Exception as exc:
                errors.append(f'adb process: {exc}')
            try:
                for row in self._adb('shell', 'ps', '-A', '-o', 'PID,ARGS').splitlines():
                    parts = row.split()
                    if parts and parts[0].isdecimal() and f'scid={self.scid}' in parts and 'app_process' in row:
                        self._adb('shell', 'kill', parts[0])
            except Exception as exc:
                errors.append(f'server: {exc}')
        for args in ([('forward', '--remove', 'tcp:'+self._port)] if self._port else []) + (
                [('shell', 'rm', '-f', self.remote)] if self._pushed else []):
            try:
                self._adb(*args)
            except Exception as exc:
                errors.append(str(exc))
        if self._log is not None:
            self._log.close()
        if errors:
            self.error = '视频资源清理失败: ' + '; '.join(errors)
            raise RuntimeError(self.error)

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.close()
        except RuntimeError:
            if exc is None:
                raise
