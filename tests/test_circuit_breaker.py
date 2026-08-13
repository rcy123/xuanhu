"""熔断器（阶段4 过载保护）单元测试。

覆盖 CircuitBreaker 的三态迁移：闭合 → 打开 → 半开 → 闭合/重新打开。
时间用 monkeypatch 控制 ``time.monotonic``，不依赖真实等待。
"""

from __future__ import annotations

import pytest

from app.core.circuit_breaker import CircuitBreaker


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """用可手动推进的假时钟替换 time.monotonic。"""
    state = {"now": 0.0}
    monkeypatch.setattr("app.core.circuit_breaker.time.monotonic", lambda: state["now"])
    return state


def test_opens_after_consecutive_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续失败达到阈值后打开熔断器。"""
    _fake_clock(monkeypatch)
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=10.0)

    assert cb.is_open is False
    cb.record_failure()
    assert cb.is_open is False
    assert cb.consecutive_failures == 1
    cb.record_failure()
    assert cb.is_open is True


def test_success_resets_failure_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功清零连续失败计数并保持闭合。"""
    _fake_clock(monkeypatch)
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=10.0)

    cb.record_failure()
    cb.record_failure()
    assert cb.consecutive_failures == 2
    cb.record_success()
    assert cb.consecutive_failures == 0
    assert cb.is_open is False


def test_half_open_probe_success_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷却结束后进入半开，探测成功后闭合。"""
    clock = _fake_clock(monkeypatch)
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10.0)

    cb.record_failure()
    assert cb.is_open is True

    clock["now"] = 10.0  # 冷却结束 → 半开
    assert cb.is_open is False

    cb.record_success()
    assert cb.is_open is False
    assert cb.consecutive_failures == 0


def test_half_open_probe_failure_reopens(monkeypatch: pytest.MonkeyPatch) -> None:
    """半开探测失败后重新打开。"""
    clock = _fake_clock(monkeypatch)
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10.0)

    cb.record_failure()
    assert cb.is_open is True

    clock["now"] = 10.0  # 半开
    assert cb.is_open is False

    cb.record_failure()
    assert cb.is_open is True


def test_rejects_invalid_config() -> None:
    """非法参数（阈值<1 或冷却≤0）应 fail-fast。"""
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0, cooldown_seconds=1.0)
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0)


def test_is_open_callable_smoke() -> None:
    """is_open 是只读属性，不修改内部状态。"""
    cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=1.0)
    assert cb.is_open is False
    assert cb.is_open is False
    assert cb.consecutive_failures == 0
