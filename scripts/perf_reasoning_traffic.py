#!/usr/bin/env python3
"""模拟问诊推理流量：对 intake-complete session 调用 advance → 触发 reasoning subgraph，
采集 reasoning_get_state + graph_node 等 histogram。

使用方式：
    uv run python scripts/perf_reasoning_traffic.py

前置条件：
    - API 已启动（uv run xuanhu-api）
    - PG 中有 completeness=passed + disposition=ready 的 inquiry 会话
    - LLM 网关可用（advance 会真实调用 LLM）
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

BASE_URL = "http://localhost:8000"


def _idempotency_key() -> str:
    return f"perf-traffic-{uuid.uuid4().hex[:12]}"


async def find_ready_session() -> dict | None:
    """从 PG 中查找一个 completeness=passed 且 disposition=ready 的 inquiry 会话。"""
    from app.db.session import get_session_factory
    from sqlalchemy import text

    factory = get_session_factory()
    async with factory() as db:
        r = await db.execute(
            text(
                """SELECT cs.id, cs.state_version
                   FROM consult_sessions cs
                   JOIN gate_results gr ON gr.session_id = cs.id
                    AND gr.gate_name = 'completeness'
                   WHERE cs.current_stage = 'inquiry'
                     AND cs.status = 'active'
                     AND gr.decision = 'passed'
                     AND gr.details->>'disposition' = 'ready'
                   LIMIT 1"""
            )
        )
        row = r.fetchone()
        if row is None:
            return None
        return {"session_id": str(row[0]), "state_version": row[1]}


async def advance_session(session_id: str, state_version: int, label: str) -> dict:
    """对指定会话调用 advance API 触发推理。"""
    async with httpx.AsyncClient(timeout=180) as c:
        t0 = time.perf_counter()
        r = await c.post(
            f"{BASE_URL}/api/v1/consult/sessions/{session_id}/advance",
            json={"force": False},
            headers={
                "X-Idempotency-Key": _idempotency_key(),
                "X-State-Version": str(state_version),
                "X-Trace-Id": f"perf-reasoning-{label}",
            },
        )
        elapsed = time.perf_counter() - t0
        body = r.json()
        print(f"  [{label}] advance: {r.status_code} in {elapsed:.2f}s")
        if r.status_code != 200:
            print(f"    error: {body.get('code')} — {body.get('message','')}")
        else:
            data = body.get("data", {})
            print(
                f"    stage={data.get('current_stage')}, "
                f"route={data.get('route')}, "
                f"artifacts={len(data.get('artifact_refs', []))}"
            )
        return {"status": r.status_code, "elapsed": round(elapsed, 3), "body": body}


async def collect_metrics() -> dict:
    """从 /api/v1/metrics 抓取 xuanhu_ histogram 的 count/sum。"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE_URL}/api/v1/metrics")
        if r.status_code != 200:
            return {"error": f"metrics status={r.status_code}"}
        lines = r.text.splitlines()
        metrics: dict[str, dict[str, float]] = {}
        for line in lines:
            if not line.startswith("xuanhu_"):
                continue
            if line.endswith("_count") or line.endswith("_sum"):
                line = line.rstrip()
                # e.g. xuanhu_reasoning_get_state_seconds_count 0.0
                parts = line.rsplit(" ", 1)
                if len(parts) != 2:
                    continue
                name = parts[0]
                try:
                    val = float(parts[1])
                except ValueError:
                    continue
                # Strip _count/_sum suffix for base name
                for suffix in ("_count", "_sum"):
                    if name.endswith(suffix):
                        base = name[: -len(suffix)]
                        metrics.setdefault(base, {})[suffix[1:]] = val
                        break
        return metrics


