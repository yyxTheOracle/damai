"""离线执行详情页启动入口；设备、输入通道和实战线程的工作函数用模拟对象替代。"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import damai_mode0_K80 as app


class DetailLaunchTests(unittest.TestCase):
    def setUp(self):
        """固定一份足以触发开场留证的详情页，所有测试均无真实 ADB 连接。"""
        self.sale = (datetime.now() + timedelta(minutes=5)).replace(second=0, microsecond=0)
        self.xml = f'''<hierarchy><node package="cn.damai">
            <node package="cn.damai" resource-id="cn.damai:id/info_v2_title_tv1" text="合肥·李健巡回演唱会"/>
            <node package="cn.damai" resource-id="cn.damai:id/id_project_count_sell_time" text="{self.sale:%m月%d日 %H:%M}开抢"/>
            <node package="cn.damai" resource-id="cn.damai:id/goto_setinfo_btn_text" text="已完成"/>
        </node></hierarchy>'''
        self.device = Mock()
        self.device.dump_hierarchy.return_value = self.xml
        self.argv = ["damai", "--from-detail", "--show", "李健", "--show", "合肥",
                     "--at", self.sale.strftime("%Y-%m-%d %H:%M")]
        self.stack = self.enterContext(ExitStack())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.stack.enter_context(patch.object(app, "ARTIFACTS_DIR", self.root))
        self.stack.enter_context(patch.object(app, "DEVICE_SERIAL", ""))
        self.stack.enter_context(patch.object(app, "resolve_adb", return_value=Path("adb")))
        self.serial = self.stack.enter_context(patch.object(app, "resolve_serial", return_value="phone"))
        self.stack.enter_context(patch.object(app.u2, "connect", return_value=self.device))

    def test_one_read_starts_formal_run_and_supplies_capture_and_button_worker(self):
        """启动前只抓一次页；开场日志和按钮线程拿到的都是已核对的原始 XML。"""
        with patch.object(sys, "argv", self.argv), patch.object(
            app, "validate_preflight", return_value=app.XIAOMI_15_MODEL
        ), patch.object(app, "PersistentQueryShell"), patch.object(app, "ScopedInput") as scoped_input, \
                patch.object(app, "tap_loop"), patch("button_ready.watch_purchase_button") as watch, \
                patch.object(app, "run_tail_probe_loop",
                             side_effect=lambda **_: self.device.dump_hierarchy.assert_called_once()):
            app.main()
        self.serial.assert_called_once()
        scoped_input.assert_called_once_with(Path("adb"), "phone")
        self.assertEqual(watch.call_args.kwargs["initial_xml"], self.xml)
        directory, = self.root.glob("run_*")
        events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
        check, = [e for e in events if e["event"] == "sale_detail_confirmed"]
        self.assertEqual((check["title"], check["sale_time"]), ("合肥·李健巡回演唱会", self.sale.isoformat()))
        initial, = [e for e in events if e.get("label") == "initial"]
        self.assertEqual((directory / initial["xml_path"]).read_text(), self.xml)

    def test_mismatch_stops_before_run_and_recording(self):
        """艺人/时间/预填任一不符都应在正式程序和输入连接创建之前退出。"""
        for xml in (self.xml.replace("李健", "其他艺人"), self.xml.replace("已完成", "去填写"),
                    self.xml.replace(self.sale.strftime("%H:%M"), (self.sale + timedelta(minutes=1)).strftime("%H:%M"))):
            with self.subTest(xml=xml), patch.object(sys, "argv", self.argv), patch.object(app, "run") as run:
                self.device.dump_hierarchy.reset_mock()
                self.device.dump_hierarchy.return_value = xml
                with self.assertRaisesRegex(SystemExit, "未启动"):
                    app.main()
                self.device.dump_hierarchy.assert_called_once()
                run.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_conflicting_cli_modes_exit_before_usb(self):
        """传两种启动方式或给旧入口混入核对参数时在解析阶段报错。"""
        absolute = self.sale.strftime("%Y-%m-%d %H:%M:%S")
        for args in (["--from-detail", absolute], [absolute, "--show", "李健"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["damai", *args]), self.assertRaises(SystemExit):
                app.main()
            self.serial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
