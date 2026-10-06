"""变化检测与事件分级。

先做字段级 diff，把「变了什么」说清楚；再做语义级判断，决定「多重要」。
分级规则可直接改，规则尽量显式写在代码里 —— 交给 LLM 判断会不可复现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.models import Severity

# 价格变动阈值
P0_DROP_RATIO = 0.10      # 降幅 >=10% 视为重大
P1_DROP_RATIO = 0.03      # 降幅 >=3% 需要当天处置
STOCK_CRASH_RATIO = 0.50  # 库存腰斩
RATING_DROP = 0.20

_TITLE_CORE_WORDS = ("包邮", "正品", "旗舰", "直播", "爆款", "新款", "直播同款", "限时")


@dataclass
class ChangeEvent:
    item_id: str
    source_id: str
    severity: str
    field_name: str
    old_value: object
    new_value: object
    event: str
    detected_at: int = field(default_factory=lambda: int(time.time()))

    def as_row(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "source_id": self.source_id,
            "severity": self.severity,
            "field": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "event": self.event,
            "detected_at": self.detected_at,
        }


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(old: float | None, new: float | None) -> float:
    if old is None or new is None or old == 0:
        return 0.0
    return (new - old) / abs(old)


def diff_snapshots(prev: dict | None, curr: dict | None, *, role: str = "competitor") -> list[ChangeEvent]:
    """比较两份快照，产出变化事件列表。首次采集（prev 为空）只产出一条 INIT，不刷告警。"""
    if prev is None or curr is None:
        return [
            ChangeEvent(
                item_id=(curr or prev or {}).get("item_id", "unknown"),
                source_id=(curr or prev or {}).get("source_id", ""),
                severity=Severity.P3.value,
                field_name="init",
                old_value=None,
                new_value=(curr or {}).get("listing_status", "unknown"),
                event="首次采集，建立基线",
            )
        ]

    events: list[ChangeEvent] = []
    item_id = curr.get("item_id", "")
    source_id = curr.get("source_id", "")

    # 1) 上下架 —— 永远 P0
    p_status, c_status = prev.get("listing_status"), curr.get("listing_status")
    if p_status != c_status:
        events.append(ChangeEvent(
            item_id, source_id, Severity.P0.value, "listing_status", p_status, c_status,
            "商品下架" if c_status != "on_sale" else "商品上架",
        ))

    # 2) 价格
    p_price, c_price = _num(prev.get("price")), _num(curr.get("price"))
    if p_price is not None and c_price is not None and p_price != c_price:
        ratio = _pct(p_price, c_price)
        sev = (
            Severity.P0.value if ratio <= -P0_DROP_RATIO
            else Severity.P1.value if ratio <= -P1_DROP_RATIO or ratio >= P0_DROP_RATIO
            else Severity.P2.value
        )
        events.append(ChangeEvent(
            item_id, source_id, sev, "price", p_price, c_price,
            f"价格变动 {ratio:+.1%}",
        ))

    # 3) 库存
    p_stock, c_stock = _num(prev.get("stock")), _num(curr.get("stock"))
    if p_stock is not None and c_stock is not None and p_stock != c_stock:
        ratio = _pct(p_stock, c_stock)
        if c_stock == 0:
            sev = Severity.P0.value          # 断货：竞品断货是机会，自有断货是事故
        elif ratio <= -STOCK_CRASH_RATIO:
            sev = Severity.P1.value
        else:
            sev = Severity.P3.value
        events.append(ChangeEvent(item_id, source_id, sev, "stock", int(p_stock), int(c_stock), "库存变动"))

    # 4) 主图 —— 视觉素材需求的主要来源
    if prev.get("main_image") != curr.get("main_image"):
        events.append(ChangeEvent(
            item_id, source_id, Severity.P1.value, "main_image",
            "旧主图", "新主图", "主图换版（需评估美工素材差距）",
        ))

    # 5) 标题 —— 核心卖点词变化才是重点
    p_title, c_title = prev.get("title") or "", curr.get("title") or ""
    if p_title != c_title:
        gained = [w for w in _TITLE_CORE_WORDS if w in c_title and w not in p_title]
        lost = [w for w in _TITLE_CORE_WORDS if w in p_title and w not in c_title]
        events.append(ChangeEvent(
            item_id, source_id, Severity.P2.value, "title", p_title[:60], c_title[:60],
            f"标题调整；新增卖点词 {gained or '无'}，丢失 {lost or '无'}",
        ))

    # 6) 促销机制
    if prev.get("promotion") != curr.get("promotion"):
        events.append(ChangeEvent(
            item_id, source_id, Severity.P1.value, "promotion",
            prev.get("promotion"), curr.get("promotion"), "促销机制变化",
        ))

    # 7) 评价与评分
    p_rev, c_rev = _num(prev.get("review_count")), _num(curr.get("review_count"))
    if p_rev is not None and c_rev is not None and c_rev > p_rev:
        events.append(ChangeEvent(
            item_id, source_id, Severity.P3.value, "review_count", int(p_rev), int(c_rev), "累计评价增长",
        ))
    p_rate, c_rate = _num(prev.get("rating")), _num(curr.get("rating"))
    if p_rate is not None and c_rate is not None and p_rate - c_rate >= RATING_DROP:
        events.append(ChangeEvent(
            item_id, source_id, Severity.P1.value, "rating", p_rate, c_rate, "评分下滑",
        ))

    # 竞品字段优先级过滤：非高优先级字段不进 P0/P1，避免噪音淹没
    if role != "competitor":
        events = [e for e in events if e.severity in (Severity.P0.value, Severity.P1.value)]
    return events


def grade(events: list[ChangeEvent]) -> str:
    """整批事件的最终等级，用于决定推送方式。"""
    if not events:
        return Severity.P3.value
    order = [Severity.P0.value, Severity.P1.value, Severity.P2.value, Severity.P3.value]
    return min((e.severity for e in events), key=order.index)
