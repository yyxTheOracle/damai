"""用最小界面快照验证小米 15 的按钮定位，不连接手机。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from xiaomi_ui import parse_page, screen_size_matches_xml, DETAIL_RESOURCE, CONFIRM_RESOURCE, SEAT_RESOURCE  # noqa: E402
from device_support import XIAOMI_15_MAIN_ACTION_POINT


SIZE = (1200, 2670)


def node(text: str = "", *, children: tuple[ET.Element, ...] = (), **attributes: str) -> ET.Element:
    """构造 Android dump_hierarchy 所使用的控件节点。"""
    result = ET.Element("node", {
        "package": "cn.damai", "text": text, "enabled": "true", "clickable": "true",
        "bounds": "[341,2429][1168,2576]", **attributes,
    })
    result.extend(children)
    return result


def parse(*nodes: ET.Element) -> tuple[str, tuple[int, int] | None]:
    """把测试控件组合成一帧独立界面。"""
    root = ET.Element("hierarchy")
    root.extend(nodes)
    return parse_page(ET.tostring(root, encoding="unicode"), SIZE)


class XiaomiUiTests(unittest.TestCase):
    def test_screen_geometry_uses_status_bar_and_top_level_window(self) -> None:
        """横移动画改变窗口宽度和位置，但状态栏宽度与窗口底边仍可复用。"""
        for bounds in ("[0,0][1200,2670]", "[0,0][1042,2670]",
                       "[3,0][1200,2670]", "[1200,0][2400,2670]"):
            with self.subTest(bounds=bounds):
                root = ET.Element("hierarchy", rotation="0")
                root.extend((node(bounds=bounds),
                             node(package="com.android.systemui", bounds="[0,0][1200,140]")))
                self.assertTrue(screen_size_matches_xml(ET.tostring(root, encoding="unicode"), SIZE))

    def test_screen_geometry_requires_complete_matching_evidence(self) -> None:
        """尺寸变化、旋转和仅弹窗等情况必须回退真实尺寸检查，不能猜测屏幕。"""
        cases = (
            ("width_changed", "0", "[0,0][1080,2670]", "[0,0][1080,140]", "cn.damai"),
            ("height_changed", "0", "[0,0][1200,2400]", "[0,0][1200,140]", "cn.damai"),
            ("rotated", "1", "[0,0][1200,2670]", "[0,0][1200,140]", "cn.damai"),
            ("rotation_missing", None, "[0,0][1200,2670]", "[0,0][1200,140]", "cn.damai"),
            ("dialog_only", "0", "[0,769][1200,1975]", "[0,0][1200,140]", "cn.damai"),
            ("status_bar_missing", "0", "[0,0][1200,2670]", None, "cn.damai"),
            ("other_app", "0", "[0,0][1200,2670]", "[0,0][1200,140]", "com.eg.android.AlipayGphone"),
            ("invalid_bounds", "0", "invalid", "[0,0][1200,140]", "cn.damai"),
        )
        for name, rotation, app_bounds, status_bounds, package in cases:
            with self.subTest(case=name):
                root = ET.Element("hierarchy", {} if rotation is None else {"rotation": rotation})
                root.append(node(package=package, bounds=app_bounds))
                if status_bounds is not None:
                    root.append(node(package="com.android.systemui", bounds=status_bounds))
                self.assertFalse(screen_size_matches_xml(ET.tostring(root, encoding="unicode"), SIZE))

    def test_detail_uses_fixed_point_despite_clickable_false(self) -> None:
        self.assertEqual(parse(node(clickable="false", **{"resource-id": DETAIL_RESOURCE})),
                         ("damai_other", XIAOMI_15_MAIN_ACTION_POINT))

    def test_submit_uses_nearest_clickable_parent(self) -> None:
        text = node("立即提交", clickable="false", bounds="[750,2470][1000,2520]")
        self.assertEqual(parse(node(children=(text,))), ("order_page", XIAOMI_15_MAIN_ACTION_POINT))

    def test_sku_confirm_uses_clickable_parent(self) -> None:
        for text in ("确定", "确认"):
            with self.subTest(text=text):
                self.assertEqual(parse(node(children=(node(text, clickable="false"),))),
                                 ("damai_other", XIAOMI_15_MAIN_ACTION_POINT))
                self.assertEqual(parse(node(clickable="false", **{"resource-id": DETAIL_RESOURCE}),
                                       node(text, enabled="false")), ("damai_other", None))

    def test_native_sku_requires_sale_summary_and_usable_button(self) -> None:
        """自绘确认按钮无文字，预约页和被遮挡的详情按钮都不能作为购票确认。"""
        cases = (
            ("sale", True, False, {}, XIAOMI_15_MAIN_ACTION_POINT),
            ("reservation", False, True, {}, None),
            ("disabled", True, True, {"enabled": "false"}, None),
            ("unclickable", True, True, {"clickable": "false"}, None),
            ("moved", True, True, {"bounds": "[0,2436][500,2579]"}, None),
        )
        for name, has_summary, has_detail, attributes, expected in cases:
            with self.subTest(page=name):
                controls = [node(bounds="[736,2436][1132,2579]",
                                 **{"resource-id": "cn.damai:id/btn_buy_view"})]
                controls[0].attrib.update(attributes)
                if has_summary:
                    controls.append(node(clickable="false", children=(
                        node("1张", **{"resource-id": "cn.damai:id/tv_num"}),
                        node("¥", **{"resource-id": "cn.damai:id/tv_sku_price_prefix"}),
                        node("588", **{"resource-id": "cn.damai:id/tv_price"}),
                    ), **{"resource-id": "cn.damai:id/id_sku_bottom_bar_container"}))
                if has_detail:
                    controls.append(node(clickable="false", **{"resource-id": DETAIL_RESOURCE}))
                self.assertEqual(parse(*controls), ("damai_other", expected))

    def test_submit_rejects_disabled_outside_or_non_bottom_controls(self) -> None:
        for attributes in (
            {"enabled": "false"}, {"bounds": "[341,2429][1201,2576]"},
            {"bounds": "[0,10][100,50]"}, {"bounds": "[5,2400][5,2500]"},
            {"bounds": "invalid"}, {"clickable": "false"},
        ):
            with self.subTest(attributes=attributes):
                self.assertEqual(parse(node("立即提交", **attributes)), ("unknown_dialog", None))

    def test_fixed_point_must_stay_inside_each_main_button(self) -> None:
        """按钮位置变化时不改点其他坐标，直接暂停，防止写死位置后误点。"""
        for text, resource in (("立即提交", ""), ("确认", ""), ("", DETAIL_RESOURCE)):
            with self.subTest(text=text, resource=resource):
                self.assertIsNone(parse(node(text, bounds="[0,2429][500,2576]",
                                              **{"resource-id": resource}))[1])

    def test_disabled_parent_prevents_child_text_click(self) -> None:
        self.assertEqual(parse(node(enabled="false", children=(node("立即提交", clickable="false"),))),
                         ("unknown_dialog", None))

    def test_ambiguous_controls_have_no_point(self) -> None:
        for text, resource in (("立即提交", ""), ("确认", ""), ("", DETAIL_RESOURCE)):
            with self.subTest(text=text, resource=resource):
                result = parse(node(text, **{"resource-id": resource}), node(text, **{"resource-id": resource}))
                self.assertIsNone(result[1])

    def test_unknown_dialog_blocks_underlying_submit_and_detail(self) -> None:
        popup = node("我知道了", **{"resource-id": CONFIRM_RESOURCE})
        self.assertEqual(parse(node("立即提交"), node(**{"resource-id": DETAIL_RESOURCE}), popup),
                         ("unknown_dialog", None))

    def test_generic_dialog_blocks_underlying_confirm(self) -> None:
        self.assertEqual(parse(node("确认"), node(**{"resource-id": "cn.damai:id/new_dialog"})),
                         ("unknown_dialog", None))

    def test_sold_out_has_precedence_over_underlying_submit(self) -> None:
        popup = node("返回重新选购", **{"resource-id": CONFIRM_RESOURCE})
        self.assertEqual(parse(node("立即提交"), popup), ("sold_out_dialog", None))

    def test_retry_requires_exact_text_and_current_bounds(self) -> None:
        for text, expected in (("继续尝试", ("retry_dialog", (754, 2502))),
                               ("继续尝试一下", ("unknown_dialog", None))):
            with self.subTest(text=text):
                self.assertEqual(parse(node(text, **{"resource-id": CONFIRM_RESOURCE})), expected)
        self.assertEqual(parse(node("继续尝试", enabled="false", **{"resource-id": CONFIRM_RESOURCE})),
                         ("unknown_dialog", None))
        self.assertEqual(parse(node("继续尝试", bounds="[341,1600][1168,1800]", **{"resource-id": CONFIRM_RESOURCE})),
                         ("retry_dialog", (754, 1700)))

    def test_refresh_uses_current_center_before_underlying_submit(self) -> None:
        """拥挤页刷新优先于底层提交，位置随实际按钮移动，不能点右下角。"""
        for bounds, center in (("[400,1813][801,1959]", (600, 1886)),
                               ("[300,1700][700,1900]", (500, 1800))):
            with self.subTest(bounds=bounds):
                refresh = node("努力刷新", bounds=bounds,
                               **{"resource-id": "cn.damai:id/state_view_btn"})
                self.assertEqual(parse(node("立即提交"), refresh), ("refresh_page", center))

    def test_refresh_rejects_unknown_disabled_ambiguous_or_outside_buttons(self) -> None:
        """错误刷新控件不能回退到底层提交；未验证的文案和坐标都要暂停。"""
        for attributes in ({"text": "刷新"}, {"text": "努力刷新一下"},
                           {"enabled": "false"}, {"clickable": "false"},
                           {"bounds": "[400,1813][1201,1959]"}, {"bounds": "invalid"}):
            with self.subTest(attributes=attributes):
                refresh = node("努力刷新", **{"resource-id": "cn.damai:id/state_view_btn"})
                refresh.attrib.update(attributes)
                self.assertEqual(parse(node("立即提交"), refresh), ("unknown_dialog", None))
        refresh = node("努力刷新", **{"resource-id": "cn.damai:id/state_view_btn"})
        self.assertEqual(parse(node("立即提交"), refresh, refresh), ("unknown_dialog", None))

    def test_blocking_dialog_and_verification_take_priority_over_refresh(self) -> None:
        """刷新页上再出现库存不足、未知弹窗或验证提示时，不能继续点刷新。"""
        for blocker, expected in (
            (node("返回重新选购", **{"resource-id": CONFIRM_RESOURCE}), "sold_out_dialog"),
            (node("我知道了", **{"resource-id": CONFIRM_RESOURCE}), "unknown_dialog"),
            (node("请完成安全验证"), "risk_verification"),
        ):
            with self.subTest(expected=expected):
                refresh = node("努力刷新", **{"resource-id": "cn.damai:id/state_view_btn"})
                self.assertEqual(parse(node("立即提交"), refresh, blocker), (expected, None))

    def test_risk_blocks_underlying_submit(self) -> None:
        self.assertEqual(parse(node("立即提交"), node("请完成安全验证")),
                         ("risk_verification", None))

    def test_frequency_verification_across_visible_nodes_blocks_retry(self) -> None:
        """两行确切提示可分别来自 text 和 content-desc，单独的频繁提示不能停机。"""
        retry = node("继续尝试", **{"resource-id": CONFIRM_RESOURCE})
        first = node("操作 过于频繁，")
        second = node(**{"content-desc": "完成验证后\n可继续购票。"})
        self.assertEqual(parse(retry, first, second), ("risk_verification", None))
        for partial in (first, second, node("操作过于频繁，请稍候再试")):
            with self.subTest(text=partial.attrib):
                self.assertEqual(parse(retry, partial), ("retry_dialog", (754, 2502)))

    def test_plain_watermark_is_not_a_risk_prompt(self) -> None:
        self.assertEqual(parse(node("立即提交"), node(**{"resource-id": "cn.damai:id/water_mark_view_id"})),
                         ("order_page", XIAOMI_15_MAIN_ACTION_POINT))

    def test_permission_prompt_blocks_underlying_damai(self) -> None:
        self.assertEqual(parse(node("立即提交"), node("允许", package="com.android.permissioncontroller")),
                         ("unknown_dialog", None))

    def test_other_app_cannot_supply_a_button(self) -> None:
        self.assertEqual(parse(node("立即提交", package="com.android.chrome")), ("damai_other", None))

    def test_only_visible_alipay_window_stops_input(self) -> None:
        cashier = node("立即提交", package="com.eg.android.AlipayGphone")
        self.assertEqual(parse(cashier), ("alipay_reached", None))
        self.assertEqual(parse(node("立即提交"), cashier), ("alipay_reached", None))
        cashier.set("visible-to-user", "false")
        self.assertEqual(parse(cashier), ("damai_other", None))

    def test_status_bar_does_not_hide_damai(self) -> None:
        for package, bounds in (("com.android.systemui", "[0,0][1200,100]"),
                                ("miui.systemui.plugin", "[300,0][900,100]"),
                                ("com.miui.home", "[0,2600][1200,2670]")):
            with self.subTest(package=package):
                self.assertEqual(parse(node("立即提交"), node(package=package, bounds=bounds)),
                                 ("order_page", XIAOMI_15_MAIN_ACTION_POINT))

    def test_expanded_system_overlay_blocks_damai(self) -> None:
        self.assertEqual(parse(node("立即提交"), node(package="com.android.systemui", bounds="[0,0][1200,2670]")),
                         ("unknown_dialog", None))

    def test_inverted_network_speed_bounds_only_allowed_at_screen_edge(self) -> None:
        """HyperOS 隐藏网速文字会导出横向倒置范围，但不能因此放行屏幕中部遮挡。"""
        for bounds, expected in (
            ("[638,40][592,105]", ("order_page", XIAOMI_15_MAIN_ACTION_POINT)),
            ("[638,1000][592,1065]", ("unknown_dialog", None)),
        ):
            with self.subTest(bounds=bounds):
                overlay = node(package="com.android.systemui", bounds=bounds,
                               **{"resource-id": "com.android.systemui:id/fullscreen_network_speed_view"})
                self.assertEqual(parse(node("立即提交"), overlay), expected)

    def test_tracked_phone_samples(self) -> None:
        samples = Path(__file__).resolve().parent.parent / "docs" / "samples"
        sold_out = (samples / "sold_out_dialog_sample.xml").read_text(encoding="utf-8")
        seat = (samples / "seat_page_sample.xml").read_text(encoding="utf-8")
        self.assertEqual(parse_page(sold_out, (1440, 3200)), ("sold_out_dialog", None))
        self.assertEqual(parse_page(seat, (1440, 3200)), ("seat_page", (1084, 3031)))

    def test_seat_requires_exact_text(self) -> None:
        for text in ("确认选座", "确认选座并支付"):
            self.assertEqual(parse(node(text, **{"resource-id": SEAT_RESOURCE})), ("seat_page", (754, 2502)))
        self.assertEqual(parse(node("选好了", **{"resource-id": SEAT_RESOURCE})), ("seat_page", None))

    def test_missing_buttons_clear_point(self) -> None:
        self.assertEqual(parse(node("立即提交"))[1], XIAOMI_15_MAIN_ACTION_POINT)
        self.assertEqual(parse(node("页面加载中")), ("damai_other", None))

    def test_terminal_dialog_and_loading_title(self) -> None:
        self.assertEqual(parse(node(**{"resource-id": "cn.damai:id/damai_theme_dialog_cancel_btn"})),
                         ("terminal_dialog", None))
        self.assertEqual(parse(node(**{"resource-id": "cn.damai:id/order_activity_title"})),
                         ("order_page_loading", None))


if __name__ == "__main__":
    unittest.main()
