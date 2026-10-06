"""采集器基类。

铁律：
- 只做「取数 + 字段归一化」，不做分析、不写库、不绕过风控。
- 每次取数都必须能被 RiskGovernor 包住，构造里拿不到 governor 就不允许发请求。
- 只对接合规数据源；base 里硬校验 compliance 字段，非登记类型直接拒绝启用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..core.models import SourceCompliance, SourceConfig
from ..core.risk import RiskGovernor

_ALLOWED_COMPLIANCE = {c.value for c in SourceCompliance}


@dataclass
class CollectorContext:
    """一次采集所需的全部上下文。"""

    target: Any
    source: SourceConfig
    governor: RiskGovernor
    ttl_seconds: int = 7200


class BaseCollector(ABC):
    adapter_name: str = "base"

    def __init__(self, ctx: CollectorContext):
        if ctx.source.compliance not in _ALLOWED_COMPLIANCE:
            raise ValueError(
                f"数据源 {ctx.source.id} 未声明合规依据，拒绝启用（compliance={ctx.source.compliance!r}）"
            )
        self.ctx = ctx
        self.source = ctx.source
        self.governor = ctx.governor

    @abstractmethod
    def build_request(self, target: Any) -> str:
        """返回规范化请求 URL，指纹计算统一走 canonical_url。"""

    @abstractmethod
    def parse(self, payload: dict[str, Any], target: Any) -> Any:
        """把平台响应归一化成 RawRecord。"""

    def run(self, target: Any):
        url = self.build_request(target)
        return self.governor.execute(
            source_id=self.source.id,
            resource_url=url,
            fetch=lambda: self.parse(self._do_fetch(url), target),
            ttl_seconds=self.ctx.ttl_seconds,
            item_id=getattr(target, "item_id", ""),
        )

    @abstractmethod
    def _do_fetch(self, url: str) -> dict[str, Any]:
        """真正发请求，返回 dict。失败时抛 FetchError。"""
