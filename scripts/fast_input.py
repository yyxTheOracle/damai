"""通过持久 scrcpy 控制连接发出触摸，避免每次点击都启动 Android input 进程。

固定协议来源（v5.0.1）：
https://github.com/Genymobile/scrcpy/blob/v5.0.1/app/src/control_msg.c
https://github.com/Genymobile/scrcpy/blob/v5.0.1/server/src/main/java/com/genymobile/scrcpy/control/Controller.java
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time

from button_stream import ButtonStream, SERVER, SERVER_SHA256


class FastInput(ButtonStream):
    """初始化连接，但不输入；tap/back 才发送事件，close 复用独立资源清理。

    仅面向小米 15 的 1200×2670 竖屏。没有视频时 server 使用原屏坐标，
    不会套用另一条按钮视频流的裁剪映射。发送成功仅表示写入连接，协议不提供执行回执。
    """

    def __init__(self, adb_path: Path, serial: str) -> None:
        super().__init__(adb_path, serial)
        self.remote = f'/data/local/tmp/damai-input-{self.scid}.jar'
        self._send_lock = threading.RLock()
        try:
            if hashlib.sha256(SERVER.read_bytes()).hexdigest() != SERVER_SHA256:
                raise RuntimeError('scrcpy server 校验失败，不能使用未知版本的输入协议')
            self._pushed = True
            self._adb('push', str(SERVER), self.remote)
            self._port = self._adb('forward', 'tcp:0', f'localabstract:scrcpy_{self.scid}')
            if not self._port.isdecimal():
                raise RuntimeError('ADB 没有返回有效的控制端口')
            self._log = tempfile.TemporaryFile()
            self._process = subprocess.Popen(self.adb + [
                'shell', f'CLASSPATH={self.remote}', 'app_process', '/',
                'com.genymobile.scrcpy.Server', '5.0.1', f'scid={self.scid}',
                'tunnel_forward=true', 'video=false', 'audio=false', 'control=true',
                'cleanup=false', 'power_on=false', 'clipboard_autosync=false', 'keep_active=false',
                'send_device_meta=false', 'send_dummy_byte=true', 'display_id=0',
            ], stdout=self._log, stderr=subprocess.STDOUT)
            self._connect()
        except BaseException as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            try:
                self.close()
            except RuntimeError:
                pass  # 清理错误保留在 error，不替换原始启动错误。
            raise

    def __enter__(self) -> FastInput:
        """兼容 with；构造时已经连接，不再启动第二个 server。"""
        if self._closed:
            raise RuntimeError('输入连接已经关闭')
        return self

    def _connect(self) -> None:
        """等 server 发回零字节握手，排除仅有 ADB forward、设备尚未监听的假连接。"""
        deadline = time.monotonic() + 5
        while True:
            try:
                self._socket = socket.create_connection(('127.0.0.1', int(self._port)), timeout=.5)
                self._socket.settimeout(.5)
                self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if self._receive(1, deadline) != b'\x00':
                    raise RuntimeError('scrcpy 控制连接握手不正确')
                return
            except (OSError, EOFError):
                if self._socket is not None:
                    self._socket.close()
                if time.monotonic() >= deadline:
                    raise TimeoutError('无法建立输入连接')
                time.sleep(.05)

    def _send(self, data: bytes) -> None:
        """一对按下/抬起同次发送；失败后拒绝后续输入，不重发可能已执行的点击。"""
        with self._send_lock:
            if self._closed or self.error or self._process.poll() is not None:
                raise RuntimeError(self.error or '输入连接已关闭或服务已退出')
            try:
                self._socket.sendall(data)
            except OSError as exc:
                self.error = f'输入发送失败: {exc}'
                raise RuntimeError(self.error) from exc

    def tap(self, point: tuple[int, int]) -> None:
        """32 字节触摸包：type/action/pointer/x/y/原屏宽高/pressure/两个 buttons 字段。"""
        x, y = point
        if type(x) is not int or type(y) is not int or not (0 <= x < 1200 and 0 <= y < 2670):
            raise ValueError('点击位置必须在小米 15 的 1200×2670 竖屏内')
        # -2 是官方 GENERIC_FINGER；触摸没有鼠标按键，压力 0xffff 表示 1.0。
        packets = b''.join(struct.pack('>BBqiiHHHII', 2, action, -2, x, y, 1200, 2670,
                                       0xffff if action == 0 else 0, 0, 0) for action in (0, 1))
        self._send(packets)

    def back(self) -> None:
        """发普通 BACK 键的按下/抬起；不使用会在熄屏时唤醒设备的 BACK_OR_SCREEN_ON。"""
        self._send(b''.join(struct.pack('>BBIII', 0, action, 4, 0, 0) for action in (0, 1)))

    def close(self) -> None:
        """与输入互斥，随后复用父类清理本实例 socket、server、forward 和 jar。"""
        with self._send_lock:
            super().close()
