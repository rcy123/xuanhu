"""Reranker Cross-Encoder real-backend evaluation (standalone)."""
import asyncio
import json
import sys
import time
from io import TextIOWrapper
from typing import Any, cast

cast(TextIOWrapper, sys.stdout).reconfigure(encoding='utf-8', errors='replace')


async def main() -> None:
    print('=== Reranker Cross-Encoder Real-Backend Evaluation ===')
    print()

    from app.core.config import get_settings
    from app.core.gateway import ModelGatewayClient
    from app.core.reranker_gateway import build_reranker_gateway_settings
    from app.rag.reranker import cross_encoder_rerank, rerank
    from app.rag.retriever import RAGRetriever
    from app.rag.schemas import MergedHit

    s = get_settings()
    rs = build_reranker_gateway_settings(s)
    print(f"Reranker gateway URL: {rs.model_gateway_base_url}")
    print(f"Reranker API key configured: {bool(getattr(rs, 'model_gateway_api_key', ''))}")
    print()

    reranker_gw = ModelGatewayClient(settings=rs)
    retriever = RAGRetriever()

    test_queries = [
        ('风寒咳嗽加减', '患者受凉后咳嗽三天，痰白稀，怕冷，麻黄汤证，应如何加减用药？'),
        ('胸胁胀痛方剂', '患者胸胁胀痛一周，情志不舒，嗳气频作，脉弦，应用什么方剂？'),
        ('脾胃虚弱穴位', '足三里用于脾胃虚弱时的定位和主治是什么？'),
    ]

    results: list[dict[str, Any]] = []
    for label, query in test_queries:
        print(f"--- [{label}] ---")
        print(f"Query: {query[:100]}...")

        candidates = await retriever.hybrid_search(
            query=query, sources=['formula', 'herb', 'case', 'theory'], top_k=12,
        )
        if not candidates:
            print('  No candidates')
            continue
        print(f'  Candidates: {len(candidates)}')

        merged = []
        for ev in candidates:
            hit = MergedHit(
                source_type=ev.source_type, source_id=ev.source_id, chunk_id=cast(str, ev.chunk_id),
                title=ev.title, content_snippet=ev.content_snippet,
                vector_score=ev.metadata.get('vector_score', 0.7),
                fulltext_score=ev.metadata.get('fulltext_score', 0.5),
                is_primary=True,
            )
            merged.append(hit)

        # ---- MVP ----
        mvp_start = time.perf_counter()
        mvp_evidence = rerank(merged, top_k=5)
        mvp_latency = (time.perf_counter() - mvp_start) * 1000
        mvp_titles = [e.title for e in mvp_evidence]
        mvp_scores = [e.score for e in mvp_evidence]
        mvp_short = [(t[:50], round(s, 3)) for t, s in zip(mvp_titles, mvp_scores, strict=False)]
        print(f"  MVP ({mvp_latency:.0f}ms): {mvp_short}")

        # ---- Cross-Encoder ----
        ce_titles, ce_scores, ce_latency = None, None, None
        try:
            ce_start = time.perf_counter()
            ce_evidence = await cross_encoder_rerank(
                query=query, merged_hits=merged, gateway=reranker_gw,
                model='jina-reranker-m0', top_k=5, timeout=10.0,
            )
            ce_latency = (time.perf_counter() - ce_start) * 1000
            ce_titles = [e.title for e in ce_evidence]
            ce_scores = [e.score for e in ce_evidence]
            provider = ce_evidence[0].metadata.get('reranker_provider', '?') if ce_evidence else '?'
            ce_short = [(t[:50], round(s, 3)) for t, s in zip(ce_titles, ce_scores, strict=False)]
            print(f"  CE ({ce_latency:.0f}ms, {provider}): {ce_short}")
        except Exception as e:
            print(f"  CE FAILED: {e}")
            import traceback
            traceback.print_exc()

        # Compare
        if ce_titles:
            mvp_set = set(mvp_titles[:3])
            ce_set = set(ce_titles[:3])
            overlap = len(mvp_set & ce_set)
            order_match = sum(
                1 for i in range(min(3, len(mvp_titles), len(ce_titles)))
                if mvp_titles[i] == ce_titles[i]
            )
            print(f"  MVP vs CE: top-3 overlap={overlap}/3, order_match={order_match}/3")

        results.append({
            'label': label, 'query': query[:150],
            'mvp': {'titles': mvp_titles, 'scores': mvp_scores, 'latency_ms': round(mvp_latency, 1)},
            'cross_encoder': {
                'titles': ce_titles, 'scores': ce_scores,
                'latency_ms': round(ce_latency, 1),
            } if ce_latency else None,
        })
        print()

    print('=' * 60)
    print('RERANKER SUMMARY')
    print('=' * 60)
    for r in results:
        mvp = r['mvp']
        ce = r['cross_encoder']
        print(f"  [{r['label']}]")
        print(f"    MVP: {mvp['latency_ms']}ms, top-3 scores={[round(s,3) for s in mvp['scores'][:3]]}")
        if ce:
            print(f"    CE:  {ce['latency_ms']}ms, top-3 scores={[round(s,3) for s in ce['scores'][:3]]}")

    with open('scripts/op3_eval_reranker.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\nSaved to scripts/op3_eval_reranker.json")


if __name__ == '__main__':
    asyncio.run(main())
