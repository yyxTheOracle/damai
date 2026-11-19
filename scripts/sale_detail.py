"""用一次大麦详情页快照核对本次实战的场次、开售时间和预填状态。"""
from __future__ import annotations

import re
from datetime import datetime
from unicodedata import normalize
from xml.etree import ElementTree as ET


def _compact(text: str) -> str:
    """大麦标题和时间的空格、全角冒号不影响场次核对。"""
    return "".join(normalize("NFKC", text).split()).casefold()


def _page_time(text: str, now: datetime) -> datetime:
    """倒计时只写月日，取离当前日期最近的年份，跨年时不把一月错算成去年。"""
    match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{2})(?::(\d{2}))?(?:准时)?(?:开抢|开售)", _compact(text))
    if not match:
        raise ValueError(f"无法识别开售时间「{text}」")
    month, day, hour, minute, second = (int(v or 0) for v in match.groups())
    dates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            dates.append(datetime(year, month, day, hour, minute, second, tzinfo=now.tzinfo))
        except ValueError:
            continue  # 例如 2 月 29 日，在非闰年没有这一天。
    if not dates:
        raise ValueError(f"开售日期或时间无效「{text}」")
    return min(dates, key=lambda dt: abs(dt - now))


def check_sale_detail(xml: str, show: list[str], at: str | None, now: datetime) -> tuple[str, datetime]:
    """仅核对同一份 XML 中大麦自身的详情控件，返回标题和实际开售时间。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("无法读取大麦详情页 XML") from exc

    fields: dict[str, list[str]] = {}
    # 剪去不可见的整棵子树，隐藏页和系统通知均不能为当前详情提供证据。
    pending = [root]
    while pending:
        node = pending.pop()
        if node.get("visible-to-user") == "false":
            continue
        pending.extend(node)
        if node.get("package") == "cn.damai":
            rid = node.get("resource-id", "")
            fields.setdefault(rid, []).append((node.get("text") or node.get("content-desc") or "").strip())

    def single(rid: str, name: str) -> str:
        """缺少、空白或重复的字段不能作为核对成功的证据。"""
        values = fields.get(f"cn.damai:id/{rid}", [])
        if len(values) != 1 or not values[0]:
            raise ValueError(f"详情页未显示唯一的{name}")
        return values[0]

    title = single("info_v2_title_tv1", "演出标题")
    for term in show:
        word = _compact(term)
        if not word or word not in _compact(title):
            raise ValueError(f"场次不匹配：期望「{term}」，当前「{title}」")

    sale = _page_time(single("id_project_count_sell_time", "开售时间"), now)
    if at is not None:
        expected_text = normalize("NFKC", at).strip()
        if not re.fullmatch(r"(?:\d{4}-\d{2}-\d{2}\s+)?\d{1,2}:\d{2}(?::\d{2})?", expected_text):
            raise ValueError(f"期望时间格式无效「{at}」")
        if "-" not in expected_text:
            expected_text = f"{now:%Y-%m-%d} {expected_text}"
        fmt = "%Y-%m-%d %H:%M:%S" if expected_text.count(":") == 2 else "%Y-%m-%d %H:%M"
        expected = datetime.strptime(expected_text, fmt).replace(tzinfo=now.tzinfo)
        if sale != expected:
            raise ValueError(f"开售时间不匹配：期望 {expected}，当前 {sale}")

    if single("goto_setinfo_btn_text", "预填状态") != "已完成":
        raise ValueError("本场购票信息尚未预填完成")
    quick = fields.get("cn.damai:id/id_project_ticket_quick_seat_tag", [])
    desc = fields.get("cn.damai:id/goto_setinfo_tv2", [])
    if any("一键下单" in t for t in quick) or any("已开启一键下单" in _compact(t) for t in desc):
        raise ValueError("本场已开启一键下单，按约定由你手动操作")
    return title, sale
