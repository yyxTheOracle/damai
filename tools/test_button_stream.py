"""不连接手机：验证帧协议、只保留最新帧和本实例资源的清理范围。"""
import hashlib
import io
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import button_stream as module


class ButtonStreamTests(unittest.TestCase):
    def make_stream(self):
        stream = module.ButtonStream(Path('/test/adb'), 'device-test')
        stream._port = '54321'
        stream._process = Mock()
        stream._process.poll.return_value = None
        stream._log = io.BytesIO()
        return stream

    def decode(self, wire, frames):
        """精确按 recv 请求长度返回短读，末尾在真实 latest 接口读取最终保留的一帧。"""
        stream = self.make_stream()
        buffer = bytearray(wire)
        observed = []
        sock = Mock()

        def recv(size):
            if not buffer:
                observed.append(stream.latest(timeout=0))
                stream._stop.set()
                return b''
            chunk = bytes(buffer[:min(size, 7)])
            del buffer[:len(chunk)]
            return chunk

        sock.recv.side_effect = recv
        codec = Mock()
        codec.decode.side_effect = frames
        with patch.object(module.socket, 'create_connection', return_value=sock), \
                patch.object(module.av.CodecContext, 'create', return_value=codec), \
                patch.object(module.av, 'Packet', side_effect=lambda data: data):
            stream._read()
        return stream, codec, observed

    def test_decodes_framed_h264_and_keeps_only_latest_image(self):
        """配置包并入首帧，不经过流容器探测；第二帧替换第一帧而不是排队。"""
        wire = b''.join(struct.pack('>QI', flags, len(data)) + data for flags, data in (
            (1 << 62, b'config'), (1 << 61, b'first'), (1234, b'second')))
        first, second = Image.new('RGB', (828,148), 'red'), Image.new('RGB', (828,148), 'blue')
        frames = [[Mock(width=828, height=148, to_image=lambda: first)],
                  [Mock(width=828, height=148, to_image=lambda: second)]]
        stream, codec, observed = self.decode(wire, frames)
        self.assertIsNone(stream.error)
        self.assertEqual(codec.decode.call_args_list[0].args, (b'configfirst',))
        self.assertEqual(codec.decode.call_args_list[1].args, (b'second',))
        self.assertEqual(observed[0][0], 2)
        self.assertIs(observed[0][2], second)
        self.assertEqual(codec.thread_count, 1)

    def test_size_change_and_invalid_header_fail_without_publishing_a_frame(self):
        cases = [
            (struct.pack('>QI', 0, 1)+b'x', [[Mock(width=148,height=828)]]),
            (struct.pack('>QI', 0, 0), []),
            (struct.pack('>QI', 0, 9*1024*1024), []),
            (struct.pack('>QI', 1 << 63, 1), []),
        ]
        for wire, frames in cases:
            with self.subTest(wire=wire):
                stream, _, _ = self.decode(wire, frames)
                self.assertIsNotNone(stream.error)
                self.assertIsNone(stream._last)
                with self.assertRaises(RuntimeError):
                    stream.latest(timeout=0)

    def test_recent_frames_are_bounded_ordered_and_expire_after_two_seconds(self):
        """超过容量后保留最新 120 帧，低帧率或停流时也不返回两秒前的画面。"""
        picture = Image.new('RGB', (828, 148))
        wire = (struct.pack('>QI', 0, 1) + b'x') * 125
        frames = [[Mock(width=828, height=148, to_image=lambda: picture)] for _ in range(125)]
        with patch.object(module.time, 'monotonic', return_value=10):
            stream, _, observed = self.decode(wire, frames)
            recent = stream.recent_frames()
        self.assertEqual([frame[0] for frame in recent], list(range(6, 126)))
        self.assertTrue(all(frame[2] is picture for frame in recent))
        self.assertEqual(observed[0][0], 125)  # latest 仍然只返回最后一帧。
        with patch.object(module.time, 'monotonic', return_value=12.001):
            self.assertEqual(stream.recent_frames(), [])

    def test_recent_frames_snapshot_list_does_not_mutate_the_internal_buffer(self):
        """消费者可以清空或重排返回列表，内部帧序列及共享图像引用保持不变。"""
        picture = Image.new('RGB', (828, 148))
        wire = (struct.pack('>QI', 0, 1) + b'x') * 2
        frames = [[Mock(width=828, height=148, to_image=lambda: picture)] for _ in range(2)]
        with patch.object(module.time, 'monotonic', return_value=10):
            stream, _, _ = self.decode(wire, frames)
            snapshot = stream.recent_frames()
            snapshot.reverse()
            snapshot.clear()
            recent = stream.recent_frames()
        self.assertEqual([frame[0] for frame in recent], [1, 2])
        self.assertIs(recent[0][2], picture)

    def test_receive_preserves_partial_packet_across_timeout(self):
        stream = self.make_stream()
        stream._socket = Mock()
        stream._socket.recv.side_effect = [b'ab', socket.timeout(), b'cd']
        self.assertEqual(stream._receive(4), b'abcd')

    def test_latest_rejects_stale_frame_and_propagates_receiver_error(self):
        stream = module.ButtonStream(Path('/test/adb'), 'test')
        self.assertIsNone(stream.latest(timeout=0))
        image = Image.new('RGB', (828,148))
        stream._last = (4, 12.0, image)
        self.assertEqual(stream.latest(after_sequence=3, timeout=0), (4, 12.0, image))
        self.assertIsNone(stream.latest(after_sequence=4, timeout=0))
        stream.error = 'connection closed'
        with self.assertRaisesRegex(RuntimeError, 'connection closed'):
            stream.latest(timeout=0)

    def test_hash_mismatch_never_pushes_or_starts_server(self):
        stream = module.ButtonStream(Path('/test/adb'), 'test')
        with patch.object(Path, 'read_bytes', return_value=b'wrong'), patch.object(stream, '_adb') as adb:
            with self.assertRaisesRegex(RuntimeError, '校验失败'):
                stream.__enter__()
            adb.assert_not_called()

    def test_context_uses_read_only_options_and_only_removes_its_own_resources(self):
        """其他 scrcpy 进程和 forward 不受影响，不允许 --remove-all。"""
        stream = module.ButtonStream(Path('/test/adb'), 'test')
        commands = []

        def adb(*args):
            commands.append(args)
            if args[:2] == ('forward', 'tcp:0'):
                return '54321'
            if args[:2] == ('shell', 'ps'):
                return f'11 app_process / com.genymobile.scrcpy.Server scid={stream.scid}\n22 app_process / com.genymobile.scrcpy.Server scid=another'
            return ''

        process = Mock()
        with patch.object(Path, 'read_bytes', return_value=b'server'), \
                patch.object(module, 'SERVER_SHA256', hashlib.sha256(b'server').hexdigest()), \
                patch.object(stream, '_adb', side_effect=adb), \
                patch.object(module.subprocess, 'Popen', return_value=process) as spawn, \
                patch.object(module.threading, 'Thread'):
            with stream as entered:
                self.assertIs(entered, stream)
            stream.close()
        argv = spawn.call_args.args[0]
        for option in ('control=false', 'audio=false', 'power_on=false', 'crop=828:148:340:2428'):
            self.assertIn(option, argv)
        self.assertNotIn('input', argv)
        self.assertIn(('shell','kill','11'), commands)
        self.assertNotIn(('shell','kill','22'), commands)
        self.assertEqual(commands.count(('forward','--remove','tcp:54321')), 1)
        self.assertIn(('shell','rm','-f',stream.remote), commands)
        self.assertFalse(any('--remove-all' in command for command in commands))

    def test_start_failure_removes_forward_and_remote_file(self):
        stream = module.ButtonStream(Path('/test/adb'), 'test')
        with patch.object(Path, 'read_bytes', return_value=b'server'), \
                patch.object(module, 'SERVER_SHA256', hashlib.sha256(b'server').hexdigest()), \
                patch.object(stream, '_adb', side_effect=['', '54321', '', '']) as adb, \
                patch.object(module.subprocess, 'Popen', side_effect=OSError('start failed')):
            with self.assertRaisesRegex(OSError, 'start failed'):
                stream.__enter__()
        self.assertIn(('forward','--remove','tcp:54321'), [call.args for call in adb.call_args_list])
        self.assertIn(('shell','rm','-f',stream.remote), [call.args for call in adb.call_args_list])

    def test_start_failure_keeps_original_error_when_cleanup_also_fails(self):
        stream = module.ButtonStream(Path('/test/adb'), 'test')
        with patch.object(Path, 'read_bytes', return_value=b'server'), \
                patch.object(module, 'SERVER_SHA256', hashlib.sha256(b'server').hexdigest()), \
                patch.object(stream, '_adb', side_effect=[OSError('push failed'), OSError('rm failed')]):
            with self.assertRaisesRegex(OSError, 'push failed'):
                stream.__enter__()
        self.assertIn('rm failed', stream.error)

    def test_process_cleanup_failure_still_removes_forward_and_remote_file(self):
        stream = self.make_stream()
        stream._pushed = True
        stream._process.wait.side_effect = OSError('wait failed')
        with patch.object(stream, '_adb', side_effect=['', '', '']) as adb:
            with self.assertRaisesRegex(RuntimeError, 'wait failed'):
                stream.close()
        self.assertEqual(adb.call_args_list[-2].args, ('forward','--remove','tcp:54321'))
        self.assertEqual(adb.call_args_list[-1].args, ('shell','rm','-f',stream.remote))

    def test_cleanup_continues_after_forward_removal_failure(self):
        stream = module.ButtonStream(Path('/test/adb'), 'test')
        stream._pushed, stream._port = True, '54321'
        with patch.object(stream, '_adb', side_effect=[OSError('forward failed'), '']) as adb:
            with self.assertRaisesRegex(RuntimeError, '清理失败'):
                stream.close()
        self.assertEqual(adb.call_args_list[-1].args, ('shell','rm','-f',stream.remote))


if __name__ == '__main__':
    unittest.main()
