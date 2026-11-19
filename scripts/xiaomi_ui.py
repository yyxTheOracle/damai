"""从小米手机的当前界面 XML 识别购票状态和实测点击位置。"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from device_support import XIAOMI_15_MAIN_ACTION_POINT


PACKAGE = "cn.damai"
ALIPAY_PACKAGE = "com.eg.android.AlipayGphone"
DETAIL_RESOURCE = "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
SKU_BUY_RESOURCE = "cn.damai:id/btn_buy_view"
CONFIRM_RESOURCE = "cn.damai:id/damai_theme_dialog_confirm_btn"
TIP_RESOURCE = "cn.damai:id/damai_theme_dialog_tip_content"
SEAT_RESOURCE = "cn.damai:id/seat_confirm_order"


def has_frequency_verification(text: str) -> bool:
    """验证提示可能拆成两行或多个节点，必须同时读到完整的原因和购票限制。"""
    return "操作过于频繁完成验证后可继续购票" in re.sub(r"[\W_]+", "", text)


def _bounds(node: ET.Element, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """只接受完全位于当前屏幕内、具有面积的控件边界。"""
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if match is None:
        return None
    left, top, right, bottom = map(int, match.groups())
    if 0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]:
        return left, top, right, bottom
    return None


def _point(
    node: ET.Element,
    parents: dict[ET.Element, ET.Element],
    size: tuple[int, int],
    *,
    bottom_only: bool = False,
    detail_container: bool = False,
    fixed_main: bool = False,
) -> tuple[int, int] | None:
    """文字可能位于不可点击的子节点，向上找最近的启用按钮。"""
    candidate = node
    while candidate.tag == "node":
        if (
            candidate.get("package") != PACKAGE
            or candidate.get("enabled") != "true"
            or candidate.get("visible-to-user") == "false"
        ):
            return None
        box = _bounds(candidate, size)
        if box is None:
            return None
        if candidate.get("clickable") == "true" or detail_container:
            if bottom_only and box[1] < size[1] * 0.75:
                return None
            if fixed_main:
                # 主流程始终点固定位置；页面改版导致按钮移开时，暂停而不是误点。
                x, y = XIAOMI_15_MAIN_ACTION_POINT
                return (x, y) if box[0] <= x < box[2] and box[1] <= y < box[3] else None
            return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        if candidate not in parents:
            return None
        candidate = parents[candidate]
    return None


def _system_bar(node: ET.Element, size: tuple[int, int]) -> bool:
    """仅放行屏幕边缘的系统控件，兼容 HyperOS 网速栏左右边界倒置。"""
    if node.get("package") not in ("com.android.systemui", "miui.systemui.plugin", "com.miui.home"):
        return False
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if match is None:
        return False
    left, top, right, bottom = map(int, match.groups())
    return (
        0 <= left <= size[0] and 0 <= right <= size[0] and 0 <= top < bottom <= size[1]
        and (bottom <= size[1] * 0.1 or top >= size[1] * 0.9)
    )


def screen_size_matches_xml(xml_text: str, screen_size: tuple[int, int]) -> bool:
    """复用完整窗口的几何证据；False 也表示证据不足，需回退实际尺寸查询。"""
    root = ET.fromstring(xml_text)
    if root.tag != "hierarchy" or root.get("rotation") != "0":
        return False
    width, height = screen_size
    status_bar_matches = False
    window_matches = False
    # 只看顶层窗口；内容节点的边界不能代表屏幕。弹窗单独出现时没有高度证据。
    for node in root:
        if node.tag != "node" or node.get("visible-to-user") == "false":
            continue
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if match is None:
            continue
        left, top, right, bottom = map(int, match.groups())
        if node.get("package") == "com.android.systemui":
            status_bar_matches |= left == top == 0 and right == width and 0 < bottom <= height * 0.1
        elif node.get("package") == PACKAGE:
            # 横移动画会平移或裁剪应用窗口，用系统栏确认宽度、贴顶窗口确认底边。
            window_matches |= top == 0 and bottom == height and 0 < right - left <= width
    return status_bar_matches and window_matches


def parse_page(
    xml_text: str, screen_size: tuple[int, int]
) -> tuple[str, tuple[int, int] | None]:
    """主按钮返回小米固定点，重试和选座返回实测点；遮挡或位置不符时禁点。"""
    root = ET.fromstring(xml_text)
    # 等待购买文案时点击线程尚未查前台，后台一旦看到支付宝窗口也要结束。
    if any(window.get("package") == ALIPAY_PACKAGE and window.get("visible-to-user") != "false"
           for window in root):
        return "alipay_reached", None
    parents = {child: parent for parent in root.iter() for child in parent}
    visible = [node for node in root.iter("node") if node.get("visible-to-user") != "false"]
    nodes = [node for node in visible if node.get("package") == PACKAGE]
    if not nodes:
        return "damai_other", None
    # Android 的权限弹窗可能与底层大麦节点一起出现，不能透过弹窗点击。
    if any(
        node.get("package") not in (PACKAGE, None, "") and not _system_bar(node, screen_size)
        for node in visible
    ):
        return "unknown_dialog", None

    resources = {node.get("resource-id", "") for node in nodes}
    windows = [node for node in root if node.get("package") == PACKAGE
               and node.get("visible-to-user") != "false"]
    # water_mark_view_id 也出现在正常选座页，使用明确验证提示识别拦截页。
    page_text = " ".join(node.get("text", "") + node.get("content-desc", "") for node in nodes)
    if has_frequency_verification(page_text) or any(
        text in page_text for text in ("验证码", "滑动验证", "安全验证", "人机验证", "滑块", "请完成验证")
    ):
        return "risk_verification", None

    confirms = [node for node in nodes if node.get("resource-id") == CONFIRM_RESOURCE]
    dialog_text = " ".join(
        child.get("text", "")
        for node in nodes
        if node.get("resource-id") in (CONFIRM_RESOURCE, TIP_RESOURCE)
        for child in node.iter("node")
    )
    if any(word in dialog_text for word in ("库存不足", "售罄", "返回", "重新选购")):
        return "sold_out_dialog", None
    if confirms:
        retry = [
            child for node in confirms for child in node.iter("node")
            if child.get("text", "").strip() == "继续尝试"
        ]
        if len(retry) != 1:
            return "unknown_dialog", None
        point = _point(retry[0], parents, screen_size)
        return ("retry_dialog", point) if point is not None else ("unknown_dialog", None)
    if "cn.damai:id/damai_theme_dialog_cancel_btn" in resources:
        return "terminal_dialog", None
    # 提交中的进度层属于正常等待；只豁免本次实测的两个 ID 和精确文案。
    dialog_resources = {resource for resource in resources if "dialog" in resource.lower()}
    progress_windows = [window for window in windows if any(
        node.get("resource-id") == "cn.damai:id/progress_dialog_layout_root"
        for node in window.iter("node")
    )]
    submitting = (
        dialog_resources == {"cn.damai:id/progress_dialog_layout_root", "cn.damai:id/tv_progress_dialog_label"}
        and "cn.damai:id/order_activity_title" in resources
        and len(windows) == 2 and len(progress_windows) == 1
        and all(node.get("clickable") != "true"
                and node.get("text", "").strip() in ("", "尝试提交中")
                and not node.get("content-desc", "").strip()
                for node in progress_windows[0].iter("node"))
        and any(node.get("resource-id") == "cn.damai:id/tv_progress_dialog_label"
                and node.get("text", "").strip() == "尝试提交中" for node in nodes)
    )
    if dialog_resources and not submitting:
        return "unknown_dialog", None

    # 单独标记拥挤页，让状态机按刷新间隔点击实测位置。
    refresh = [node for node in nodes if node.get("resource-id") == "cn.damai:id/state_view_btn"]
    if refresh:
        if len(refresh) != 1 or refresh[0].get("text", "").strip() != "努力刷新":
            return "unknown_dialog", None
        point = _point(refresh[0], parents, screen_size)
        return ("refresh_page", point) if point is not None else ("unknown_dialog", None)

    seat_nodes = [node for node in nodes if node.get("resource-id") == SEAT_RESOURCE]
    if seat_nodes:
        seats = [
            child for node in seat_nodes for child in node.iter("node")
            if child.get("text", "").strip() in ("确认选座", "确认选座并支付")
        ]
        if len(seats) != 1:
            return "seat_page", None
        point = _point(seats[0], parents, screen_size, bottom_only=True)
        return "seat_page", point

    if submitting:
        return "order_page_loading", None

    submits = [node for node in nodes if node.get("text", "").strip() == "立即提交"]
    if submits:
        if len(submits) != 1:
            return "unknown_dialog", None
        point = _point(submits[0], parents, screen_size, bottom_only=True, fixed_main=True)
        return ("order_page", point) if point is not None else ("unknown_dialog", None)
    if "cn.damai:id/order_activity_title" in resources:
        return "order_page_loading", None

    # 实机「确定」是自绘文字；数量和总价用于区分同 ID 的「已预约」按钮。
    sku_buttons = [node for node in nodes if node.get("resource-id") == SKU_BUY_RESOURCE]
    if sku_buttons:
        sale_controls = {"cn.damai:id/id_sku_bottom_bar_container", "cn.damai:id/tv_num", "cn.damai:id/tv_price"}
        if len(sku_buttons) != 1 or not sale_controls <= resources or sku_buttons[0].get("clickable") != "true":
            return "damai_other", None
        point = _point(sku_buttons[0], parents, screen_size, bottom_only=True, fixed_main=True)
        return "damai_other", point

    # 票档面板优先于底层详情，按钮不可用时不能回退去点被遮住的购买入口。
    confirms = [node for node in nodes if node.get("text", "").strip() in ("确定", "确认")]
    if confirms:
        points = [_point(node, parents, screen_size, bottom_only=True, fixed_main=True) for node in confirms]
        points = [point for point in points if point is not None]
        return "damai_other", points[0] if len(points) == 1 else None

    # 详情页购票栏实测 clickable=false，固定资源 ID 是该容器的识别依据。
    details = [node for node in nodes if node.get("resource-id") == DETAIL_RESOURCE]
    if details:
        if len(details) != 1:
            return "damai_other", None
        point = _point(details[0], parents, screen_size, bottom_only=True, detail_container=True, fixed_main=True)
        return "damai_other", point

    # 只标记明确的详情骨架/空全屏 Layout；由调用方核对此前是否正在正常购票。
    # 禁用 SKU、移出屏幕的按钮已在上面返回，不能借此恢复主连点。
    skeleton = (
        {"cn.damai:id/scheme_project_detail_flyt", "cn.damai:id/id_new_skeleton"} <= resources
        and len(windows) == 1
        and (box := _bounds(windows[0], screen_size)) == (0, 0, *screen_size)
        and all(
            not node.get("content-desc", "").strip()
            and (node.get("text", "").strip() in ("", "帮助")
                 or node.get("resource-id") == "cn.damai:id/project_item_bottom_customer_service_iconf")
            and (node.get("clickable") != "true"
                 or node.get("resource-id") == "cn.damai:id/project_item_bottom_customer_service_lv")
            for node in nodes
        )
    )
    empty_window = len(windows) == 1 and all(
        node.get("class") in ("android.widget.FrameLayout", "android.widget.LinearLayout")
        and not any(node.get(key, "").strip() for key in ("resource-id", "text", "content-desc"))
        and node.get("clickable") == "false"
        and (box := _bounds(node, screen_size)) is not None
        and box[1] == 0 and box[3] == screen_size[1]
        and box[0] <= XIAOMI_15_MAIN_ACTION_POINT[0] < box[2]
        for node in nodes
    )
    if skeleton or empty_window:
        return "purchase_transition", None
    return "damai_other", None
