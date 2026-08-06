"""Quick reranker connectivity test."""
import requests, json

BASE = 'https://www.dmxapi.cn/v1'
KEY = 'sk-nddHCp7Tf8JlhsohBPFR3zJ5PVGfLw0HKdLNuiXckFNObHDk'

payload = {
    'model': 'jina-reranker-m0',
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
