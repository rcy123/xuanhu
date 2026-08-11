"""Quick reranker connectivity test (secrets read from env only)."""
import json
import os
import sys
from typing import Any

import requests

KEY_ENV = 'RERANKER_GATEWAY_API_KEY'
BASE_ENV = 'RERANKER_GATEWAY_BASE_URL'
MODEL_ENV = 'RAG_RERANKER_MODEL'

KEY = os.environ.get(KEY_ENV, '').strip()
if not KEY:
    sys.exit(
        f'{KEY_ENV} 未设置：请从环境（或 .env）提供 reranker 网关 API 密钥后再运行。'
    )

# 网关地址与模型名按既定配置口径读取；未设置时给出默认值，但不涉及密钥。
BASE = os.environ.get(BASE_ENV, '').strip() or 'https://www.dmxapi.cn/v1'
MODEL = os.environ.get(MODEL_ENV, '').strip() or 'jina-reranker-m0'

payload: dict[str, Any] = {
    'model': MODEL,
    'query': '患者咳嗽痰白稀，恶寒发热，麻黄汤证',
    'documents': [
        '麻黄汤治疗风寒感冒，发汗解表，宣肺平喘',
        '小青龙汤治疗外寒内饮，咳嗽痰多清稀',
        '足三里为胃经合穴，主治脾胃虚弱',
        '肝郁气滞证应疏肝理气，用柴胡疏肝散',
    ],
    'top_n': 4,
}

try:
    r = requests.post(
        f'{BASE}/rerank',
        headers={'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=10
    )
    print(f'Status: {r.status_code}')
    data = r.json()
    if 'results' in data:
        results = sorted(data['results'], key=lambda x: x['index'])
        print(f'Returned {len(results)} scores:')
        for item in results:
            idx = item['index']
            doc = payload['documents'][idx]
            score = item.get('relevance_score', item.get('score', '?'))
            print(f'  [{idx}] score={score:.4f}  ->  {doc[:60]}')

        best_idx = results[0]['index']
        print(f'\nBest match: doc[{best_idx}] (should be doc[0] = 麻黄汤)')
        if best_idx == 0:
            print('Verdict: PASS — correct ranking')
        else:
            print(f'Verdict: UNEXPECTED — doc[{best_idx}] ranked first')
    else:
        print(f'Unexpected response: {json.dumps(data, indent=2)[:300]}')
except Exception as e:
    print(f'Connection FAILED: {e}')
