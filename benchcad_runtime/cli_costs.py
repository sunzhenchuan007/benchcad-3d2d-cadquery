"""Offline, per-stage API-equivalent estimates from retained CLI usage."""
from decimal import Decimal
import html
import json
from pathlib import Path
import re

SOURCE = 'https://developers.openai.com/api/docs/models/gpt-6.1-sol'
CHECKED = '2026-10-18'
RATES = {'input': '2.00', 'cached_input': '0.10', 'cache_write': '2.50', 'output': '10.00'}
KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')


def estimate_stage(folder):
    folder = Path(folder)
    trial_path, bridge_path = folder / 'trial.json', folder / 'bridge-summary.json'
    result = {'status': 'unavailable', 'estimated_usd': None}
    if not trial_path.is_file() or not bridge_path.is_file():
        return result
    trial = json.loads(trial_path.read_text(encoding='utf-8'))
    bridge = json.loads(bridge_path.read_text(encoding='utf-8'))
    if not bridge.get('submitted'):
        return {**result, 'reason': 'successful_submission_required'}
    if trial.get('model') != 'gpt-6.1-sol':
        return {**result, 'reason': 'model_price_not_verified'}
    usage = trial.get('usage')
    if not isinstance(usage, list) or not usage:
        return {**result, 'reason': 'usage_not_recorded'}
    counts = dict.fromkeys(KEYS, 0)
    for row in usage:
        if not isinstance(row, dict) or any(k not in row for k in KEYS):
            return {**result, 'reason': 'incomplete_usage'}
        if any(type(row[k]) is not int or row[k] < 0 for k in KEYS):
            return {**result, 'reason': 'invalid_usage'}
        if row['cached_input_tokens'] + row['cache_write_input_tokens'] > row['input_tokens']:
            return {**result, 'reason': 'invalid_input_breakdown'}
        for k in KEYS:
            counts[k] += row[k]
    regular = counts['input_tokens'] - counts['cached_input_tokens'] - counts['cache_write_input_tokens']
    components = {'input': regular, 'cached_input': counts['cached_input_tokens'],
                  'cache_write': counts['cache_write_input_tokens'], 'output': counts['output_tokens']}
    amount = sum(Decimal(v) * Decimal(RATES[k]) for k, v in components.items()) / Decimal(1_000_000)
    return {'status': 'estimated', 'model': trial['model'], 'usage': counts,
            'uncached_nonwrite_input_tokens': regular, 'estimated_usd': float(amount),
            'usage_source': 'trial.json:usage', 'actual_subscription_charge': None}


def generate(root):
    root = Path(root)
    data = {'schema': 'benchcad-cli-stage-costs/1', 'currency': 'USD',
            'pricing_source': SOURCE, 'pricing_checked_on': CHECKED,
            'rates_per_million': RATES, 'pricing_basis': 'Standard / short context',
            'scope': 'Selected successful drawing and reconstruction, reported separately; no total',
            'limitations': ['API-equivalent estimate, not a subscription charge',
                            'No request-level long-context surcharge verification',
                            'Reasoning output is already included in output tokens',
                            'Interrupted attempts and evaluator compute are excluded'],
            'stages': {'drawing': estimate_stage(root),
                       'reconstruction': estimate_stage(root / 'reconstruction')}}
    (root / 'api-cost-estimate.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def money(stage):
    amount = stage.get('estimated_usd')
    return f'${amount:.4f}' if amount is not None else '待统计'


def section(data):
    rows = ''.join('<tr><td>' + label + '</td><td>' + money(data['stages'][name]) + '</td></tr>'
                   for name, label in [('drawing', '出图'), ('reconstruction', '独立重建')])
    return ('<!-- api-stage-cost:start --><section id="api-stage-cost"><h2>费用估算</h2>'
            '<table><tr><th>阶段</th><th>等价 API 费用（美元）</th></tr>' + rows + '</table>'
            '<p>只统计本报告采用的成功出图和成功重建，分别列示。不是 Codex 订阅实际扣费。</p>'
            '<p>按 GPT-6.1 Sol 标准短上下文价格估算：每百万 token，普通输入 $2、缓存输入 $0.10、'
            '缓存写入 $2.50、输出 $10。缓存输入不重复按普通输入收费；推理 token 已包含在输出中。</p>'
            '<p>CLI 汇总未提供逐请求上下文长度，未核验超过 272K 输入的长上下文附加费。'
            '中断尝试、服务器计算及评分重试不计入此表。</p>'
            '<p><a href="' + html.escape(SOURCE) + '">官方定价</a>（核对日期：' + CHECKED + '） · '
            '<a href="api-cost-estimate.json">用量与计算依据</a></p></section><!-- api-stage-cost:end -->')


def update_report(root):
    root = Path(root)
    data = generate(root)
    path = root / 'index.html'
    if not path.exists():
        return data
    page = path.read_text(encoding='utf-8')
    page = re.sub(r'<!-- api-stage-cost:start -->.*?<!-- api-stage-cost:end -->', '', page, flags=re.S)
    marker = '<section><h2>最终图纸</h2>'
    if marker in page:
        page = page.replace(marker, section(data) + marker, 1)
    elif '</main>' in page:
        page = page.replace('</main>', section(data) + '</main>', 1)
    else:
        page += section(data)
    path.write_text(page, encoding='utf-8')
    return data
