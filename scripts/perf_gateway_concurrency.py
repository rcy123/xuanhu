#!/usr/bin/env python3
"""模型网关并发压测脚本（阶段4 验证 / 阶段1 前提验证用）。

目标：
1. 校准真实模型网关延迟：不同 max_tokens（32/128/512）下单次 chat 延迟，
   用于估算真实开方链路（一次 advance 3 次调用、max_tokens=4096）的延迟量级。
2. 并发吞吐扫描：并发 1/2/4/8/16/32 下 ModelGatewayClient（httpx 64 连接池）
   的吞吐 / p50 / p95 / p99 / 错误率。
   —— 直接验证阶段1 的核心前提：「只要模型网关有 64 连接的能力，串行消费就是浪费」。

使用方式：
    uv run python scripts/perf_gateway_concurrency.py

前置条件：
    - .env 已配置 MODEL_GATEWAY_*（真实网关，非 placeholder）
    - 会真实调用 LLM 网关，消耗 API 配额（本轮约 130 次小请求）

注意：
    - max_tokens 刻意用较小值控制配额与耗时；真实生成延迟见校准阶段的缩放曲线。
    - 输出脱敏，不打印 API Key。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Any, cast

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Windows 控制台 UTF-8 输出（避免 GBK 编码错误）
if sys.platform == "win32":
    try:
        cast(Any, sys.stdout).reconfigure(encoding="utf-8")
        cast(Any, sys.stderr).reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.core.config import get_settings  # noqa: E402
from app.core.gateway import ModelGatewayClient  # noqa: E402

PROMPT = "请用简洁的中文回答：什么是辨证论治？"
CONCURRENCY_LEVELS = [1, 2, 4, 8, 16, 32]
ROUNDS = 20
CALIBRATION_TOKENS = [32, 128, 512]
CALIBRATION_ROUNDS = 3
SWEEP_MAX_TOKENS = 64


def _nearest_rank_percentile(sorted_values: list[float], p: float) -> float:
    """最近秩百分位（小样本下 p95/p99 趋近 max，可接受）。"""
    if not sorted_values:
        raise ValueError("empty")
    idx = min(len(sorted_values) - 1, max(0, round(len(sorted_values) * p) - 1))
    return sorted_values[idx]


async def _single_call(client: ModelGatewayClient, max_tokens: int, trace_id: str) -> tuple[float, str | None]:
    t0 = time.perf_counter()
    try:
        await client.chat(
            [{"role": "user", "content": PROMPT}],
            max_tokens=max_tokens,
            trace_id=trace_id,
        )
        return time.perf_counter() - t0, None
    except Exception as exc:  # noqa: BLE001 - 压测脚本需要统计所有错误
        return time.perf_counter() - t0, type(exc).__name__


async def _calibrate(client: ModelGatewayClient) -> None:
    print("== 1. 单次延迟校准（不同 max_tokens） ==")
    for mt in CALIBRATION_TOKENS:
        latencies: list[float] = []
        errors: list[str] = []
        for i in range(CALIBRATION_ROUNDS):
            elapsed, err = await _single_call(client, mt, f"cal-{mt}-{i}")
            if err is None:
                latencies.append(elapsed)
            else:
                errors.append(err)
        if latencies:
            print(
                f"  max_tokens={mt:>4}: p50={statistics.median(latencies) * 1000:6.0f}ms  "
                f"min={min(latencies) * 1000:6.0f}ms  max={max(latencies) * 1000:6.0f}ms  n={len(latencies)}"
            )
        if errors:
            print(f"  max_tokens={mt}: 错误 {errors}")


async def _sweep(client: ModelGatewayClient) -> None:
    print(f"\n== 2. 并发吞吐扫描（max_tokens={SWEEP_MAX_TOKENS}, {ROUNDS} 轮/级） ==")
    print(f"{'并发':>4} | {'吞吐(req/min)':>13} | {'p50(ms)':>8} | {'p95(ms)':>8} | {'p99(ms)':>8} | {'失败':>4}")

    async def _run_level(conc: int) -> None:
        sem = asyncio.Semaphore(conc)
        results: list[float] = []
        errors = 0
        t0 = time.perf_counter()

        async def worker(i: int) -> None:
            nonlocal errors
            async with sem:
                elapsed, err = await _single_call(client, SWEEP_MAX_TOKENS, f"sweep-{conc}-{i}")
                if err is not None:
                    errors += 1
                else:
                    results.append(elapsed)

        await asyncio.gather(*(worker(i) for i in range(ROUNDS)))
        total = time.perf_counter() - t0
        if results:
            sorted_results = sorted(results)
            throughput = len(results) * 60.0 / total
            p50 = statistics.median(results) * 1000
            p95 = _nearest_rank_percentile(sorted_results, 0.95) * 1000
            p99 = _nearest_rank_percentile(sorted_results, 0.99) * 1000
            print(f"{conc:>4} | {throughput:>12.1f} | {p50:>8.0f} | {p95:>8.0f} | {p99:>8.0f} | {errors:>4}")
        else:
            print(f"{conc:>4} | {'--':>13} | {'--':>8} | {'--':>8} | {'--':>8} | {errors:>4}")

    for conc in CONCURRENCY_LEVELS:
        await _run_level(conc)


async def main() -> None:
    settings = get_settings()
    print("=== 模型网关并发压测 ===")
    print(f"网关: {settings.model_gateway_base_url}")
    print(f"模型: {settings.chat_model}")
    print(f"超时: {settings.model_gateway_timeout_seconds}s  重试: {settings.model_gateway_max_retries}")
    client = ModelGatewayClient(settings)
    try:
        await _calibrate(client)
        await _sweep(client)
    finally:
        await client.aclose()
    print("\n完成。")


if __name__ == "__main__":
    asyncio.run(main())
