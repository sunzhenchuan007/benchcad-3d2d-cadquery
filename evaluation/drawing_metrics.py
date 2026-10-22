"""Versioned, unit-scale preference and multiplicative scoring policy."""
from __future__ import annotations

import math

VERSION = 'drawing-product/3'
MAJOR = {'projection', 'full_section', 'half_section'}
LOCAL = {'local_section', 'detail'}


def unit(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('Expected a finite score in [0,1]')
    return float(value)


def aggregate(metrics):
    names = ('reconstruction_iou', 'expert_preference', 'annotation_collision')
    missing = [n for n in names if metrics.get(n, {}).get('status') != 'computed']
    metrics['overall_score'] = None if missing else math.prod(unit(metrics[n]['score']) for n in names)
    metrics['overall_status'] = 'not_evaluated' if missing else 'computed'
    metrics['overall_missing'] = missing
    metrics['score_scale'] = '0..1'
    metrics['score_policy'] = VERSION
    return metrics


def vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('Expected XYZ')
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in value):
        raise ValueError('Invalid XYZ')
    return value


def direction(value):
    v = vector(value)
    size = math.hypot(*v)
    if abs(size - 1) > 1e-6:
        raise ValueError('Expected unit direction')
    return v


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def minus(a, b):
    return [x-y for x, y in zip(a, b)]


def matches(plane, target):
    """Infinite-axis containment, or intersection station along a unit axis."""
    p, n = vector(plane['point_mm']), direction(plane['normal'])
    tol = target['position_tolerance_mm']
    if type(tol) not in (int, float) or not math.isfinite(tol) or tol <= 0:
        raise ValueError('Invalid position tolerance')
    mode = target['mode']
    if mode in ('contains_axis', 'contains_lines'):
        lines = [target['axis']] if mode == 'contains_axis' else target['lines']
        if mode == 'contains_lines' and len(lines) != 2:
            raise ValueError('Two lines required')
        passed = all(abs(dot(n, direction(a['direction']))) <= 1e-6 and
                     abs(dot(n, minus(vector(a['origin_mm']), p))) <= tol for a in lines)
    elif mode == 'crosses_axis_at_station':
        axis = target['axis']; o, d = vector(axis['origin_mm']), direction(axis['direction'])
        denom = dot(n, d)
        station = target['station_mm']
        if type(station) not in (int, float) or not math.isfinite(station):
            raise ValueError('Invalid station')
        passed = abs(denom) > 1e-6 and abs(dot(n, minus(p, o))/denom - station) <= tol
    else:
        raise ValueError('Unsupported target mode')
    angle = target.get('angle_rule', {'mode': 'unrestricted'})
    if angle['mode'] == 'plane_normal':
        expected = direction(angle['normal']); degrees = angle['tolerance_deg']
        if type(degrees) not in (int, float) or not math.isfinite(degrees) or not 0 <= degrees <= 90:
            raise ValueError('Invalid angular tolerance')
        passed = passed and math.degrees(math.acos(min(1., abs(dot(n, expected))))) <= degrees + 1e-9
    elif angle['mode'] != 'unrestricted':
        raise ValueError('Unsupported angle rule')
    return passed


def count_penalty(major, local, reference):
    gm, gl = reference['major_views'], reference['local_sections']
    for number in (major, local, gm, gl):
        if type(number) is not int or number < 0:
            raise ValueError('Counts must be nonnegative integers')
    total = gm + gl
    if total == 0:
        raise ValueError('Expert total view count is unset/zero')
    allowance = int(major == gm and local > gl)
    excess = max(0, major + local - total - allowance)
    return {'excess': excess, 'local_allowance': allowance, 'deduction': excess/total}


def score_expert(candidate, expert, *, evidence_verified=False):
    result = {'metric': 'expert_preference', 'status': 'not_evaluated', 'score': None,
              'score_scale': '0..1', 'metric_version': VERSION}
    if not evidence_verified:
        return {**result, 'reason': 'final_view_evidence_not_verified'}
    if expert.get('schema_version') != 'benchcad-section-position-draft/5':
        raise ValueError('Expert v5 JSON with view_policy is required')
    for key in ('case_id', 'input_sha256', 'units'):
        if not candidate.get(key) or candidate[key] != expert.get(key):
            raise ValueError('Candidate/expert source, case or units differ')
    if candidate['units'] != 'mm' or candidate.get('coordinate_frame') != 'source_step':
        raise ValueError('Expected source STEP coordinates in mm')
    views = candidate['views']; ids = [v['id'] for v in views]
    if len(set(ids)) != len(ids) or any(v['kind'] not in MAJOR | LOCAL for v in views):
        raise ValueError('Unclassified or duplicate final view')
    major = sum(v['kind'] in MAJOR for v in views); local = len(views)-major
    policy = expert['view_policy']; schemes = policy['reference_schemes']
    if not schemes:
        return {**result, 'reason': 'expert_reference_counts_missing'}
    comparisons = [{'id': s['id'], **count_penalty(major, local, s['counts'])} for s in schemes]
    best = min(comparisons, key=lambda s: s['deduction'])
    groups = {g['id']: [] for g in policy['required_groups']}
    if len(groups) != len(policy['required_groups']):
        raise ValueError('Duplicate required group')
    requirements = []; missing = []; seen = set()
    for row in expert['sections']:
        name = row['id']; req = row['requirement']; kind = req['kind']
        if name in seen or kind not in ('optional', 'required', 'one_of'):
            raise ValueError('Invalid expert requirement')
        seen.add(name)
        if kind == 'optional':
            continue
        if row.get('target') is None:
            # Do not silently count a required ordinary/auxiliary view as a missed section.
            return {**result, 'reason': 'required_nonsection_matching_not_configured'}
        hits = [s['view_id'] for s in candidate['sections'] if matches(s['plane'], row['target'])]
        requirements.append({'id': name, 'matched_views': hits, 'met': bool(hits)})
        if kind == 'required' and not hits:
            missing.append({'kind': 'required', 'id': name})
        if kind == 'one_of':
            if req.get('group_id') not in groups:
                raise ValueError('Unknown required group')
            groups[req['group_id']].append(bool(hits))
    for name, hits in groups.items():
        if not hits:
            raise ValueError('Empty required alternative group')
        if not any(hits):
            missing.append({'kind': 'one_of', 'id': name})
    deduction = .2*len(missing)
    return {**result, 'status': 'computed', 'reason': 'verified_final_views_compared',
            'score': max(0., 1-best['deduction']-deduction),
            'counts': {'major_views': major, 'local_sections': local},
            'reference_comparisons': comparisons, 'selected_reference': best['id'],
            'count_deduction': best['deduction'], 'missing_requirement_deduction': deduction,
            'missing_requirements': missing, 'requirement_matches': requirements}
