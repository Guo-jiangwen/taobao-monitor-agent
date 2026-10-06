"""采集 → 落库 → diff → 事件分级 的编排。

第 2 步接入 Dify 时，只需要把 diff 之后的「建议生成」换成 Dify 工作流 API 调用
（而不是本地 LLM 节点），这个文件其余部分不用动。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .analysis.diff import ChangeEvent, diff_snapshots, grade
from .collectors.base import CollectorContext
from .collectors.registry import build
from .core.fingerprint import content_fingerprint
from .core.models import MonitorTarget, RawRecord, Snapshot, SourceConfig
from .core.risk import RiskGovernor
from .store.repository import SnapshotRepository

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_SYNTHETIC_PREFIX = "SAMPLE"


def snapshot_from_record(rec: RawRecord, fp: str) -> Snapshot:
    return Snapshot(
        item_id=rec.item_id,
        source_id=rec.source_id,
        title=rec.title,
        price=rec.price,
        stock=rec.stock,
        listing_status=rec.listing_status,
        main_image=rec.main_image,
        promotion=rec.promotion,
        review_count=rec.review_count,
        rating=rec.rating,
        content_fp=fp,
        payload=json.dumps(rec.to_dict(), ensure_ascii=False),
        fetched_at=int(time.time()),
    )


class MonitorPipeline:
    def __init__(self, config_dir: str | Path | None = None, dedupe_ttl: int | None = None):
        base = Path(config_dir) if config_dir else CONFIG_DIR
        self.cfg = json.loads((base / "config.json").read_text(encoding="utf-8"))
        self.src_cfg = json.loads((base / "sources.json").read_text(encoding="utf-8"))
        # 调试用：None 表示按配置走；给 0 表示关掉去重缓存，强制每轮真取一次
        self._dedupe_ttl_override = dedupe_ttl

        storage = self.cfg.get("storage", {})
        self.repo = SnapshotRepository(str(base.parent / storage.get("path", "data/monitor.db")))
        self.governor = RiskGovernor(self.cfg.get("risk", {}))
        self.targets = [MonitorTarget(**t) for t in self.src_cfg.get("targets", [])]
        self.sources = {s["id"]: s for s in self.src_cfg.get("sources", [])}
        self._register_sources()
        self.stats: dict[str, int] = {"fetched": 0, "cached": 0, "skipped": 0, "saved": 0, "events": 0}

    def _register_sources(self) -> None:
        for raw in self.sources.values():
            src = SourceConfig(**raw)
            if not src.enabled:
                continue
            if not src.compliance:
                raise ValueError(f"数据源 {src.id} 未声明合规依据，拒绝启动")
            self.governor.register(src)

    def _resolve_source(self, target: MonitorTarget) -> SourceConfig:
        """真实数据源优先；本地脱敏样本走 sample_local，保证无 AppKey 也能跑链路。"""
        pool = [s for s in self.sources.values() if s.get("enabled")]
        synthetic = [s for s in pool if s.get("compliance") == "synthetic"]
        real = [s for s in pool if s.get("compliance") != "synthetic"]
        chosen = synthetic[0] if (target.item_id.startswith(_SYNTHETIC_PREFIX) or not real) else real[0]
        return SourceConfig(**chosen)

    def _dedupe_ttl(self) -> int:
        """去重 TTL 必须严格小于采集间隔，否则缓存会把下一次定时刷新一起吃掉。

        TTL 7200s + 间隔 3600s 时，每个商品两天才真正采一次，监控形同虚设。
        上限取间隔的一半，留足余量避免边界抖动导致漏采。

        调试场景（同一进程内连跑多轮）必须能关掉这层缓存，否则第 2 轮起
        全部命中去重、数据根本不更新，diff 永远为空 —— 会让人误判成「检测不灵」。
        """
        if self._dedupe_ttl_override is not None:
            return max(0, int(self._dedupe_ttl_override))
        ttl = int(self.cfg.get("risk", {}).get("defaults", {}).get("dedupe_ttl_seconds", 600))
        interval = int(self.cfg.get("scheduler", {}).get("interval_seconds", 3600))
        return max(0, min(ttl, max(60, interval // 2)))


    def _collect(self, target: MonitorTarget, source: SourceConfig):
        ctx = CollectorContext(
            target=target, source=source, governor=self.governor, ttl_seconds=self._dedupe_ttl()
        )
        return build(ctx).run(target)

    def collect_cycle(self) -> dict[str, Any]:
        started = time.time()
        all_events: list[ChangeEvent] = []
        results: list[dict[str, Any]] = []

        for target in self.targets:
            source = self._resolve_source(target)
            result = self._collect(target, source)
            results.append({"item_id": target.item_id, "role": target.role, "ok": result.ok, "msg": result.message})

            if not result.ok or result.record is None:
                self.stats["skipped"] += 1
                reason = result.anomaly.value if result.anomaly else "blocked"
                self.repo.log_fetch(result.source_id, f"fail:{reason}", result.message)
                continue

            self.stats["cached" if result.cached else "fetched"] += 1
            fp = content_fingerprint(result.record.content_fingerprint_payload())
            prev = self.repo.latest(target.item_id)
            if self.repo.save_snapshot(snapshot_from_record(result.record, fp), fp):
                self.stats["saved"] += 1
                events = diff_snapshots(prev, self.repo.latest(target.item_id), role=target.role)
                if prev is not None:
                    all_events.extend(events)
                    self.stats["events"] += len(events)
                    self.repo.save_events([e.as_row() for e in events])

        retention = int(self.cfg.get("storage", {}).get("retention_days", 180))
        self.repo.drop_old(retention)

        return {
            "elapsed_ms": int((time.time() - started) * 1000),
            "stats": dict(self.stats),
            "results": results,
            "events": [e.as_row() for e in all_events],
            "top_severity": grade(all_events),
            "health": self.governor.health_report(),
        }
