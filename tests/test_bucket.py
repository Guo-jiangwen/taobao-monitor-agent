"""第 1 课验收：令牌桶限频器。

这些用例描述的是「行为契约」，不是实现。你只要让它们全绿，
说明你的限频器在语义上是正确的，我会再跟你对一遍实现细节。

跑法（在项目根目录）：
    python -m unittest discover -s tests -k bucket -v
"""

import threading
import time
import unittest

from src.core.bucket import TokenBucket


class TestTokenBucket(unittest.TestCase):
    def test_full_bucket_allows_burst(self):
        """桶是满的时候，允许一次性打出 capacity 个请求（这就是「突发」的意义）。"""
        b = TokenBucket(rate=1, capacity=3)
        self.assertTrue(all(b.try_acquire() for _ in range(3)), "满桶应该允许 burst")
        self.assertFalse(b.try_acquire(), "桶一空必须拒绝，不能排队放行")

    def test_refill_follows_rate(self):
        """令牌按 rate 匀速回来，不是一下子全补。"""
        b = TokenBucket(rate=5, capacity=5)
        for _ in range(5):
            b.try_acquire()
        self.assertFalse(b.try_acquire())

        time.sleep(0.2)  # 按 rate=5 应该回来大约 1 个
        self.assertTrue(b.try_acquire(), "过了 0.2s 应该补到约 1 个令牌")
        self.assertFalse(b.try_acquire(), "只该补 1 个，不能补 2 个")

    def test_no_overflow_beyond_capacity(self):
        """长时间空闲后，桶里最多只有 capacity 个，多出来的不能攒着。"""
        b = TokenBucket(rate=10, capacity=2)
        time.sleep(0.3)  # 理论可补 3 个，但桶只装得下 2 个
        self.assertLessEqual(b.tokens, 2.0001, "令牌不能溢出桶容量")

    def test_retry_after_is_estimated_correctly(self):
        """空桶时 retry_after 要能估出大概还要等多久。"""
        b = TokenBucket(rate=2, capacity=1)
        b.try_acquire()  # 把唯一的令牌拿走，桶空
        wait = b.retry_after()
        self.assertGreater(wait, 0.3, f"rate=2 时空桶应等约 0.5s，实际 {wait}")
        self.assertLess(wait, 0.7, f"rate=2 时空桶应等约 0.5s，实际 {wait}")

    def test_concurrent_no_over_issue(self):
        """8 个线程抢 50 个令牌，总数绝不能超发。这是并发限频最容易写错的地方。

        注意 rate 必须压到极小：最初写成 rate=1000，结果 8 线程跑 160 次尝试要花 1ms 以上，
        桶在这期间会合法补出 1 个令牌，断言「恰好 50」就必然随机失败 ——
        那是测试自己在假设时间不流逝，不是实现有 bug。
        把补速降到 1e-6（约 11 天补 1 个），就把「补令牌」这个变量控制住了，
        这条用例才真正只在测「扣令牌是否原子」。
        """
        b = TokenBucket(rate=1e-6, capacity=50)
        granted = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                if b.try_acquire():
                    with lock:
                        granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(granted), 50, "50 个令牌被超发了，说明取令牌不是原子操作")


if __name__ == "__main__":
    unittest.main()
