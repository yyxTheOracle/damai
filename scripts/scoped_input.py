"""通过预先连好的 ADB 通道给指定应用发触摸；Android 在投递时核对接收窗口的 UID。"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import select
import subprocess
import threading
import time

from device_support import SCREEN_SIZES, XIAOMI_15_MODEL


HELPER = Path(__file__).resolve().parent / 'android_scoped_input' / 'scoped_input.jar'
SCREEN_WIDTH, SCREEN_HEIGHT = SCREEN_SIZES[XIAOMI_15_MODEL]


class TargetMismatch(RuntimeError):
    """系统拒绝向其他 UID 的窗口投递，调用方必须停点。"""


class ScopedInput:
    """正式流程只向大麦投递；Chrome 仅供本机无订单页面验证同一套输入通道。"""

    TEST_PACKAGE = 'com.android.chrome'

    def __init__(self, adb_path: Path, serial: str, package: str = 'cn.damai') -> None:
        if package not in ('cn.damai', self.TEST_PACKAGE):
            raise ValueError('只允许向大麦或本机测试用的 Chrome 发送定向输入')
        self.adb = [str(adb_path), '-s', serial]
        self.remote = f'/data/local/tmp/damai-scoped-{secrets.token_hex(12)}.jar'
        self._lock = threading.Lock()
        self._pushed = False
        self._closed = False
        self._process = None
        self.error = None
        try:
            expected = HELPER.with_suffix('.jar.sha256').read_text(encoding='ascii').split()
            digest = hashlib.sha256(HELPER.read_bytes()).hexdigest()
            if expected != [digest, HELPER.name]:
                raise RuntimeError('定向输入 helper 的 SHA-256 校验失败')
            self.uid = self._unique_uid(package)
            self._pushed = True  # push 失败也可能在设备留下不完整的文件。
            self._adb('push', str(HELPER), self.remote)
            self._adb('shell', 'chmod', '444', self.remote)
            self._process = subprocess.Popen(self.adb + [
                'shell', '-T', f'CLASSPATH={self.remote}', 'exec', 'app_process',
                '/system/bin', 'dev.damai.ScopedInput', str(self.uid),
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            if self._reply(5) != f'READY {self.uid}':
                raise RuntimeError('设备未确认本次定向输入目标 UID')
        except BaseException:
            self.close()
            raise

    def _adb(self, *args: str) -> str:
        """安装与清理命令绑定本次设备，全部在点击路径以外执行。"""
        return subprocess.check_output(self.adb + list(args), text=True, timeout=5).strip()

    def _unique_uid(self, package: str) -> int:
        """从当前 Android 用户的完整包列表核对唯一归属，共用 UID 时无法隔离窗口。"""
        user = self._adb('shell', 'am', 'get-current-user')
        if not re.fullmatch(r'[0-9]+', user):
            raise RuntimeError('无法确定当前 Android 用户')
        packages = self._adb('shell', 'pm', 'list', 'packages', '-U', '--user', user)
        owners = {}
        for line in packages.splitlines():
            match = re.fullmatch(r'package:(\S+)\s+uid:([0-9]+)', line.strip())
            if not match:
                raise RuntimeError('Android 包列表不完整，无法核对 UID 归属')
            name, uid = match.groups()
            owners.setdefault(int(uid), set()).add(name)
        targets = [uid for uid, names in owners.items() if package in names]
        if len(targets) != 1 or owners[targets[0]] != {package}:
            raise RuntimeError(f'{package} 的 UID 缺失、重复或与其他包共享')
        uid = targets[0]
        # Android 按用户划分 100000 个 UID。此通道只接受独占 UID 的普通应用。
        if uid // 100000 != int(user) or uid % 100000 < 10000:
            raise RuntimeError(f'{package} 的 UID 不属于当前用户的普通应用')
        return uid

    def _reply(self, timeout: float) -> str:
        """按完整协议行读回执；半行或静默到期即失败，不会阻塞在 readline。"""
        fd = self._process.stdout.fileno()
        deadline = time.monotonic() + timeout
        data = bytearray()
        while len(data) < 128:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError('等待定向输入回执超时')
            chunk = os.read(fd, 128)
            if not chunk:
                raise RuntimeError('定向输入通道已断开')
            data.extend(chunk)
            if b'\n' in chunk:
                line, _, extra = data.partition(b'\n')
                if extra:
                    raise RuntimeError('定向输入收到额外回执')
                return line.decode('ascii')
        raise RuntimeError('定向输入回执过长')

    def tap(self, point: tuple[int, int]) -> None:
        """每次最多一个点击在途；即便回执丢失也不重试，避免重复提交。"""
        x, y = point
        if type(x) is not int or type(y) is not int or not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT):
            raise ValueError('坐标必须在小米 15 的 1200×2670 竖屏内')
        with self._lock:
            if self._closed or self.error or self._process.poll() is not None:
                raise RuntimeError(self.error or '定向输入通道已关闭')
            try:
                command = f'TAP {x} {y}\n'.encode('ascii')
                if self._process.stdin.write(command) != len(command):
                    raise RuntimeError('定向输入命令未完整送达')
                reply = self._reply(2)
                if reply == 'REJECT':
                    raise TargetMismatch('Android 拒绝向其他 UID 的窗口投递点击')
                if reply != 'OK':
                    raise RuntimeError(f'设备未确认定向点击：{reply}')
            except BaseException as exc:
                self.error = f'{type(exc).__name__}: {exc}'
                raise

    def back(self) -> None:
        """验证页由用户接管；此通道没有任何全局按键。"""
        raise RuntimeError('定向输入不支持返回键')

    def close(self) -> None:
        """关闭命令流让设备端退出；只移除本次上传的文件，重复调用安全。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._process is not None:
                self._process.stdin.close()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
                finally:
                    self._process.stdout.close()
            if self._pushed:
                try:
                    self._adb('shell', 'rm', '-f', self.remote)
                except (OSError, subprocess.SubprocessError) as exc:
                    logging.getLogger(__name__).info('设备上的临时输入 helper 未能删除 %s: %s', self.remote, exc)
