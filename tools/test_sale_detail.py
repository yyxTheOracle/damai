"""用脱敏的小米详情页字段回归固定启动入口，不连接设备。"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from sale_detail import check_sale_detail


NOW = datetime(2026, 11, 18, 10, 17)
TITLE = "合肥·“万物安生时”李健巡回演唱会-合肥站"


def detail(sale="11月18日 10:18开抢", prefill="已完成", extra=()):
    """保留原生详情字段的 ID 和 package，让通知、隐藏页和评论可单独构造。"""
    tree = ET.Element("hierarchy")
    fields = [("info_v2_title_tv1", TITLE), ("id_project_count_sell_time", sale),
              ("goto_setinfo_btn_text", prefill), *extra]
    for rid, text in fields:
        ET.SubElement(tree, "node", {"resource-id": f"cn.damai:id/{rid}", "package": "cn.damai", "text": text})
    return tree


def check(tree, show=(), at=None, now=NOW):
    return check_sale_detail(ET.tostring(tree, encoding="unicode"), list(show), at, now)


class SaleDetailTests(unittest.TestCase):
    def test_only_visible_damai_detail_controls_count(self):
        tree = detail(extra=[("tv_people_comment", "薛之谦无锡站")])
        notification = dict(tree[1].attrib, package="com.android.systemui", text="今天 18:30")
        ET.SubElement(tree, "node", notification)
        hidden = ET.SubElement(tree, "node", {"visible-to-user": "false"})
        ET.SubElement(hidden, "node", dict(tree[0].attrib, text="薛之谦"))
        self.assertEqual(check(tree, ["合肥", "李健"], "10：18"), (TITLE, datetime(2026, 11, 18, 10, 18)))
        with self.assertRaisesRegex(ValueError, "场次不匹配"):
            check(tree, ["薛之谦"])

    def test_expected_show_time_and_prefill_must_all_pass(self):
        cases = [
            (detail(), ["李健", "杭州"], None, "场次"),
            (detail(), [], "10:19", "时间不匹配"),
            (detail(), [], "2026-11-19 10:18:00", "时间不匹配"),
            (detail(prefill="去设置"), [], None, "未预填"),
            (detail(extra=[("goto_setinfo_btn_text", "已完成")]), [], None, "唯一"),
            (detail(extra=[("id_project_count_sell_time", "11月18日 10:19开抢")]), [], None, "唯一"),
            (detail(extra=[("info_v2_title_tv1", "另一场")]), [], None, "唯一"),
            (detail(sale="开抢"), [], None, "无法识别"),
            (detail(sale="11月31日10:18开抢"), [], None, "无效"),
        ]
        for tree, show, at, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                check(tree, show, at)
        tree = detail()
        tree.remove(tree[1])
        with self.assertRaisesRegex(ValueError, "开售时间"):
            check(tree)

    def test_quick_order_is_manual_even_when_prefill_complete(self):
        for field in [("id_project_ticket_quick_seat_tag", "一键下单，抢票省心"),
                      ("goto_setinfo_tv2", "已开启 一键下单，抢票时将看到一键下单按钮")]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "一键下单"):
                check(detail(extra=[field]))

    def test_no_expectations_and_new_year(self):
        self.assertEqual(check(detail("11月18日10:18准时开抢")), (TITLE, datetime(2026, 11, 18, 10, 18)))
        year_end = datetime(2026, 12, 31, 23, 59)
        tree = detail("1月1日 00:00:03开售")
        self.assertEqual(check(tree, at="2027-01-01 00:00:03", now=year_end)[1], datetime(2027, 1, 1, 0, 0, 3))
        with self.assertRaisesRegex(ValueError, "时间不匹配"):
            check(tree, at="00:00:03", now=year_end)


if __name__ == "__main__":
    unittest.main()
