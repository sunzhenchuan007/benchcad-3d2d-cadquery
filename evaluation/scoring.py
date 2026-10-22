"""BenchCAD primitive-baseline correction and the user-defined 0..100 scale."""
import math


VERSION = 'normalized-iou48-primitive-threshold/3'


def reconstruction_score(raw_iou, baseline):
    for value in (raw_iou, baseline):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError('IoU and baseline must be finite numbers in [0, 1]')
    if baseline >= 1:
        corrected = 1.0 if raw_iou >= 1 else 0.0
    else:
        corrected = min(1.0, max(0.0, (raw_iou - baseline) / (1 - baseline)))
    if corrected <= .90:
        score = 0.0
    elif corrected >= .995:
        score = 100.0
    else:
        score = 100 * (corrected - .90) / (.995 - .90)
    return {'raw_iou': float(raw_iou), 'primitive_baseline': float(baseline),
            'corrected_iou': corrected, 'score': score / 100, 'score_scale': '0..1',
            'reconstruction_score_100': score,
            'score_policy': {'input': 'corrected_iou', 'zero_at_or_below': .90,
                             'full_at_or_above': .995, 'interpolation': 'linear',
                             'baseline_one_rule': 'one_only_if_raw_iou_is_one'}}