async def get_session_state(session_id: str) -> int | None:
    """获取会话当前 state_version。"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE_URL}/api/v1/consult/sessions/{session_id}")
        if r.status_code != 200:
            print(f"    get session failed: {r.status_code}")
            return None
        data = r.json().get("data", {})
        stage = data.get("current_stage")
        sv = data.get("state_version")
        print(f"    session stage={stage}, state_version={sv}")
        return sv


async def main():
    print("=" * 60)
    print("Xuanhu 问诊推理流量模拟")
    print("=" * 60)
    print()

    # 1. 采集 before metrics
    print("[1/4] 采集推理前 metrics...")
    before = await collect_metrics()
    for name in sorted(before):
        if "reasoning" in name or "graph_node" in name:
            d = before[name]
            print(f"  {name}: count={d.get('count',0)}, sum={d.get('sum',0):.3f}s")
    print()

    # 2. 查找 ready session
    print("[2/4] 查找 intake-complete 会话...")
    session = await find_ready_session()
    if session is None:
        print("  ❌ 未找到 completeness=passed 的 inquiry 会话")
        print("  提示: 先通过 API 创建会话并完成问诊流程")
        return
    sid = session["session_id"]
    sv = session["state_version"]
    print(f"  session_id={sid}, state_version={sv}")
    print()

    # 3. 执行多次 advance 触发 reasoning（使用不同 ready 会话）
    print("[3/4] 执行 advance 触发推理...")
    results = []

    # 收集多个 ready 会话，逐个触发
    from app.db.session import get_session_factory
    from sqlalchemy import text

    factory = get_session_factory()
    async with factory() as db:
        r = await db.execute(
            text(
                """SELECT cs.id, cs.state_version
                   FROM consult_sessions cs
                   JOIN gate_results gr ON gr.session_id = cs.id
                    AND gr.gate_name = 'completeness'
                   WHERE cs.current_stage = 'inquiry'
                     AND cs.status = 'active'
                     AND gr.decision = 'passed'
                     AND gr.details->>'disposition' = 'ready'
                   LIMIT 5"""
            )
        )
        ready_sessions = [(str(row[0]), row[1]) for row in r.fetchall()]

    if not ready_sessions:
        print("  ❌ 未找到 ready 会话")
        return

    for i, (sid, sv) in enumerate(ready_sessions):
        if i >= 3:
            break
        print(f"  [{i+1}/{min(3, len(ready_sessions))}] session={sid[:8]}... state_version={sv}")
        result = await advance_session(sid, sv, f"session-{i+1}")
        results.append(result)
        await asyncio.sleep(2)
    print()

    # 4. 采集 after metrics
    print("[4/4] 采集推理后 metrics...")
    await asyncio.sleep(1)  # 让 histogram 刷新
    after = await collect_metrics()
    print()

    # 5. 汇总
    print("=" * 60)
    print("推理流量结果")
    print("=" * 60)
    print()

    # Advance 耗时
    successes = [r for r in results if r["status"] == 200]
    if successes:
        times = [r["elapsed"] for r in successes]
        print(f"advance 成功: {len(successes)}/{len(results)}")
        print(f"  耗时: {times}")
        print(f"  avg: {sum(times)/len(times):.2f}s")
    else:
        print("advance: 全部失败")
        for r in results:
            body = r["body"]
            print(f"  status={r['status']}, code={body.get('code')}, msg={body.get('message','')}")
    print()

    # Histogram diff
    print("Histogram 变化 (before → after):")
    for name in sorted(after):
        if "reasoning" not in name and "graph_node" not in name:
            continue
        bv = before.get(name, {})
        av = after[name]
        b_count = bv.get("count", 0)
        a_count = av.get("count", 0)
        b_sum = bv.get("sum", 0)
        a_sum = av.get("sum", 0)
        d_count = a_count - b_count
        d_sum = a_sum - b_sum
        avg_before = b_sum / b_count if b_count > 0 else 0
        avg_after = a_sum / a_count if a_count > 0 else 0
        print(
            f"  {name}: "
            f"count {b_count}→{a_count} (+{d_count}), "
            f"sum {b_sum:.3f}→{a_sum:.3f}s, "
            f"avg {avg_before*1000:.1f}→{avg_after*1000:.1f}ms"
        )

    # 保存结果
    output = {
        "session_id": sid,
        "advance_results": results,
        "metrics_before": {k: v for k, v in before.items() if "reasoning" in k or "graph_node" in k},
        "metrics_after": {k: v for k, v in after.items() if "reasoning" in k or "graph_node" in k},
    }
    output_path = Path(__file__).parent / "perf_reasoning_results.json"
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\n结果已保存到: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
