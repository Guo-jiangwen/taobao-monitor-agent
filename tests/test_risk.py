"""风控内核测试。这些用例不通过，说明采集层不能上生产。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.models import AnomalyType, RawRecord, SourceConfig  # noqa: E402
from src.core.risk import FetchError, RiskGovernor, TokenBucket, classify_http  # noqa: E402


def _rec(item_id="X1", price=99.0) -> RawRecord:
    return RawRecord(item_id=item_id, source_id="t", title="t", price=price, stock=10, listing_status="on_sale")


class TestTokenBucket(unittest.TestCase):
    def test_burst_then_limit(self):
        b = TokenBucket(rate=2.0, capacity=3)
        self.assertTrue(b.try_acquire())
        self.assertTrue(b.try_acquire())
        self.assertTrue(b.try_acquire())
        self.assertFalse(b.try_acquire(), "超出突发容量必须被挡住，不能排队猛冲")
        time.sleep(0.6)
        self.assertTrue(b.try_acquire(), "令牌按速率补充")

    def test_retry_after_is_positive_when_empty(self):
        b = TokenBucket(rate=1.0, capacity=1)
        b.try_acquire()
        self.assertGreater(b.retry_after(), 0)


class TestQuota(unittest.TestCase):
    def test_daily_cap_hard_stop(self):
        gov = RiskGovernor({"defaults": {"qps": 100, "burst": 100, "daily_quota": 2, "dedupe_ttl_seconds": 0}})
        gov.register(SourceConfig(id="s", adapter="sample", enabled=True, daily_quota=2, qps=100, burst=100))
        ok = sum(1 for _ in range(5) if gov.runtime("s").quota.try_consume())
        self.assertEqual(ok, 2, "日配额必须硬停")


class TestDedupe(unittest.TestCase):
    def test_same_request_is_not_sent_twice(self):
        gov = RiskGovernor({"defaults": {"qps": 100, "burst": 100, "daily_quota": 100, "dedupe_ttl_seconds": 60}})
        gov.register(SourceConfig(id="s", adapter="sample", enabled=True, qps=100, burst=100))
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return _rec()

        url = "https://gw.api.taobao.com/router/router?method=taobao.tbk&item_ids=X1&utm_source=ad&t=999"
        r1 = gov.execute(source_id="s", resource_url=url, fetch=fetch, ttl_seconds=60)
        r2 = gov.execute(source_id="s", resource_url=url, fetch=fetch, ttl_seconds=60)
        self.assertTrue(r1.ok)
        self.assertTrue(r2.cached, "相同请求在 TTL 内必须命中去重")
        self.assertEqual(calls["n"], 1, "去重生效意味着底层只发了一次请求")


class TestAnomalyClassification(unittest.TestCase):
    def test_rate_limited(self):
        self.assertEqual(classify_http(429, '{"code":429}'), AnomalyType.RATE_LIMITED)
        self.assertEqual(classify_http(200, '{"sub_code":"ISV_LIMIT"}'), AnomalyType.RATE_LIMITED)
        self.assertEqual(classify_http(200, '{"msg":"API 调用频繁"}'), AnomalyType.RATE_LIMITED)

    def test_auth_expired(self):
        self.assertEqual(classify_http(403, "invalid session"), AnomalyType.AUTH_EXPIRED)
        self.assertEqual(classify_http(200, '{"msg":"签名错误"}'), AnomalyType.AUTH_EXPIRED)

    def test_verify_required(self):
        self.assertEqual(classify_http(200, "请完成滑块验证"), AnomalyType.VERIFY_REQUIRED)
        self.assertEqual(classify_http(200, "captcha required"), AnomalyType.VERIFY_REQUIRED)

    def test_server_error(self):
        self.assertEqual(classify_http(502, "<html>"), AnomalyType.TRANSPORT_ERROR)
        self.assertEqual(classify_http(200, ""), None)


class TestCircuitBreaker(unittest.TestCase):
    def _governor(self, failure_threshold=2, open_seconds=300.0, pause_after=3, half_open_max=1,
                  pause_after_consecutive=None):
        defaults = {"qps": 100, "burst": 100, "daily_quota": 1000,
                    "dedupe_ttl_seconds": 0, "pause_after_breaker_opens": pause_after,
                    "breaker": {"failure_threshold": failure_threshold, "open_seconds": open_seconds,
                                "half_open_max_attempts": half_open_max, "max_open_seconds": 3600,
                                "backoff_factor": 2.0}}
        if pause_after_consecutive is not None:
            defaults["pause_after_consecutive_failures"] = pause_after_consecutive
        policy = {"defaults": defaults}
        gov = RiskGovernor(policy)
        gov.register(SourceConfig(id="s", adapter="sample", enabled=True, qps=100, burst=100))
        return gov

    def test_open_blocks_further_requests(self):
        gov = self._governor()
        def boom():
            raise FetchError(429, '{"msg":"API 调用频繁"}')

        for _ in range(2):
            gov.execute(source_id="s", resource_url="https://a/b", fetch=boom, ttl_seconds=0, max_retries=0)
        blocked = gov.execute(source_id="s", resource_url="https://a/c", fetch=boom, ttl_seconds=0, max_retries=0)
        self.assertTrue(blocked.blocked, "达到失败阈值后必须熔断")
        self.assertIsNotNone(blocked.anomaly)

    def test_open_source_stops_issuing_requests(self):
        gov = self._governor(failure_threshold=1)
        def boom():
            raise FetchError(429, "ISV_LIMIT")

        gov.execute(source_id="s", resource_url="https://a/1", fetch=boom, ttl_seconds=0, max_retries=0)
        self.assertEqual(gov.state_of("s").value, "open")
        blocked = gov.execute(source_id="s", resource_url="https://a/2", fetch=boom, ttl_seconds=0, max_retries=0)
        self.assertTrue(blocked.blocked, "闸开着就不能再往平台打请求")
        self.assertEqual(gov.state_of("s").value, "open")

    def test_auto_pause_after_repeated_opens(self):
        """真·反复开闸：闸关→半开探针打进去又失败→再开闸，累计 2 次必须暂停。"""
        gov = self._governor(failure_threshold=1, open_seconds=0.2, pause_after=2)
        def boom():
            raise FetchError(403, "invalid session")

        for i in range(6):
            gov.execute(source_id="s", resource_url=f"https://a/{i}", fetch=boom, ttl_seconds=0, max_retries=0)
            if gov.state_of("s").value == "paused":
                break
            # 闸开着时要主动等过冷却，否则半开探针永远打不出去，测不到「反复开闸」
            while gov.runtime("s").breaker.open_remaining > 0:
                time.sleep(0.02)
        self.assertGreaterEqual(gov.runtime("s").breaker.opens, 2, "应该真的反复开过闸")
        self.assertEqual(gov.state_of("s").value, "paused", "反复熔断必须自动暂停，而不是无限重试")
        r = gov.execute(source_id="s", resource_url="https://a/z", fetch=boom, ttl_seconds=0)
        self.assertTrue(r.blocked and "暂停" in r.message)

    def test_pause_when_source_never_recovers(self):
        """闸一直开着、后续请求全被挡下的场景也要能暂停——否则会假死在 open 态。"""
        gov = self._governor(failure_threshold=1, open_seconds=300.0, pause_after_consecutive=3)
        def boom():
            raise FetchError(403, "invalid session")

        for i in range(4):
            gov.execute(source_id="s", resource_url=f"https://a/{i}", fetch=boom, ttl_seconds=0, max_retries=0)
        rt = gov.runtime("s")
        self.assertGreaterEqual(rt.consecutive_failures, 3, "被熔断挡下的请求同样要计入连续失败")
        self.assertEqual(gov.state_of("s").value, "paused", "长时间未恢复必须自动暂停")

    def test_success_resets_failure_counter(self):
        gov = self._governor(failure_threshold=1, open_seconds=0.2)
        def boom():
            raise FetchError(403, "invalid session")

        gov.execute(source_id="s", resource_url="https://a/0", fetch=boom, ttl_seconds=0, max_retries=0)
        self.assertGreater(gov.runtime("s").consecutive_failures, 0)
        while gov.runtime("s").breaker.open_remaining > 0:
            time.sleep(0.02)
        gov.execute(source_id="s", resource_url="https://a/ok", fetch=lambda: _rec(), ttl_seconds=0, max_retries=0)
        rt = gov.runtime("s")
        self.assertEqual(rt.consecutive_failures, 0, "一次成功要清零，避免偶发抖动被累计成暂停")
        self.assertEqual(rt.breaker.state.value, "closed", "成功必须把闸合上")

    def test_resume_requires_explicit_call(self):
        gov = self._governor(failure_threshold=1, open_seconds=300.0, pause_after=1)
        def boom():
            raise FetchError(403, "invalid session")

        for i in range(4):
            gov.execute(source_id="s", resource_url=f"https://a/{i}", fetch=boom, ttl_seconds=0, max_retries=0)
        self.assertEqual(gov.state_of("s").value, "paused")
        gov.resume("s")
        self.assertEqual(gov.state_of("s").value, "healthy")
        self.assertEqual(gov.runtime("s").consecutive_failures, 0, "人工恢复要清零失败计数")

    def test_success_closes_breaker(self):
        gov = self._governor(failure_threshold=1)
        def boom():
            raise FetchError(429, "ISV_LIMIT")
        gov.execute(source_id="s", resource_url="https://a/b", fetch=boom, ttl_seconds=0, max_retries=0)
        self.assertEqual(gov.state_of("s").value, "open")
        gov.resume("s")
        gov.execute(source_id="s", resource_url="https://a/ok", fetch=lambda: _rec(), ttl_seconds=0)
        self.assertEqual(gov.state_of("s").value, "healthy")

    def test_half_open_probe_recovers(self):
        gov = self._governor(failure_threshold=1, open_seconds=0.2)
        def boom():
            raise FetchError(429, "ISV_LIMIT")

        gov.execute(source_id="s", resource_url="https://a/b", fetch=boom, ttl_seconds=0, max_retries=0)
        self.assertEqual(gov.state_of("s").value, "open")
        time.sleep(0.35)
        self.assertEqual(gov.state_of("s").value, "degraded", "冷却结束后进入半开探测")
        r = gov.execute(source_id="s", resource_url="https://a/ok", fetch=lambda: _rec(), ttl_seconds=0)
        self.assertTrue(r.ok, "半开探测允许一次试探请求")
        self.assertEqual(gov.state_of("s").value, "healthy")

    def test_context_mismatch_opens_immediately(self):
        gov = self._governor(failure_threshold=99)
        def wrong_item():
            r = _rec(item_id="WRONG")
            return r

        r = gov.execute(source_id="s", resource_url="https://a/x", fetch=wrong_item,
                        ttl_seconds=0, item_id="X1", max_retries=0)
        self.assertFalse(r.ok, "返回内容与请求不匹配必须被拦下")
        self.assertEqual(r.anomaly, AnomalyType.CONTEXT_MISMATCH, "串号必须被识别为最严重异常")
        self.assertEqual(gov.state_of("s").value, "open", "串号直接熔断，不攒次数")

    def test_quota_exhaustion_pauses_source(self):
        gov = RiskGovernor({"defaults": {"qps": 100, "burst": 100, "dedupe_ttl_seconds": 0,
                                         "pause_after_breaker_opens": 3,
                                         "breaker": {"failure_threshold": 2, "open_seconds": 0.2,
                                                     "half_open_max_attempts": 1, "max_open_seconds": 5,
                                                     "backoff_factor": 2.0}}})
        gov.register(SourceConfig(id="s", adapter="sample", enabled=True, qps=100, burst=100, daily_quota=3))
        ok_n = 0
        for i in range(6):
            r = gov.execute(source_id="s", resource_url=f"https://a/{i}", fetch=lambda: _rec(), ttl_seconds=0)
            ok_n += 1 if r.ok else 0
        self.assertEqual(ok_n, 3)
        self.assertEqual(gov.state_of("s").value, "paused", "配额耗尽后必须暂停而非继续打接口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
