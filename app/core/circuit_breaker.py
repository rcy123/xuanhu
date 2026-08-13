"""简单熔断器（阶段4 过载保护）。

连续失败达到阈值后进入「打开」状态一段时间；打开期间调用方应**快速失败**
而不是继续发起可能超时的下游请求，避免在模型网关持续故障时用 60s×重试
硬扛、放大延迟与积压。冷却结束后进入「半开」状态，放行一个探测请求：
成功即闭合，失败即重新打开。

纯同步、无 I/O：状态变更在 asyncio 单事件循环内是原子的，调用方（如
``ModelGatewayClient``）无需额外加锁。
"""

from __future__ import annotations

import time


class CircuitBreaker:
    """连续失败 N 次后打开，冷却 ``cooldown_seconds`` 后进入半开探测。"""

    def __init__(self, *, failure_threshold: int = 5, cooldown_seconds: float = 30.0) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        """是否处于「打开」状态（调用方应快速失败）。

        冷却结束后进入半开（返回 False），放行探测请求，由
        ``record_success`` / ``record_failure`` 决定闭合或重新打开。
        """
        if self._opened_at is None:
            return False
        return time.monotonic() - self._opened_at < self._cooldown_seconds

    @property
    def consecutive_failures(self) -> int:
        """当前连续失败计数（测试/诊断用）。"""
        return self._consecutive_failures

    def record_success(self) -> None:
        """记录一次成功，闭合电路并清零计数。"""
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """记录一次失败；达到阈值后打开电路。"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_at = time.monotonic()


__all__ = ["CircuitBreaker"]
