"""离线验证定向输入。子进程只扮演 helper，覆盖真实管道的超时、断开与串行回执。"""
import concurrent.futures
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import scoped_input as module


class ScopedInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.jar = self.tmp / 'scoped_input.jar'
        self.jar.write_bytes(b'test dex')
        digest = hashlib.sha256(self.jar.read_bytes()).hexdigest()
        self.jar.with_suffix('.jar.sha256').write_text(f'{digest}  scoped_input.jar\n')
        self.enterContext(patch.object(module, 'HELPER', self.jar))
        self.packages = 'package:cn.damai uid:10420\npackage:com.android.chrome uid:10421'
        self.user = '0'
        self.commands = []
        self.adb = self.enterContext(patch.object(module.ScopedInput, '_adb', self.fake_adb))
        self.behavior = "print('OK', flush=True)"
        self.handshake = "print('READY ' + sys.argv[1], flush=True)"
        self.real_popen = subprocess.Popen
        self.spawn = self.enterContext(patch.object(module.subprocess, 'Popen', side_effect=self.fake_process))
        self.channel = None

    def fake_adb(self, *args):
        # 显式校验每个子命令：任何意外调用（包括普通 adb input）都使测试失败。
        self.commands.append(args)
        if args == ('shell', 'am', 'get-current-user'):
            return self.user
        if args == ('shell', 'pm', 'list', 'packages', '-U', '--user', self.user):
            return self.packages
        if args[0] == 'push' or args[:3] in (('shell', 'chmod', '444'), ('shell', 'rm', '-f')):
            return ''
        raise AssertionError(args)

    def fake_process(self, args, **kwargs):
        self.assertEqual(args[:5], ['fake-adb', '-s', 'testing', 'shell', '-T'])
        self.assertEqual(args[6:-1], ['exec', 'app_process', '/system/bin', 'dev.damai.ScopedInput'])
        program = f"""
import os, sys, time, select
{self.handshake}
for line in sys.stdin:
    with open(sys.argv[2], 'a') as log:
        log.write(line)
    {self.behavior}
"""
        return self.real_popen([sys.executable, '-u', '-c', program, args[-1], str(self.tmp / 'calls')], **kwargs)

    def connect(self, **kwargs):
        self.channel = module.ScopedInput(Path('fake-adb'), 'testing', **kwargs)
        self.addCleanup(self.channel.close)
        return self.channel

    def calls(self):
        path = self.tmp / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def test_only_sends_tap_to_acknowledged_uid_and_cleans_unique_resources(self):
        first = self.connect()
        second = self.connect(package=module.ScopedInput.TEST_PACKAGE)
        self.assertEqual((first.uid, second.uid), (10420, 10421))
        self.assertNotEqual(first.remote, second.remote)
        self.assertEqual(self.calls(), [])
        first.tap((930, 2502))
        self.assertEqual(self.calls(), ['TAP 930 2502'])
        self.assertEqual(self.spawn.call_count, 2)
        first.close()
        first.close()
        self.assertEqual(self.commands.count(('shell', 'rm', '-f', first.remote)), 1)
        self.assertNotIn(('shell', 'rm', '-f', second.remote), self.commands)
        with self.assertRaises(RuntimeError):
            first.tap((50, 60))

    def test_shared_uid_missing_or_partial_package_name_rejected_before_push(self):
        for packages in (self.packages + '\npackage:other.app uid:10420',
                         'package:cn.damai.clone uid:10420', '', 'invalid',
                         self.packages + '\npackage:cn.damai uid:10429', 'package:cn.damai uid:1000'):
            with self.subTest(packages=packages):
                self.packages = packages
                with self.assertRaises(RuntimeError):
                    self.connect()
        self.spawn.assert_not_called()
        self.assertFalse(any(args[0] == 'push' for args in self.commands))

    def test_non_owner_android_user(self):
        self.user = '10'
        with self.assertRaises(RuntimeError):
            self.connect()
        self.packages = 'package:cn.damai uid:1010420'
        self.assertEqual(self.connect().uid, 1010420)

    def test_unknown_package_and_bad_sha_never_reach_device(self):
        with self.assertRaises(ValueError):
            self.connect(package='com.eg.android.AlipayGphone')
        self.jar.write_bytes(b'other dex')
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            self.connect()
        self.assertEqual(self.commands, [])

    def test_wrong_ready_uid_cleans_up_before_any_tap(self):
        self.handshake = "print('READY 10421', flush=True)"
        with self.assertRaisesRegex(RuntimeError, '未确认'):
            self.connect()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.commands[-1][:3], ('shell', 'rm', '-f'))

    def test_reject_latches_even_if_target_comes_back(self):
        self.behavior = "print('REJECT', flush=True)"
        channel = self.connect()
        with self.assertRaises(module.TargetMismatch):
            channel.tap((1, 2))
        with self.assertRaises(RuntimeError):
            channel.tap((1, 2))
        self.assertEqual(self.calls(), ['TAP 1 2'])

    def test_timeout_partial_response_eof_and_engine_errors_never_retry(self):
        for behavior in ("os.write(1, b'O'); time.sleep(.2)", 'break',
                         "print('ERROR', flush=True)", "print('OK\\nOK', flush=True)"):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                channel = self.connect()
                real_reply = channel._reply
                # 缩短 ACK 等待，保留 select、真实管道和半行读取逻辑。
                with patch.object(channel, '_reply', side_effect=lambda _: real_reply(.05)):
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        channel.tap((10, 20))
                with self.assertRaises(RuntimeError):
                    channel.tap((10, 20))
                channel.close()
        self.assertEqual(self.calls(), ['TAP 10 20'] * 4)

    def test_second_sender_waits_for_first_ack(self):
        # 用无缓冲读取检查第一击获回执前，第二个调用是否提前写入了管道。
        self.behavior = "print('ERROR' if select.select([sys.stdin], [], [], .05)[0] else 'OK', flush=True)"
        channel = self.connect()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(channel.tap, [(5, 6), (7, 8)]))
        self.assertCountEqual(self.calls(), ['TAP 5 6', 'TAP 7 8'])

    def test_invalid_points_and_back_do_not_send(self):
        channel = self.connect()
        for point in ((-1, 5), (1200, 100), (12, 2670), (True, 0), (2.5, 1)):
            with self.assertRaises(ValueError):
                channel.tap(point)
        with self.assertRaises(RuntimeError):
            channel.back()
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
