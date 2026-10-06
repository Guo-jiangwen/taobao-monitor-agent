"""采集器注册表。新增数据源只需要在 config/sources.json 里声明，这里挂对应适配器。"""

from __future__ import annotations

from typing import Callable

from ..core.models import SourceConfig
from .base import BaseCollector, CollectorContext
from .sample import SampleCollector
from .top_api import TaobaoTopCollector

_ADAPTERS: dict[str, type[BaseCollector]] = {
    "top_api": TaobaoTopCollector,
    "sample": SampleCollector,
}


def register(adapter: str, cls: type[BaseCollector]) -> None:
    _ADAPTERS[adapter] = cls


def build(ctx: CollectorContext) -> BaseCollector:
    cls = _ADAPTERS.get(ctx.source.adapter)
    if cls is None:
        raise KeyError(f"未注册的采集器适配器：{ctx.source.adapter!r}，可选 {sorted(_ADAPTERS)}")
    return cls(ctx)
