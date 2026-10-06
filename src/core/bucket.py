"""令牌桶限频器。

为什么用令牌桶而不是 `time.sleep(1/qps)`：
sleep 是无脑匀速，会把「攒着的请求」也拉平。真实场景是「整点批量刷新 20 个竞品」，
希望这 20 个能一口气打出去（突发能力），之后再匀速。
令牌桶 = 按 rate 每秒倒入令牌，桶最多装 capacity 个；请求要付 1 个令牌才发得出去。
桶满时可突发 capacity 发，桶空时必须等。

两个容易写错的点：
1. 按时间差一次性补令牌，不要 while sleep 循环补——遇到长时间空闲会空转且算不准。
2. 用 time.monotonic() 而不是 time.time()——墙钟被 NTP 回拨时会算出负的 elapsed，
   凭空补出一桶白送的令牌，限频直接失效。
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """按 rate 匀速补充令牌，桶容量 capacity。

    - try_acquire() 拿到令牌返回 True，桶空返回 False（立即拒绝，不排队）
    - retry_after() 返回「还要等几秒才有 1 个令牌」
    """

    def __init__(self, rate: float, capacity: int, name: str = ""):
        if rate <= 0:
            raise ValueError("rate 必须为正")
        if capacity < 1:
            raise ValueError("capacity 必须 >= 1")
        self.rate = float(rate)
        self.capacity = int(capacity)
        self.name = name
        # 初始为满桶：服务刚启动时允许一次突发，这也是 capacity 的意义
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """按距上次补充的时间差一次性补令牌。调用方必须已持锁。"""
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed <= 0:
            # 理论上不会发生（monotonic 单调），留一道防御
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    @property
    def tokens(self) -> float:
        """当前令牌数（调试用）。

        注意：读状态也必须走补偿逻辑。否则桶空置 0.3 秒后直接返回 _tokens，
        拿到的是 0 而不是补进来的 2，跟 try_acquire 看到的世界不一致。
        """
        with self._lock:
            self._refill()
            return self._tokens

    def try_acquire(self, n: int = 1) -> bool:
        """拿 n 个令牌；不够就一个都不给（原子操作，多线程安全）。"""
        if n <= 0:
            return True
        # 必须先补、再判断、再扣减，三步整体持锁。
        # 把 if self._tokens >= n 和 self._tokens -= n 拆开，多线程下就会超发。
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def retry_after(self) -> float:
        """距下一次可取令牌还需多少秒。"""
        with self._lock:
            self._refill()
            if self._tokens >= 1:
                return 0.0
            # 差 1 个令牌才够发，每个令牌需要 1/rate 秒
            return (1 - self._tokens) / self.rate
