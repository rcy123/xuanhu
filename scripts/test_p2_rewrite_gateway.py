"""Test P2 rewrite gateway end-to-end with Qwen3.5-2B-free @ dmxapi."""
import asyncio, sys, time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


async def main():
    print('=== P2 Query Rewrite Gateway End-to-End Test ===')
    print()

    from app.core.config import get_settings
    from app.core.gateway import ModelGatewayClient
    from app.core.rewrite_gateway import build_rewrite_gateway_settings

    s = get_settings()

    # ---- Config check ----
    print('--- Config ---')
    print(f'  rag_query_rewrite_enabled = {s.rag_query_rewrite_enabled}')
    print(f'  rag_query_rewrite_model  = {s.rag_query_rewrite_model!r}')
    print(f'  rewrite_gateway_base_url  = {s.rag_query_rewrite_gateway_base_url!r}')
    print(f'  rewrite_gateway_api_key   = {"***" if s.rag_query_rewrite_gateway_api_key else "NOT SET"}')
    print()

    # ---- Build rewrite gateway ----
    rs = build_rewrite_gateway_settings(s)
    if rs is None:
        print('  => Rewrite gateway NOT configured (URL or key missing)')
        print('  => Would fall back to runtime.gateway')
        return
    print(f'  Rewrite gateway base URL: {rs.model_gateway_base_url}')
    print(f'  Rewrite gateway timeout:  {rs.model_gateway_timeout_seconds}s')
    print()

    gw = ModelGatewayClient(settings=rs)

    # ---- Simulate observations ----
    from app.schemas.syndrome import SyndromeObservationContext

    observations = [
        SyndromeObservationContext(
            observation_id='550e8400-e29b-41d4-a716-446655440001',
            session_id='550e8400-e29b-41d4-a716-446655440000',
            state_version=1,
            fact_key='chief_complaint.symptom',
            value='受凉后咳嗽三天',
            normalized_value=None,
            status='active',
        ),
        SyndromeObservationContext(
            observation_id='550e8400-e29b-41d4-a716-446655440002',
            session_id='550e8400-e29b-41d4-a716-446655440000',
            state_version=1,
            fact_key='present_illness.cough',
            value='咳嗽',
            normalized_value=None,
            status='active',
        ),
        SyndromeObservationContext(
            observation_id='550e8400-e29b-41d4-a716-446655440003',
            session_id='550e8400-e29b-41d4-a716-446655440000',
            state_version=1,
            fact_key='present_illness.sputum',
            value='痰白稀',
            normalized_value=None,
            status='active',
        ),
        SyndromeObservationContext(
            observation_id='550e8400-e29b-41d4-a716-446655440004',
            session_id='550e8400-e29b-41d4-a716-446655440000',
            state_version=1,
            fact_key='present_illness.chills',
            value='怕冷明显',
            normalized_value=None,
            status='active',
        ),
        SyndromeObservationContext(
            observation_id='550e8400-e29b-41d4-a716-446655440005',
            session_id='550e8400-e29b-41d4-a716-446655440000',
            state_version=1,
            fact_key='present_illness.rhinorrhea',
            value='流清涕',
            normalized_value=None,
            status='active',
        ),
    ]

    # Set rewrite model to Qwen3.5-2B-free
    s.rag_query_rewrite_enabled = True
    if not s.rag_query_rewrite_model:
        s.rag_query_rewrite_model = 'Qwen3.5-2B-free'

    print(f'  Rewrite model: {s.rag_query_rewrite_model}')
    print()

    from app.rag.reasoning_retrieval import rewrite_syndrome_query, build_syndrome_query

    orig_query = build_syndrome_query(observations)
    print(f'  Original query ({len(orig_query)} chars):')
    print(f'    {orig_query}')
    print()

    print('  Calling rewrite_syndrome_query via dmxapi...')
    start = time.perf_counter()
    try:
        rewritten = await rewrite_syndrome_query(
            observations,
            gateway=gw,
            trace_id='p2-gateway-test',
        )
        latency = (time.perf_counter() - start) * 1000
        print(f'  Latency: {latency:.0f}ms')
        print(f'  Rewritten ({len(rewritten)} chars):')
        print(f'    {rewritten}')
        print()
        if rewritten != orig_query:
            print(f'  Verdict: REWRITE SUCCESS — output differs from key=value input')
        else:
            print(f'  Verdict: NO CHANGE — output same as input')
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        print(f'  Latency: {latency:.0f}ms')
        print(f'  Verdict: FAILED — {e}')
        import traceback
        traceback.print_exc()

    # Reset
    s.rag_query_rewrite_enabled = False
    print()
    print('=== DONE ===')


if __name__ == '__main__':
    asyncio.run(main())
