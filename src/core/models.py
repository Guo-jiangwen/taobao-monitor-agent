"""核心数据模型。全部用 dataclass，方便跨层传递与序列化到 JSON。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class SourceCompliance(str, Enum):
    """数据源合规类别。非此枚举外的来源一律拒绝启用。"""

    OFFICIAL_API = "official_api"      # 平台官方接口，已备案 + 授权
    OWN_DATA = "own_data"              # 自有店铺/自有业务数据
    AUTHORIZED_VENDOR = "vendor"       # 第三方书面授权数据源
    SYNTHETIC = "synthetic"            # 脱敏/模拟数据，仅限自测


class AnomalyType(str, Enum):
    """异常响应类型，决定重试策略与是否触发熔断。"""

    RATE_LIMITED = "rate_limited"          # 429 / 平台限流错误码
    AUTH_EXPIRED = "auth_expired"          # 签名失效 / 未授权 / 登录墙
    VERIFY_REQUIRED = "verify_required"    # 触发验证码、滑块、风控挑战
    EMPTY_DATA = "empty_data"              # 200 但无业务数据
    MALFORMED = "malformed"                # 结构解析失败
    TRANSPORT_ERROR = "transport_error"    # 超时 / DNS / 连接错误
    CONTEXT_MISMATCH = "context_mismatch"  # 返回内容与请求资源不匹配（最危险，一律熔断）


class Severity(str, Enum):
    P0 = "P0"  # 立即告警：竞品大幅降价、下架、断货、主图换版
    P1 = "P1"  # 当日处置：促销机制变化、评分下滑、SKU 增减
    P2 = "P2"  # 并入日报：标题卖点调整、详情页改版
    P3 = "P3"  # 静默观察：常规波动


class SourceState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"    # 抛错但可自愈，降级继续
    OPEN = "open"            # 熔断开启
    PAUSED = "paused"        # 自动暂停，需人工确认恢复


@dataclass
class SourceConfig:
    """一个采集源的完整声明。"""

    id: str
    adapter: str
    enabled: bool = False
    display_name: str = ""
    compliance: str = ""
    compliance_note: str = ""
    host: str = ""
    endpoint: str = ""
    api: str = ""
    qps: float = 0.5
    burst: int = 3
    daily_quota: int = 1000
    dedupe_ttl_seconds: int = 7200
    credentials_env: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitorTarget:
    item_id: str
    title: str
    role: str = "self"      # self | competitor
    priority: int = 3       # 1 最高，数字越小越紧急


@dataclass
class RawRecord:
    """采集器返回的原始归一化记录。采集层只负责取数和字段对齐，不负责分析。"""

    item_id: str
    source_id: str
    title: str = ""
    price: float | None = None
    original_price: float | None = None
    stock: int | None = None
    listing_status: str = "unknown"   # on_sale | off_shelf | unknown
    main_image: str = ""
    promotion: str = ""
    review_count: int | None = None
    rating: float | None = None
    sku_list: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def content_fingerprint_payload(self) -> str:
        """参与指纹计算的字段。剔除采集时间等噪声，保证同一内容重复请求不产生新快照。"""
        return "|".join(
            str(
                [
                    self.listing_status,
                    self.price,
                    self.original_price,
                    self.stock,
                    self.title,
                    self.main_image,
                    self.promotion,
                    self.review_count,
                    self.rating,
                    sorted(self.sku_list),
                ]
            )
        )


@dataclass
class FetchResult:
    """一次受治理的采集结果。ok=False 时 anomaly 必有值，禁止静默吞错。"""

    ok: bool
    source_id: str
    item_id: str = ""
    record: RawRecord | None = None
    cached: bool = False          # 命中去重缓存
    blocked: bool = False         # 被风控拦截（熔断/配额/限频）
    anomaly: AnomalyType | None = None
    message: str = ""
    elapsed_ms: int = 0
    fetched_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class Snapshot:
    """落库的快照。历史保留，供 diff 使用。"""

    id: int | None = None
    item_id: str = ""
    source_id: str = ""
    title: str = ""
    price: float | None = None
    stock: int | None = None
    listing_status: str = ""
    main_image: str = ""
    promotion: str = ""
    review_count: int | None = None
    rating: float | None = None
    content_fp: str = ""
    payload: str = "{}"
    fetched_at: int = field(default_factory=lambda: int(time.time()))

    @classmethod
    def from_record(cls, rec: RawRecord, fp: str) -> "Snapshot":
        return cls(
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
            payload=rec.to_dict().__str__(),
        )
