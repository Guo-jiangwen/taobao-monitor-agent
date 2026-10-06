"""风险治理内核：限频 + 配额 + 去重 + 异常识别 + 熔断 + 自动暂停。

设计原则：
- 「慢」比「断」好。宁可采集晚 10 分钟，也不给平台制造额外负担。
- 任何一次失败都必须被分类，不能静默吞掉。
- 熔断后必须自动暂停而不是疯狂重试，重试间隔指数退避。
- 触达日配额硬停，不靠「自觉」。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Callable

from .fingerprint import request_fingerprint
from .models import AnomalyType, FetchResult, RawRecord, SourceConfig, SourceState


class FetchError(Exception):
    """采集异常，携带平台返回的原始状态码和正文，供分类器判定。"""

    def __init__(self, status: int = 0, body: str = "", exc: BaseException | None = None):
        super().__init__(exc.__class__.__name__ if exc else f"HTTP {status}")
        self.status = status
        self.body = body or ""
        self.exc = exc


# ---------- 令牌桶：平滑限频 ----------


class TokenBucket:
    """按 rate 匀速补充令牌，容量 burst。超量请求直接被拒而不是排队到进城。"""

    def __init__(self, rate: float, capacity: int, name: str = ""):
        self.rate = float(rate)
        self.capacity = max(1, int(capacity))
        self.name = name
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False

    def retry_after(self) -> float:
        """还要等多久才有 1 个令牌。"""
        with self._lock:
            self._refill()
            return max(0.0, (1 - self._tokens) / self.rate) if self.rate > 0 else 60.0


# ---------- 日配额账本 ----------


class DailyQuota:
    """按源 + 自然日计数。跨天自动清零，触顶即硬停。"""

    def __init__(self, limit: int, name: str = ""):
        self.limit = int(limit)
        self.name = name
        self._count = 0
        self._day = date.today()
        self._lock = threading.Lock()

    def consumed(self) -> int:
        with self._lock:
            self._rollover()
            return self._count

    def remaining(self) -> int:
        return max(0, self.limit - self.consumed())

    def try_consume(self) -> bool:
        with self._lock:
            self._rollover()
            if self._count >= self.limit:
                return False
            self._count += 1
            return True

    def _rollover(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._count = 0


# ---------- 熔断器 ----------


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    open_seconds: float = 60.0
    half_open_max_attempts: int = 1
    max_open_seconds: float = 3600.0
    backoff_factor: float = 2.0

    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opens: int = 0
    open_until: float = 0.0
    half_open_attempts: int = 0
    last_reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def open_remaining(self) -> float:
        return max(0.0, self.open_until - time.monotonic())

    def allows(self) -> bool:
        with self._lock:
            if self.state == BreakerState.OPEN:
                if self.open_remaining <= 0:
                    self.state = BreakerState.HALF_OPEN
                    self.half_open_attempts = 0
                    return True
                return False
            if self.state == BreakerState.HALF_OPEN:
                return self.half_open_attempts < self.half_open_max_attempts
            return True

    def record_success(self) -> None:
        with self._lock:
            self.state = BreakerState.CLOSED
            self.failures = 0
            self.open_until = 0.0
            self.half_open_attempts = 0
            self.last_reason = ""

    def record_failure(self, anomaly: AnomalyType, detail: str = "", retry_after: float = 0.0) -> BreakerState:
        """记录失败并返回新状态。永久级异常（签名/风控/串号）直接开闸，不攒次数。"""
        with self._lock:
            if anomaly in (
                AnomalyType.CONTEXT_MISMATCH,
                AnomalyType.VERIFY_REQUIRED,
                AnomalyType.AUTH_EXPIRED,
            ):
                self.failures += self.failure_threshold
            else:
                self.failures += 1

            if self.failures < self.failure_threshold:
                return self.state

            self.opens += 1
            self.state = BreakerState.OPEN
            self.last_reason = f"{anomaly.value}:{detail[:120]}"
            if retry_after > 0:
                self.open_until = time.monotonic() + retry_after
            else:
                # 必须写成「当前时刻 + 持续时长」，写成绝对值会让熔断立即失效
                cooldown = min(
                    self.open_seconds * (self.backoff_factor ** max(0, self.opens - 1)),
                    self.max_open_seconds,
                )
                self.open_until = time.monotonic() + cooldown
            self.half_open_attempts = 0
            return self.state


# ---------- 异常分类 ----------

_RATE_CODES = {
    "isv.limit", "isv.limit.rate", "fail_sys_traffic_limit", "fail_sys_traffic_limit_control",
    "isp.traffic_limit", "isp.cpu_limit", "isp.server_error",
    "isv.sub_account_error", "api.service.trade.repeat",
}
# 注意：codes 在比较前会 .upper()，所以码表一律写成大写，否则大小写不匹配会静默判不出来
_CONTEXT_CODES = {"CONTEXT_MISMATCH", "ITEM_NOT_MATCH", "RESOURCE_MISMATCH", "DATA_MISMATCH"}
_RATE_TEXT = re.compile(r"ISV_LIMIT|TRAFFIC_LIMIT|RESOURCE_EXCEED|API\s*调用频繁|访问过于频繁|too\s+many\s+requests", re.I)
_AUTH_TEXT = re.compile(r"invalid\s+session|未授权|登录失效|请重新登录|签名错误|appkey\s+不在白名单|missing\s+appkey", re.I)
_VERIFY_TEXT = re.compile(r"验证码|captcha|滑块|punish|风控校验|人机验证")
_EMPTY_TEXT = re.compile(r"^\s*$|null|\[\]|\{\s*\}")


def classify_http(status: int, body: str) -> AnomalyType | None:
    """把 HTTP 响应翻译成异常类型。返回 None 表示正常。"""
    text = (body or "")[:4000]

    if status in (429, 503):
        return AnomalyType.RATE_LIMITED
    if status in (401, 403):
        return AnomalyType.AUTH_EXPIRED if _AUTH_TEXT.search(text) else AnomalyType.RATE_LIMITED
    if status >= 500:
        return AnomalyType.TRANSPORT_ERROR

    # 业务错误码：JSON body 里的 code / sub_code / msg
    code, sub_code, msg = "", "", ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            code = str(obj.get("code") or obj.get("error_code") or "")
            sub_code = str(obj.get("sub_code") or obj.get("error_description") or "")
            msg = str(obj.get("msg") or obj.get("message") or "")
    except Exception:
        pass
    codes = (code + "|" + sub_code + "|" + msg).upper()

    if _VERIFY_TEXT.search(text):
        return AnomalyType.VERIFY_REQUIRED
    if any(c in codes for c in _CONTEXT_CODES):
        return AnomalyType.CONTEXT_MISMATCH
    if _AUTH_TEXT.search(text) or "INVALIDSESSION" in codes or "SIGN" in codes and "ERROR" in codes:
        return AnomalyType.AUTH_EXPIRED
    if _RATE_TEXT.search(text) or any(c in codes for c in _RATE_CODES):
        return AnomalyType.RATE_LIMITED
    return None


# ---------- 治理器 ----------


@dataclass
class SourceRuntime:
    bucket: TokenBucket
    quota: DailyQuota
    breaker: CircuitBreaker
    dedupe: dict[str, tuple[float, FetchResult]] = field(default_factory=dict)
    paused: bool = False
    pause_reason: str = ""
    last_result: FetchResult | None = None
    stats: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # 连续失败计数。熔断被挡下的请求同样计入：闸开着说明源没恢复，
    # 只统计「真正打到源上才失败」的路径会让 opens 永远停在 1，自动暂停永远不会触发。
    consecutive_failures: int = 0
    total_failures: int = 0


class RiskGovernor:
    """统一入口。所有采集必须经过这里，绕过去的代码 review 一律打回。"""

    def __init__(self, policy: dict[str, Any] | None = None):
        self.policy = policy or {}
        self.defaults = self.policy.get("defaults", {})
        self.global_policy = self.policy.get("global", {})
        self.anomaly_cfg = self.defaults.get("anomaly", {})
        self._runtimes: dict[str, SourceRuntime] = {}
        self._lock = threading.Lock()
        self.audit_sink: Callable[[dict[str, Any]], None] | None = None

    # ---- 注册 ----
    def register(self, cfg: SourceConfig) -> None:
        b = self.defaults.get("breaker", {})
        rt = SourceRuntime(
            bucket=TokenBucket(cfg.qps, cfg.burst, cfg.id),
            quota=DailyQuota(cfg.daily_quota, cfg.id),
            breaker=CircuitBreaker(
                failure_threshold=int(b.get("failure_threshold", 5)),
                open_seconds=float(b.get("open_seconds", 60)),
                half_open_max_attempts=int(b.get("half_open_max_attempts", 1)),
                max_open_seconds=float(b.get("max_open_seconds", 3600)),
                backoff_factor=float(b.get("backoff_factor", 2.0)),
            ),
        )
        with self._lock:
            self._runtimes[cfg.id] = rt

    def runtime(self, source_id: str) -> SourceRuntime:
        with self._lock:
            if source_id not in self._runtimes:
                rt = SourceRuntime(
                    bucket=TokenBucket(self.defaults.get("qps", 0.5), self.defaults.get("burst", 3), source_id),
                    quota=DailyQuota(self.defaults.get("daily_quota", 1000), source_id),
                    breaker=CircuitBreaker(**{
                        k: v for k, v in self.defaults.get("breaker", {}).items()
                    }),
                )
                self._runtimes[source_id] = rt
            return self._runtimes[source_id]

    def state_of(self, source_id: str) -> SourceState:
        rt = self.runtime(source_id)
        if rt.paused:
            return SourceState.PAUSED
        if rt.breaker.state == BreakerState.OPEN:
            # 冷却已结束但还没打探测请求时，对外就报「降级」，别继续假装在熔断中
            return SourceState.DEGRADED if rt.breaker.open_remaining <= 0 else SourceState.OPEN
        if rt.breaker.state == BreakerState.HALF_OPEN:
            return SourceState.DEGRADED
        return SourceState.HEALTHY

    def resume(self, source_id: str) -> None:
        """人工确认后的恢复动作。自动暂停只能靠人工或冷却到期，不允许自愈式偷偷开跑。"""
        rt = self.runtime(source_id)
        rt.paused = False
        rt.pause_reason = ""
        rt.consecutive_failures = 0
        rt.total_failures = 0
        rt.breaker.record_success()

    # ---- 核心：受治理的采集 ----
    def execute(
        self,
        *,
        source_id: str,
        resource_url: str,
        fetch: Callable[[], RawRecord],
        ttl_seconds: int | None = None,
        item_id: str = "",
        max_retries: int = 2,
        collect_context: bool = True,
    ) -> FetchResult:
        started = time.time()
        rt = self.runtime(source_id)

        # 1) 人工暂停 —— 最高优先级，直接返回
        if rt.paused:
            return FetchResult(
                ok=False, source_id=source_id, item_id=item_id, blocked=True,
                anomaly=AnomalyType.AUTH_EXPIRED, message=f"源已自动暂停：{rt.pause_reason}",
                elapsed_ms=int((time.time() - started) * 1000),
            )

        # 2) 熔断检查
        if not rt.breaker.allows():
            wait = int(rt.breaker.open_remaining)
            self._note_failure(rt)
            self._pause_if_needed(source_id, rt)
            self._audit(source_id, "blocked", "breaker_open", wait)
            return FetchResult(
                ok=False, source_id=source_id, item_id=item_id, blocked=True,
                anomaly=AnomalyType.RATE_LIMITED,
                message=f"熔断器开启，{wait}s 后重试：{rt.breaker.last_reason}",
                elapsed_ms=int((time.time() - started) * 1000),
            )

        # 3) 请求级去重：同样的 URL + 参数在 TTL 内只发一次
        fp = request_fingerprint(resource_url)
        ttl = ttl_seconds if ttl_seconds is not None else int(self.defaults.get("dedupe_ttl_seconds", 7200))
        cached = rt.dedupe.get(fp)
        now = time.time()
        if cached and (now - cached[0]) < ttl:
            result = cached[1]
            result.cached = True
            self._audit(source_id, "dedup", "hit", ttl)
            rt.stats["dedup_hits"] += 1
            return result

        # 4) 日配额
        if not rt.quota.try_consume():
            self._pause(source_id, f"日配额 {rt.quota.limit} 已用尽", rt)
            self._audit(source_id, "blocked", "quota_exceeded", 0)
            return FetchResult(
                ok=False, source_id=source_id, item_id=item_id, blocked=True,
                anomaly=AnomalyType.RATE_LIMITED,
                message=f"日配额 {rt.quota.limit} 已用尽，今日停止采集",
                elapsed_ms=int((time.time() - started) * 1000),
            )

        # 5) 礼貌性抖动：随机停顿，避免机械节奏被识别为脚本
        jitter = self.global_policy.get("politeness_jitter_ms", [120, 600])
        if isinstance(jitter, list) and len(jitter) == 2:
            time.sleep(random.uniform(jitter[0], jitter[1]) / 1000.0)

        # 6) 令牌桶限频（无限频令牌时不强行发请求）
        if not rt.bucket.try_acquire():
            wait = int(rt.bucket.retry_after())
            self._audit(source_id, "throttled", "rate_limit", wait)
            return FetchResult(
                ok=False, source_id=source_id, item_id=item_id, blocked=True,
                anomaly=AnomalyType.RATE_LIMITED,
                message=f"限频保护触发，{wait}s 后重试",
                elapsed_ms=int((time.time() - started) * 1000),
            )

        # 7) 真正发请求，带退避重试
        last_anomaly, last_msg = AnomalyType.TRANSPORT_ERROR, ""
        for attempt in range(max_retries + 1):
            try:
                record = fetch()
                if record is None or not getattr(record, "item_id", ""):
                    raise FetchError(200, "{}")
                # 串号校验：请求 A 商品却返回 B 商品，说明接口被缓存污染或路由错乱。
                # 这类数据一旦落库会污染整个 diff 基线，必须当作最严重异常处理。
                if collect_context and item_id and record.item_id != item_id:
                    raise FetchError(200, json.dumps(
                        {"sub_code": "CONTEXT_MISMATCH",
                         "msg": f"requested={item_id} but got={record.item_id}"}))
                rt.stats["success"] += 1
                rt.consecutive_failures = 0
                rt.breaker.record_success()
                result = FetchResult(ok=True, source_id=source_id, item_id=record.item_id, record=record)
                self._cache_dedupe(rt, fp, result, ttl)
                self._audit(source_id, "ok", "fetched", attempt)
                return result
            except FetchError as err:
                last_anomaly = classify_http(err.status, err.body) or AnomalyType.TRANSPORT_ERROR
                last_msg = f"{last_anomaly.value}:{err.body[:160]}"
            except Exception as err:  # noqa: BLE001 - 采集器任意异常都要被分类
                last_anomaly = AnomalyType.TRANSPORT_ERROR
                last_msg = f"{err.__class__.__name__}: {str(err)[:160]}"
                break

            rt.stats["failure"] += 1
            new_state = rt.breaker.record_failure(last_anomaly, last_msg)
            self._note_failure(rt)
            if new_state == BreakerState.OPEN:
                self._pause_if_needed(source_id, rt)
            if attempt < max_retries and last_anomaly in (
                AnomalyType.RATE_LIMITED,
                AnomalyType.TRANSPORT_ERROR,
                AnomalyType.EMPTY_DATA,
            ):
                time.sleep(min(2.0 ** (attempt + 1), 8.0) + random.uniform(0, 0.5))
                continue
            break

        result = FetchResult(
            ok=False, source_id=source_id, item_id=item_id,
            anomaly=last_anomaly, message=last_msg,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        rt.last_result = result
        self._cache_dedupe(rt, fp, result, ttl)
        return result

    # ---- 内部 ----
    def _pause(self, source_id: str, reason: str, rt: SourceRuntime) -> None:
        rt.paused = True
        rt.pause_reason = reason
        self._audit(source_id, "paused", "auto", 0)

    def _note_failure(self, rt: SourceRuntime) -> None:
        rt.consecutive_failures += 1
        rt.total_failures += 1

    def _pause_if_needed(self, source_id: str, rt: SourceRuntime) -> None:
        # 两条腿：①反复开闸 ②长时间没恢复。只认一条都会漏——
        # 闸开着时请求被挡下，opens 不再增长，光看 opens 就永远等不到暂停。
        opens_limit = int(self.defaults.get("pause_after_breaker_opens", 3))
        fail_limit = int(self.defaults.get("pause_after_consecutive_failures", 5))
        if rt.breaker.opens >= opens_limit:
            self._pause(source_id, f"连续熔断 {rt.breaker.opens} 次，人工确认后恢复", rt)
        elif rt.consecutive_failures >= fail_limit:
            self._pause(
                source_id,
                f"连续 {rt.consecutive_failures} 次失败未恢复（累计 {rt.total_failures} 次），人工确认后恢复",
                rt,
            )

    def _cache_dedupe(self, rt: SourceRuntime, fp: str, result: FetchResult, ttl: int) -> None:
        """只缓存成功结果。失败结果缓存意义不大，还会拖慢重试。"""
        if result.ok:
            rt.dedupe[fp] = (time.time(), result)
            self._gc_dedupe(rt, ttl)

    def _gc_dedupe(self, rt: SourceRuntime, ttl: int) -> None:
        now = time.time()
        for k in [k for k, (ts, _) in list(rt.dedupe.items()) if now - ts > ttl]:
            rt.dedupe.pop(k, None)

    def _audit(self, source_id: str, outcome: str, reason: str, extra: Any) -> None:
        if self.audit_sink:
            self.audit_sink({"source_id": source_id, "outcome": outcome, "reason": reason, "detail": extra, "ts": time.time()})

    def health_report(self) -> dict[str, Any]:
        out = {}
        for sid, rt in self._runtimes.items():
            out[sid] = {
                "state": self.state_of(sid).value,
                "breaker": rt.breaker.state.value,
                "opens": rt.breaker.opens,
                "consecutive_failures": rt.consecutive_failures,
                "total_failures": rt.total_failures,
                "open_remaining_s": round(rt.breaker.open_remaining, 1),
                "quota_used": rt.quota.consumed(),
                "quota_limit": rt.quota.limit,
                "dedup_cache": len(rt.dedupe),
                "paused": rt.paused,
                "paused_reason": rt.pause_reason,
                "stats": dict(rt.stats),
            }
        return out
