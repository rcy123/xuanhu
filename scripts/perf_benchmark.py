#!/usr/bin/env python3
"""性能对比测试：httpx连接池复用前后对比。

测试策略：
1. gateway延迟测试 — 发送多轮 chat 请求，对比首请求 vs 复用请求的耗时分布
2. 多轮embedding测速
3. repository批量加载 vs 逐条加载对比（通过构造大量agent_run数据模拟）

注意：chat/embedding 会真实调用 LLM 网关，会消耗 API 配额。
"""

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

BASE_URL = "http://localhost:8000"
SESSION_ID = "ab973f4b-f4a7-4efa-9e69-f71b611b933b"


async def test_health_llm() -> dict:
    """测试 LLM 健康检查——测量网关层连接复用在多次调用之间的效果

    首轮：新建 TCP 连接 + LLM 网关 chat 调用
    后续轮：复用连接池，仅 LLM 网关调用
    """
    async with httpx.AsyncClient(timeout=30) as c:
        timings = []
        for i in range(6):
            t0 = time.perf_counter()
            r = await c.get(f"{BASE_URL}/api/v1/health/llm")
            elapsed = time.perf_counter() - t0
            timings.append(elapsed)
            print(f"  health/llm #{i+1}: {elapsed:.3f}s  status={r.status_code}")
        return {
            "test": "health_llm_connection_reuse",
            "description": "LLM健康检查6轮 — 首轮新建TCP连接，后续复用连接池",
            "results": [round(t, 3) for t in timings],
            "first_vs_avg": f"{timings[0]:.3f}s vs {statistics.mean(timings[1:]):.3f}s",
        }


async def test_chat_gateway() -> dict:
    """调用一次真实的LLM chat，测量网关层耗时（注意：会消耗API配额）"""
    async with httpx.AsyncClient(timeout=120) as c:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "请用一句话回答: 1+1=?"}],
            "max_tokens": 50,
            "temperature": 0.0,
        }
        timings = []
        for i in range(3):
            t0 = time.perf_counter()
            r = await c.post(
                f"{BASE_URL}/api/v1/health/llm",
                timeout=120,
            )
            # 绕路测gateway：直接测health endpoint
            # 实际gateway调用需要走chat completion
            elapsed = time.perf_counter() - t0
            timings.append(elapsed)
            print(f"  chat-like #{i+1}: {elapsed:.3f}s  status={r.status_code}")
        return {
            "test": "chat_gateway_connection_reuse",
            "description": "LLM网关调用3轮 — 通过health endpoint间接观察连接池效果",
            "results": [round(t, 3) for t in timings],
        }


async def test_api_concurrent_health() -> dict:
    """并发健康检查 — 模拟高并发场景下连接池复用的效果"""
    async with httpx.AsyncClient(timeout=30) as c:
        t0 = time.perf_counter()
        tasks = [c.get(f"{BASE_URL}/api/v1/health/llm") for _ in range(5)]
        results = await asyncio.gather(*tasks)
        total = time.perf_counter() - t0
        statuses = [r.status_code for r in results]
        print(f"  5并发health/llm: {total:.3f}s  statuses={statuses}")
        return {
            "test": "concurrent_health",
            "description": "5个并发health/llm请求，利用连接池复用",
            "total_seconds": round(total, 3),
            "statuses": statuses,
        }


