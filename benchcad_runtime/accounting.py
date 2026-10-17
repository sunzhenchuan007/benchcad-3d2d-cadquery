"""Usage totals and optional price estimates; never invent missing costs."""
import math


def summarize_usage(records, rates=None):
    known = [r for r in records if r is not None]
    totals = {'input_tokens':0, 'output_tokens':0, 'cached_input_tokens':0}
    complete = bool(records) and len(known)==len(records)
    for record in known:
        if not isinstance(record, dict):
            complete = False
            continue
        for key in ('input_tokens','output_tokens'):
            value = record.get(key)
            if type(value) is not int or value < 0:
                complete = False
            else:
                totals[key] += value
        details = record.get('input_tokens_details')
        cached = details.get('cached_tokens') if isinstance(details, dict) else None
        input_tokens = record.get('input_tokens')
        if (type(cached) is not int or cached < 0 or type(input_tokens) is not int
                or input_tokens < 0 or cached > input_tokens):
            complete = False
        else:
            totals['cached_input_tokens'] += cached
    estimate = None
    if rates is not None:
        for key in ('input_per_million','cached_input_per_million','output_per_million'):
            if type(rates.get(key)) not in (int,float) or not math.isfinite(rates[key]) or rates[key]<0:
                raise ValueError('Prices must be finite nonnegative numbers')
        if not rates.get('model') or not rates.get('date') or not rates.get('source_url'):
            raise ValueError('Pricing requires model, date and official source URL')
        if complete:
            estimate = ((totals['input_tokens']-totals['cached_input_tokens'])*rates['input_per_million']
                        +totals['cached_input_tokens']*rates['cached_input_per_million']
                        +totals['output_tokens']*rates['output_per_million'])/1e6
    return {'usage_complete':complete, 'known_usage_subtotal':totals if known else None,
            'estimated_api_fee_usd':estimate, 'actual_fee':None, 'pricing':rates}
