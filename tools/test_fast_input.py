"""只使用模拟 ADB/socket：验证 v5.0.1 输入字节、启动行为和独立清理范围。"""
import hashlib
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import fast_input as module


class FastInputTests(unittest.TestCase):
    def setUp(self):
        """统一封住全部外部入口；测试不能接触 USB 或真实文件。"""
        self.commands = []
        self.sock = Mock()
        self.sock.recv.return_value = b'\x00'
        self.process = Mock()
        self.process.poll.return_value = None
        self.adb = self.enterContext(patch.object(module.FastInput, '_adb', side_effect=self.adb_command))
        self.spawn = self.enterContext(patch.object(module.subprocess, 'Popen', return_value=self.process))
        self.connect = self.enterContext(patch.object(module.socket, 'create_connection', return_value=self.sock))
        self.enterContext(patch.object(Path, 'read_bytes', return_value=b'server'))
        self.enterContext(patch.object(module, 'SERVER_SHA256', hashlib.sha256(b'server').hexdigest()))

    def adb_command(self, *args):
        """只返回当前测试需要的端口；清理时设备上的其他进程不应被终止。"""
        self.commands.append(args)
        if args[:2] == ('forward', 'tcp:0'):
            return '54321'
        if args[:2] == ('shell', 'ps'):
            return '22 app_process / com.genymobile.scrcpy.Server scid=foreign'
        return ''

    def make_input(self):
        channel = module.FastInput(Path('/test/adb'), 'device-test')
        self.addCleanup(channel.close)
        return channel

    def test_start_only_connects_without_sending_input_or_changing_settings(self):
        channel = self.make_input()
        with channel as entered:
            self.assertIs(entered, channel)
            self.assertEqual(self.spawn.call_count, 1)
            self.sock.sendall.assert_not_called()
        args = self.spawn.call_args.args[0]
        for option in ('video=false', 'audio=false', 'control=true', 'power_on=false',
                       'clipboard_autosync=false', 'keep_active=false', 'send_dummy_byte=true',
                       'send_device_meta=false', 'display_id=0'):
            self.assertIn(option, args)
        self.assertFalse(any(option.startswith('crop=') for option in args))
        self.sock.setsockopt.assert_called_once_with(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.assertFalse(any('input' in command for command in self.commands))

    def test_tap_is_exactly_two_32_byte_touch_packets_using_full_screen_coordinates(self):
        channel = self.make_input()
        channel.tap((1020, 2500))
        data = self.sock.sendall.call_args.args[0]
        self.assertEqual(len(data), 64)
        self.assertEqual(struct.unpack('>BBqiiHHHII', data[:32]),
                         (2, 0, -2, 1020, 2500, 1200, 2670, 65535, 0, 0))
        self.assertEqual(struct.unpack('>BBqiiHHHII', data[32:]),
                         (2, 1, -2, 1020, 2500, 1200, 2670, 0, 0, 0))
        self.sock.sendall.assert_called_once()
        self.assertEqual(self.spawn.call_count, 1)

    def test_back_is_keycode_down_and_up_without_screen_on_message(self):
        self.make_input().back()
        data = self.sock.sendall.call_args.args[0]
        self.assertEqual(len(data), 28)
        self.assertEqual(struct.unpack('>BBIII', data[:14]), (0, 0, 4, 0, 0))
        self.assertEqual(struct.unpack('>BBIII', data[14:]), (0, 1, 4, 0, 0))

    def test_invalid_coordinates_never_send(self):
        channel = self.make_input()
        for point in [(-1, 0), (1200, 0), (0, 2670), (1.5, 2), (True, 1)]:
            with self.subTest(point=point), self.assertRaises(ValueError):
                channel.tap(point)
        self.sock.sendall.assert_not_called()

    def test_send_failure_is_not_retried_and_blocks_future_input(self):
        channel = self.make_input()
        self.sock.sendall.side_effect = BrokenPipeError('disconnected')
        with self.assertRaisesRegex(RuntimeError, '输入发送失败'):
            channel.tap((10, 20))
        with self.assertRaisesRegex(RuntimeError, '输入发送失败'):
            channel.back()
        self.sock.sendall.assert_called_once()

    def test_closed_channel_and_exited_server_never_send(self):
        channel = self.make_input()
        self.process.poll.return_value = 1
        with self.assertRaises(RuntimeError):
            channel.back()
        channel.close()
        with self.assertRaises(RuntimeError):
            channel.tap((10, 20))
        self.sock.sendall.assert_not_called()

    def test_hash_mismatch_does_not_touch_adb(self):
        with patch.object(module, 'SERVER_SHA256', 'wrong'), self.assertRaisesRegex(RuntimeError, '校验失败'):
            module.FastInput(Path('/test/adb'), 'device-test')
        self.adb.assert_not_called()
        self.spawn.assert_not_called()

    def test_start_failure_removes_only_allocated_resources(self):
        self.spawn.side_effect = OSError('cannot start')
        with self.assertRaisesRegex(OSError, 'cannot start'):
            module.FastInput(Path('/test/adb'), 'device-test')
        self.assertIn(('forward', '--remove', 'tcp:54321'), self.commands)
        remote = self.commands[0][2]
        self.assertIn(('shell', 'rm', '-f', remote), self.commands)
        self.sock.sendall.assert_not_called()

    def test_failed_handshake_cleans_server_forward_and_jar(self):
        self.sock.recv.return_value = b'\x01'
        with self.assertRaisesRegex(RuntimeError, '握手不正确'):
            module.FastInput(Path('/test/adb'), 'device-test')
        self.assertIn(('forward', '--remove', 'tcp:54321'), self.commands)
        self.assertIn(('shell', 'rm', '-f', self.commands[0][2]), self.commands)
        self.sock.shutdown.assert_called_once()
        self.process.wait.assert_called_once()

    def test_connection_refused_retries_without_input(self):
        self.connect.side_effect = [ConnectionRefusedError(), self.sock]
        with patch.object(module.time, 'sleep'):
            self.make_input()
        self.assertEqual(self.connect.call_count, 2)
        self.sock.sendall.assert_not_called()

    def test_close_is_idempotent_and_does_not_touch_foreign_resources(self):
        channel = self.make_input()
        self.adb.side_effect = lambda *args: (
            f'11 app_process / com.genymobile.scrcpy.Server scid={channel.scid}\n'
            '22 app_process / com.genymobile.scrcpy.Server scid=foreign'
            if args[:2] == ('shell', 'ps') else self.adb_command(*args))
        channel.close()
        channel.close()
        self.assertIn(('shell', 'kill', '11'), self.commands)
        self.assertNotIn(('shell', 'kill', '22'), self.commands)
        self.assertEqual(self.commands.count(('forward', '--remove', 'tcp:54321')), 1)
        self.assertEqual(self.commands.count(('shell', 'rm', '-f', channel.remote)), 1)
        self.assertFalse(any('--remove-all' in command for command in self.commands))


if __name__ == '__main__':
    unittest.main()
