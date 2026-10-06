"""脱敏样本源。

用途：在没有 AppKey 的环境里把整条链路跑通、跑测试、做演示。
内容全是本地生成的假数据，绝不连接任何真实商品页面。
真实数据源接入后，本适配器可以保留（CI 里跑），但默认在部署配置里关掉。
"""

from __future__ import annotations

import random
from typing import Any

from .base import BaseCollector
from ..core.models import RawRecord

_SEED_DATA = {
    "SAMPLE-001": {"title": "自有主推款 纯棉圆领T恤 夏季薄款", "price": 89.0, "stock": 420, "promotion": "满199减30"},
    "SAMPLE-002": {"title": "竞品A 重磅纯棉短袖T恤 宽松版型", "price": 79.0, "stock": 1200, "promotion": "第三件半价"},
    "SAMPLE-003": {"title": "竞品B 冰丝防晒衣 UPF50+ 男款", "price": 129.0, "stock": 860, "promotion": "限时券20"},
}


class SampleCollector(BaseCollector):
    adapter_name = "sample"

    def build_request(self, target: Any) -> str:
        return f"http://localhost/sample/{target.item_id}?fields=title,price,stock,promotion"

    def parse(self, payload: dict[str, Any], target: Any) -> RawRecord:
        base = _SEED_DATA.get(target.item_id, {"title": target.title, "price": None, "stock": None, "promotion": ""})
        # 引入随机波动，模拟真实集市上竞品调价/改标题；pipeline 连跑两次必然产生 diff
        drift = random.choice([0, 0, 0, -10.0, 5.0])
        price = base["price"]
        return RawRecord(
            item_id=target.item_id,
            source_id=self.source.id,
            title=base["title"],
            price=round(price * (1 + drift / 100), 2) if price else None,
            original_price=price,
            stock=base["stock"],
            listing_status="on_sale",
            main_image=f"https://cdn.example.com/{target.item_id}_v1.jpg",
            promotion=base["promotion"],
            review_count=random.choice([120, 340, 890]),
            rating=round(random.uniform(4.3, 4.9), 1),
            sku_list=[f"{target.item_id}-sku-1", f"{target.item_id}-sku-2"],
            raw={"_synthetic": True},
        )

    def _do_fetch(self, url: str) -> dict[str, Any]:
        return {"ok": True, "url": url}