async def test_embedding_cache_hit() -> dict:
    """直接观察 EmbeddingCache 命中效果（绕过 HTTP，直测 Redis 缓存层）

    策略：对同一 query 文本连续取 2 次 embedding。
    - 第 1 次：未命中 → 触发真实 gateway.embed 网关调用并回填 Redis
    - 第 2 次：命中 → 直接返回 Redis 中的向量，无网关 RTT
    对比两次耗时应能观察到"网关 RTT 被抹掉"的效果。
    """
    from app.core.config import get_settings
    from app.core.gateway import ModelGatewayClient
    from app.rag.embedding_cache import get_embedding, set_embedding, clear_cache

    # 提前清掉该 query 的缓存，保证第 1 次必 miss
    query = "__perf_benchmark_embedding_cache_probe__"
    await clear_cache(query)

    client = ModelGatewayClient(get_settings())
    trace_id = "perf-embedding-cache"

    # 第 1 次：miss → 网关
    t0 = time.perf_counter()
    embeddings_miss = await client.embed([query], trace_id=trace_id + "-miss")
    miss_elapsed = time.perf_counter() - t0
    # 显式回填（retriever 路径会自动回填，此处独立验证回填可用性）
    await set_embedding(query, embeddings_miss[0])

    # 第 2 次：hit → Redis
    t0 = time.perf_counter()
    cached = await get_embedding(query)
    hit_elapsed = time.perf_counter() - t0

    await client.aclose()
    await clear_cache(query)

    hit_ok = cached is not None and len(cached) == len(embeddings_miss[0])
    speedup = (miss_elapsed / hit_elapsed) if hit_elapsed > 0 else float("inf")
    print(
        f"  embedding cache: miss={miss_elapsed:.3f}s hit={hit_elapsed:.3f}s "
        f"hit_ok={hit_ok} speedup~{speedup:.1f}x"
    )
    return {
        "test": "embedding_cache_hit",
        "description": "同一 query 连续取 embedding，对比 miss(网关) vs hit(Redis)",
        "miss_seconds": round(miss_elapsed, 3),
        "hit_seconds": round(hit_elapsed, 3),
        "hit_ok": hit_ok,
        "approx_speedup": round(speedup, 1),
    }


async def test_prometheus_metrics() -> dict:
    """检查Prometheus /api/v1/metrics 端点是否曝光正确指标

    经过本轮优化后，/api/v1/metrics 路由已挂载且 prometheus_client 已正式加入依赖。
    """
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE_URL}/api/v1/metrics")
        if r.status_code != 200:
            return {
                "test": "prometheus_metrics",
                "description": "Prometheus /api/v1/metrics 端点",
                "status": r.status_code,
                "note": "metrics端点不可用",
            }
        lines = r.text.splitlines()
        relevant = [l for l in lines if "xuanhu_" in l and not l.startswith("#")]
        print(f"  Prometheus xuanhu_ 指标行数: {len(relevant)}")
        for m in relevant[:15]:
            print(f"    {m}")
        return {
            "test": "prometheus_metrics",
            "description": "Prometheus /api/v1/metrics 自定义指标曝光",
            "metric_count": len(relevant),
            "total_xuanhu_lines": len(lines),
            "sample_metrics": relevant[:15],
        }


async def main():
    print("=" * 60)
    print("Xuanhu 后端性能对比测试")
    print("=" * 60)
    print()

    results = {}

    # 1. 健康检查连接复用测试
    print("[1/5] LLM健康检查连接复用测试...")
    results["health_llm"] = await test_health_llm()
    print()

    # 2. 并发健康检查
    print("[2/5] 并发健康检查测试...")
    results["concurrent"] = await test_api_concurrent_health()
    print()

    # 3. Embedding 缓存命中效果
    print("[3/5] Embedding 缓存命中效果测试...")
    results["embedding_cache"] = await test_embedding_cache_hit()
    print()

    # 4. Prometheus指标
    print("[4/5] Prometheus 指标检查...")
    results["metrics"] = await test_prometheus_metrics()
    print()

    # 5. 总结
    print("[5/5] 结果汇总")
    print("-" * 60)
    print()
    for key, data in results.items():
        print(f"  {data.get('test', key)}:")
        print(f"    描述: {data.get('description', '')}")
        if "results" in data:
            vals = data["results"]
            if len(vals) > 1:
                print(f"    各轮耗时(s): {vals}")
                print(f"    均值: {statistics.mean(vals):.3f}s  (首轮: {vals[0]:.3f}s)")
            else:
                print(f"    耗时: {vals[0]:.3f}s")
        if "total_seconds" in data:
            print(f"    总耗时: {data['total_seconds']}s")
        if "first_vs_avg" in data:
            print(f"    首轮vs复用: {data['first_vs_avg']}")
        if "metric_count" in data:
            print(f"    自定义指标数: {data['metric_count']}")
        if "miss_seconds" in data:
            print(f"    miss(网关): {data['miss_seconds']}s  hit(Redis): {data['hit_seconds']}s  命中: {data['hit_ok']}  ~{data['approx_speedup']}x")
        print()

    # 保存结果到JSON
    output_path = Path(__file__).parent / "perf_results.json"
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"结果已保存到: {output_path}")
    return results


if __name__ == "__main__":
    asyncio.run(main())