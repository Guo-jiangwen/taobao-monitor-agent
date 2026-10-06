"""端到端链路测试：采集 → 落库 → diff → 事件分级。

用可控的假数据源跑两轮，验证「首轮建基线、次轮出 diff、无变化不出噪音」。
这些用例跑不过，说明监控闭环根本不成立。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collectors.base import BaseCollector, CollectorContext  # noqa: E402
from src.collectors.registry import register  # noqa: E402
from src.core.models import MonitorTarget, RawRecord  # noqa: E402
from src.pipeline import MonitorPipeline  # noqa: E402

_CONFIG = {
    "storage": {"backend": "sqlite", "path": "monitor.db", "retention_days": 30},
    "logging": {"level": "INFO", "audit_log": "audit.jsonl"},
    "scheduler": {"mode": "cron", "cron_expr": "17 * * * *", "interval_seconds": 3600},
    "risk": {
        "global": {"politeness_jitter_ms": [0, 0]},
        "defaults": {
            "qps": 50, "burst": 50, "daily_quota": 500, "dedupe_ttl_seconds": 0,
            "pause_after_breaker_opens": 3, "pause_after_consecutive_failures": 5,
            "breaker": {"failure_threshold": 3, "open_seconds": 60, "half_open_max_attempts": 1,
                        "max_open_seconds": 3600, "backoff_factor": 2.0},
        },
    },
}

_SOURCES = {
    "sources": [{
        "id": "fake", "adapter": "fake", "enabled": True, "display_name": "测试源",
        "compliance": "synthetic", "compliance_note": "仅测试环境使用", "qps": 50, "burst": 50,
        "daily_quota": 500,
    }],
    "targets": [{"item_id": "T-1", "title": "测试商品", "role": "competitor"}],
}

# 模块级可变状态，模拟「平台上商品被人改了」
STATE = {
    "title": "纯棉圆领T恤 夏季薄款",
    "price": 89.0,
    "stock": 420,
    "promotion": "满199减30",
    "main_image": "https://cdn.example.com/v1.jpg",
    "review_count": 100,
    "rating": 4.8,
    "listing_status": "on_sale",
}


class _FakeCollector(BaseCollector):
    """不连任何平台的假采集器。数据全由 STATE 决定，测试里直接改 STATE 就能造变化。"""

    adapter_name = "fake"

    def build_request(self, target):
        return f"https://fake.example/item/{target.item_id}"

    def parse(self, payload, target):
        return RawRecord(
            item_id=target.item_id, source_id=self.source.id,
            title=STATE["title"], price=STATE["price"], original_price=STATE["price"],
            stock=STATE["stock"], listing_status=STATE["listing_status"],
            main_image=STATE["main_image"], promotion=STATE["promotion"],
            review_count=STATE["review_count"], rating=STATE["rating"],
        )

    def _do_fetch(self, url):
        return {"ok": True}


register("fake", _FakeCollector)


class TestPipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        register("fake", _FakeCollector)
        STATE.update({
            "title": "纯棉圆领T恤 夏季薄款", "price": 89.0, "stock": 420,
            "promotion": "满199减30", "main_image": "https://cdn.example.com/v1.jpg",
            "review_count": 100, "rating": 4.8, "listing_status": "on_sale",
        })
        self.tmp = TemporaryDirectory()
        # 配置必须放在子目录：pipeline 把相对库路径解析为「配置目录的父级」，
        # 把配置直接扔在 tmp 根上会让所有测试共用同一个库，diff 基线互相污染。
        base = Path(self.tmp.name) / "config"
        base.mkdir(parents=True, exist_ok=True)
        (base / "config.json").write_text(__import__("json").dumps(_CONFIG), encoding="utf-8")
        (base / "sources.json").write_text(__import__("json").dumps(_SOURCES), encoding="utf-8")
        self.pipeline = MonitorPipeline(config_dir=base)
        self._pipelines = [self.pipeline]

    def _make_pipeline(self, base: Path) -> MonitorPipeline:
        p = MonitorPipeline(config_dir=base)
        self._pipelines.append(p)
        return p

    def tearDown(self):
        for p in self._pipelines:
            p.repo.close()
        self.tmp.cleanup()

    def test_first_cycle_builds_baseline_without_alerts(self):
        out = self.pipeline.collect_cycle()
        self.assertEqual(out["stats"]["fetched"], 1, "首轮必须真的取数")
        self.assertEqual(out["events"], [], "首轮只建基线，不许刷告警")
        self.assertEqual(out["top_severity"], "P3")
        self.assertIsNotNone(self.pipeline.repo.latest("T-1"), "基线必须落库")

    def test_second_cycle_reports_real_changes(self):
        self.pipeline.collect_cycle()
        # 竞品大幅降价 + 改标题 + 换促销 + 砍库存
        STATE.update({"price": 69.0, "title": "纯棉圆领T恤 夏季薄款 直播爆款新款",
                      "promotion": "限时券20", "stock": 100})
        out = self.pipeline.collect_cycle()
        fields = {e["field"] for e in out["events"]}
        for field in ("price", "title", "promotion", "stock"):
            self.assertIn(field, fields, f"变化字段 {field} 必须被检出")

        price_ev = next(e for e in out["events"] if e["field"] == "price")
        self.assertEqual(price_ev["old_value"], 89.0)
        self.assertEqual(price_ev["new_value"], 69.0)
        self.assertEqual(price_ev["severity"], "P0", "降幅 22.5% 必须判 P0")
        self.assertIn("22.5%", price_ev["event"])

        title_ev = next(e for e in out["events"] if e["field"] == "title")
        self.assertEqual(title_ev["severity"], "P2", "标题调整属于观察级")
        for word in ("直播", "爆款", "新款"):
            self.assertIn(word, title_ev["event"], f"新增卖点词 {word} 必须被识别")

        self.assertEqual(out["top_severity"], "P0")
        rows = self.pipeline.repo.recent_events(50)
        self.assertGreaterEqual(len(rows), len(out["events"]), "事件要落库")

    def test_unchanged_cycle_produces_no_noise(self):
        self.pipeline.collect_cycle()
        STATE["review_count"] = 150
        out = self.pipeline.collect_cycle()
        fields = {e["field"] for e in out["events"]}
        self.assertNotIn("price", fields)
        self.assertIn("review_count", fields, "评价自然增长要记为常规波动")

    def test_off_shelf_is_p0_for_competitor(self):
        self.pipeline.collect_cycle()
        STATE["listing_status"] = "off_shelf"
        out = self.pipeline.collect_cycle()
        ev = next(e for e in out["events"] if e["field"] == "listing_status")
        self.assertEqual(ev["severity"], "P0", "竞品下架是立刻要响应的信号")
        self.assertEqual(ev["event"], "商品下架")

    def test_role_filter_drops_low_priority_noise_for_owner(self):
        """竞品下架是情报（全量报），自有商品只报高优先级 —— 否则自有店铺会被标题微调刷爆。"""
        base = Path(self.tmp.name) / "config"
        base.mkdir(parents=True, exist_ok=True)
        (base / "sources.json").write_text(
            __import__("json").dumps({
                "sources": _SOURCES["sources"],
                "targets": [{"item_id": "T-1", "title": "测试商品", "role": "self"}],
            }), encoding="utf-8")
        own = self._make_pipeline(base)
        own.collect_cycle()
        STATE.update({"title": "纯棉圆领T恤 夏季薄款 直播爆款", "price": 89.0})
        out = own.collect_cycle()
        fields = {e["field"] for e in out["events"]}
        self.assertNotIn("title", fields, "自有商品的标题微调不该进事件流")
        self.assertNotIn("price", fields, "自有商品价格微调（<3%）不该进事件流")

        STATE["listing_status"] = "off_shelf"
        out = own.collect_cycle()
        ev = next(e for e in out["events"] if e["field"] == "listing_status")
        self.assertEqual(ev["severity"], "P0", "自有商品下架是事故，必须 P0")

    def test_dedupe_ttl_never_exceeds_half_interval(self):
        """去重 TTL 超过间隔一半会把定时刷新一起吃掉，监控就再也看不到变化。"""
        self.pipeline.cfg["risk"]["defaults"]["dedupe_ttl_seconds"] = 7200
        self.assertEqual(self.pipeline._dedupe_ttl(), 1800)
        self.pipeline.cfg["risk"]["defaults"]["dedupe_ttl_seconds"] = 600
        self.assertEqual(self.pipeline._dedupe_ttl(), 600)

    def test_compliance_declared_before_any_request(self):
        cfg = dict(_SOURCES["sources"][0])
        cfg["compliance"] = ""
        base = Path(self.tmp.name)
        (base / "config.json").write_text(__import__("json").dumps(_CONFIG), encoding="utf-8")
        (base / "sources.json").write_text(
            __import__("json").dumps({"sources": [cfg], "targets": _SOURCES["targets"]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self._make_pipeline(base)


if __name__ == "__main__":
    unittest.main()
