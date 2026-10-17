"""Replay declared drawing operations. This is not geometric verification.

Only the structured protocol below is supported; arbitrary Python, stdout and
CAD kernel calls cannot be reconstructed by this converter. No input label can
grant trusted status. Keep this module on the controller side when integrating.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path

EVENT_SCHEMA = 'benchcad-operation-event/1'
OUTPUT_SCHEMA = 'benchcad-section-observations/1'
MAX_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 10000
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('Missing or unsupported fields')


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('Invalid identifier')
    return value


def _sha(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError('Expected a lowercase SHA256')
    return value


def _integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError('Expected a positive integer')
    return value


def _vector(value, direction=False):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('Expected XYZ triple')
    try:
        if any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
            raise ValueError('Expected finite numeric coordinates')
        result = [float(x) for x in value]
    except OverflowError as exc:
        raise ValueError('Coordinates exceed floating-point range') from exc
    if direction:
        # Scaling first avoids overflow while normalizing finite large vectors.
        scale = max(abs(x) for x in result)
        if scale == 0:
            raise ValueError('Direction must be nonzero')
        result = [x / scale for x in result]
        norm = math.hypot(*result)
        result = [x / norm for x in result]
    return result


def _view(value):
    _keys(value, ('kind',), ('plane', 'parent_view_id'))
    if value['kind'] not in ('projection', 'full_section'):
        raise ValueError('Unsupported view kind; half/offset/local sections need a future schema')
    result = {'kind': value['kind']}
    if value.get('parent_view_id') is not None:
        result['parent_view_id'] = _id(value['parent_view_id'])
    if value['kind'] == 'full_section':
        plane = value.get('plane')
        _keys(plane, ('point', 'normal', 'keep_side'))
        if type(plane['keep_side']) is not int or plane['keep_side'] not in (-1, 1):
            raise ValueError('keep_side must be -1 or +1')
        result['plane'] = {'point': _vector(plane['point']),
                           'normal': _vector(plane['normal'], True),
                           'keep_side': plane['keep_side']}
    elif 'plane' in value:
        raise ValueError('Projection cannot declare a section plane')
    return result


def _references(views):
    for start in views:
        seen = set()
        current = start
        while current is not None:
            if current not in views:
                raise ValueError('Unknown parent view')
            if current in seen:
                raise ValueError('Cyclic parent view reference')
            seen.add(current)
            current = views[current].get('parent_view_id')


def event(seq, operation, payload, *, round_number=None):
    """Build an event for a future tool/controller adapter; does not attest it."""
    result = {'schema': EVENT_SCHEMA, 'seq': seq, 'operation': operation, 'payload': payload}
    if round_number is not None:
        result['round'] = round_number
    return result


class OperationRecorder:
    """Append-only event writer for a future controlled tool boundary.

    Invoke *after* an operation succeeds. Files inside the agent workspace are
    agent-writable, regardless of this class or the declared producer name.
    A controller must own the recorder destination to protect captured bytes.
    """
    def __init__(self, path, metadata):
        self.path = Path(path)
        self.events = [parse_json(json.dumps(event(1, 'log.started', metadata), allow_nan=False))]
        replay(self.events)
        with self.path.open('xb') as stream:
            self._write(stream, self.events[0])

    @staticmethod
    def _write(stream, item):
        stream.write((json.dumps(item, allow_nan=False, separators=(',', ':')) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())

    def append(self, operation, payload, *, round_number=None):
        item = parse_json(json.dumps(event(len(self.events) + 1, operation, payload,
                                          round_number=round_number), allow_nan=False))
        updated = [*self.events, item]
        replay(updated)
        if self.path.is_symlink():
            raise ValueError('Recorder destination became a symlink')
        with self.path.open('ab') as stream:
            self._write(stream, item)
        self.events = updated
        return item

    def drawing_view_created(self, view, *, round_number=None):
        """Adapt the metadata returned by Drawing.projection/full_section."""
        record = {'kind': view.get('type')}
        if record['kind'] == 'full_section':
            record['plane'] = {**view['plane'], 'keep_side': view['keep_side']}
        return self.append('view.created', {'view_id': view['id'], 'view': record},
                           round_number=round_number)


def replay(events, *, final_dxf_sha256=None):
    """Replay a complete declared stream; reject incomplete/inconsistent changes.

    final_dxf_sha256 should come from the controller's frozen artifact, not a
    value returned by the model. Equality is still only a byte-version match.
    """
    views, used_ids, history = {}, set(), []
    header = exported = None
    last_round = 0
    for seq, item in enumerate(events, 1):
        if seq > MAX_EVENTS:
            raise ValueError('Too many operation events')
        _keys(item, ('schema', 'seq', 'operation', 'payload'), ('round',))
        if item['schema'] != EVENT_SCHEMA or _integer(item['seq']) != seq:
            raise ValueError('Unexpected schema or non-contiguous sequence')
        if 'round' in item:
            current_round = _integer(item['round'])
            if current_round < last_round:
                raise ValueError('Round numbers must not decrease')
            last_round = current_round
        op, payload = item['operation'], item['payload']
        if seq == 1:
            if op != 'log.started':
                raise ValueError('First event must be log.started')
            _keys(payload, ('case_id', 'source_sha256', 'units', 'coordinate_frame', 'producer', 'coverage'))
            _id(payload['case_id'])
            _sha(payload['source_sha256'])
            if payload['units'] not in ('mm', 'cm', 'm', 'in'):
                raise ValueError('Unsupported source length units')
            if payload['coordinate_frame'] != 'source_step':
                raise ValueError('Only source_step coordinates are supported; transform first')
            if payload['producer'] not in ('agent_declaration', 'tool_adapter', 'controller_capture', 'working_state_adapter'):
                raise ValueError('Unsupported producer declaration')
            if payload['coverage'] not in ('structured_calls_only', 'working_state_snapshot'):
                raise ValueError('Unsupported coverage declaration')
            header = dict(payload)
        elif exported is not None:
            raise ValueError('No events allowed after artifact.exported')
        elif op in ('view.created', 'view.updated'):
            required = ('view_id', 'view') if op == 'view.created' else ('view_id', 'view', 'expected_revision')
            _keys(payload, required)
            name = _id(payload['view_id'])
            view = _view(payload['view'])
            if op == 'view.created':
                if name in used_ids:
                    raise ValueError('View IDs cannot be reused, even after deletion')
                revision = 1
            else:
                if name not in views or _integer(payload['expected_revision']) != views[name]['revision']:
                    raise ValueError('Unknown view or stale revision')
                revision = views[name]['revision'] + 1
            candidate = {**views, name: {**view, 'id': name, 'revision': revision,
                                       'last_event_seq': seq}}
            _references(candidate)
            views = candidate
            used_ids.add(name)
        elif op == 'view.deleted':
            _keys(payload, ('view_id', 'expected_revision'))
            name = _id(payload['view_id'])
            if name not in views or _integer(payload['expected_revision']) != views[name]['revision']:
                raise ValueError('Unknown view or stale revision')
            candidate = {k: v for k, v in views.items() if k != name}
            _references(candidate)
            views = candidate
        elif op == 'artifact.exported':
            _keys(payload, ('dxf_sha256',))
            exported = _sha(payload['dxf_sha256'])
        else:
            raise ValueError('Unsupported operation')
        history.append({'seq': seq, 'operation': op, 'round': item.get('round'),
                        'view_id': payload.get('view_id')})
    if header is None:
        raise ValueError('Operation stream is empty')
    actual = _sha(final_dxf_sha256) if final_dxf_sha256 is not None else None
    binding = 'not_checked' if actual is None else ('missing_export_event' if exported is None else
               ('matches_frozen_bytes' if actual == exported else 'mismatch'))
    source = {'sha256': header['source_sha256'], 'units': header['units'],
              'coordinate_frame': header['coordinate_frame']}
    final_views = [views[name] for name in sorted(views)]
    mm_factor = {'mm': 1, 'cm': 10, 'm': 1000, 'in': 25.4}[header['units']]
    sections = [{'view_id': v['id'],
                 'plane': {'point_mm': _vector([x * mm_factor for x in v['plane']['point']]),
                           'normal': v['plane']['normal']},
                 'keep_side': v['plane']['keep_side'],
                 'view': {'type': 'full_section', 'category': 'major_views'},
                 'evidence': {'kind': 'declared_operation', 'seq': v['last_event_seq'],
                              'revision': v['revision']}}
                for v in final_views if v['kind'] == 'full_section']
    # The whole output uses mm. Do not leave a second plane in source units
    # under views while the root declares mm.
    for view in final_views:
        if 'plane' in view:
            plane = view['plane']
            view['plane'] = {'point_mm': _vector([x * mm_factor for x in plane['point']]),
                             'normal': plane['normal'], 'keep_side': plane['keep_side']}
    return {'schema': OUTPUT_SCHEMA, 'case_id': header['case_id'],
            'input_sha256': header['source_sha256'], 'units': 'mm',
            'coordinate_frame': 'source_step', 'source': source, 'views': final_views, 'sections': sections,
            'audit': history,
            'provenance': {'producer_claim': header['producer'], 'coverage_claim': header['coverage'],
                           'evidence_status': 'unverified_declaration',
                           'geometry_verified': False, 'dxf_consistency_verified': False,
                           'capture_integrity_verified': False,
                           'arbitrary_python_covered': False},
            'artifact_binding': {'declared_dxf_sha256': exported, 'observed_dxf_sha256': actual,
                                 'status': binding},
            'section_match': {'status': 'not_evaluated', 'score': None}, 'formal_score': False}


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def parse_json(data):
    def reject_constant(value):
        raise ValueError('Non-finite JSON constant')
    return json.loads(data, object_pairs_hook=_no_duplicates, parse_constant=reject_constant)


def read_jsonl(data: bytes):
    if len(data) > MAX_BYTES:
        raise ValueError('Operation log exceeds byte limit')
    events = []
    for line in data.decode('utf-8').splitlines():
        if not line.strip():
            raise ValueError('Blank lines are not part of the event protocol')
        events.append(parse_json(line))
        if len(events) > MAX_EVENTS:
            raise ValueError('Too many operation events')
    return events


def convert_working_state(state, *, case_id, units='mm', coordinate_frame='source_step', final_dxf_sha256=None):
    """Explicit adapter for Drawing.save() snapshots; no operation history exists.

    Never infer unsupported section types or infer a plane from a projection.
    Only curated geometric fields enter output; raw state stays private.
    """
    if not isinstance(state, dict) or state.get('schema') != 'benchcad-cadquery-working-state/1':
        raise ValueError('Unsupported working state schema')
    events = [event(1, 'log.started', {'case_id': case_id, 'source_sha256': state.get('source_sha256'),
              'units': units, 'coordinate_frame': coordinate_frame,
              'producer': 'working_state_adapter', 'coverage': 'working_state_snapshot'})]
    if not isinstance(state.get('views'), list):
        raise ValueError('Working state views must be an array')
    for view in state['views']:
        if not isinstance(view, dict):
            raise ValueError('Working state view must be an object')
        record = {'kind': view.get('type')}
        if record['kind'] == 'full_section':
            plane = view.get('plane')
            if not isinstance(plane, dict):
                raise ValueError('Section plane is missing')
            record['plane'] = {'point': plane.get('point'), 'normal': plane.get('normal'),
                               'keep_side': view.get('keep_side')}
        events.append(event(len(events) + 1, 'view.created', {'view_id': view.get('id'), 'view': record}))
    if 'dxf_sha256' in state:
        events.append(event(len(events) + 1, 'artifact.exported', {'dxf_sha256': state['dxf_sha256']}))
    result = replay(events, final_dxf_sha256=final_dxf_sha256)
    result['provenance']['history_available'] = False
    for section in result['sections']:
        section['evidence']['kind'] = 'agent_writable_working_state'
        section['evidence']['seq'] = None
        section['evidence']['revision'] = None
    for view in result['views']:
        view.pop('last_event_seq', None)
        view.pop('revision', None)
    # Synthetic adapter events must never be mistaken for actual operation timing.
    result['audit'] = []
    return result


def read_bounded(path: Path, limit=MAX_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular non-symlink file')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Input file exceeds byte limit')
    return data


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def prepare_comparison(candidate, expert):
    """Check the v5 expert/candidate contract, without matching or scoring.

    Call only on the evaluator side. Expert IDs/order are independent of view
    IDs/order. Expert fields are not copied to candidate artifacts or AI input.
    """
    if not isinstance(candidate, dict) or not isinstance(expert, dict):
        raise ValueError('Candidate and expert must be objects')
    if candidate.get('schema') != OUTPUT_SCHEMA:
        raise ValueError('Unsupported candidate schema')
    if expert.get('schema_version') != 'benchcad-section-position-draft/5':
        raise ValueError('Expert draft v5 required; migrate older drafts with the annotation tool')
    for key in ('case_id', 'input_sha256', 'units'):
        if not candidate.get(key) or candidate[key] != expert.get(key):
            raise ValueError('Candidate and expert case/source/units differ')
    _id(candidate['case_id'])
    _sha(candidate['input_sha256'])
    if candidate['units'] != 'mm' or candidate.get('coordinate_frame') != 'source_step':
        raise ValueError('Comparison needs source STEP coordinates in mm')
    # v5 has no coordinate_frame field: the annotation tool's points are in the
    # source STEP frame. Accept an explicit declaration only if it agrees.
    if expert.get('coordinate_frame', 'source_step') != 'source_step':
        raise ValueError('Expert coordinate frame differs')
    if expert.get('status') != 'draft':
        raise ValueError('Unexpected expert v5 status')
    policy = expert.get('view_policy')
    if not isinstance(policy, dict) or not isinstance(policy.get('required_groups'), list) or not isinstance(policy.get('reference_schemes'), list):
        raise ValueError('Expert view policy is missing')
    groups = set()
    def expert_id(row):
        # The existing expert editor accepts arbitrary nonempty ID strings.
        # Do not impose the operation protocol's ASCII ID restriction on GT.
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise ValueError('Expert ID must be a nonempty string')
        return row['id']
    for group in policy['required_groups']:
        name = expert_id(group)
        if name in groups:
            raise ValueError('Duplicate required group')
        groups.add(name)
    if not isinstance(expert.get('sections'), list) or not isinstance(candidate.get('sections'), list):
        raise ValueError('Sections must be arrays')
    ids = set()
    for row in expert['sections']:
        name = expert_id(row)
        if name in ids:
            raise ValueError('Duplicate expert ID')
        ids.add(name)
        requirement = row.get('requirement', {})
        if not isinstance(requirement, dict):
            raise ValueError('Expert requirement must be an object')
        if requirement.get('kind') not in ('optional', 'required', 'one_of'):
            raise ValueError('Unsupported expert requirement')
        if requirement['kind'] == 'one_of' and requirement.get('group_id') not in groups:
            raise ValueError('Unknown required group')
        target = row.get('target')
        if target is None:
            if row.get('view', {}).get('type') in ('full_section', 'half_section', 'local_section'):
                raise ValueError('Expert section target missing')
            continue
        if not isinstance(target, dict):
            raise ValueError('Expert target must be an object')
        mode = target.get('mode')
        if mode not in ('contains_axis', 'crosses_axis_at_station', 'contains_lines'):
            raise ValueError('Unsupported expert target')
        tolerance = target.get('position_tolerance_mm')
        if type(tolerance) not in (float, int) or not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError('Invalid position tolerance')
        if mode == 'contains_lines':
            lines = target.get('lines')
            if not isinstance(lines, list) or len(lines) != 2:
                raise ValueError('Expected two expert lines')
        else:
            lines = [target.get('axis')]
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError('Missing axis/line')
            _vector(line.get('origin_mm'))
            direction = _vector(line.get('direction'))
            if abs(math.hypot(*direction) - 1) > 1e-6:
                raise ValueError('Expert axis direction must be unit length')
        if mode == 'crosses_axis_at_station':
            station = target.get('station_mm')
            if type(station) not in (float, int) or not math.isfinite(station):
                raise ValueError('Invalid station')
        angle = target.get('angle_rule', {})
        if not isinstance(angle, dict):
            raise ValueError('Expert angle rule must be an object')
        if angle.get('mode') not in ('unrestricted', 'plane_normal'):
            raise ValueError('Unsupported angle rule')
        if mode == 'contains_lines' and angle['mode'] != 'unrestricted':
            raise ValueError('Two-line target requires unrestricted angle rule')
        if angle['mode'] == 'plane_normal':
            normal = _vector(angle.get('normal'))
            tolerance = angle.get('tolerance_deg')
            if abs(math.hypot(*normal) - 1) > 1e-6 or type(tolerance) not in (float, int) or not math.isfinite(tolerance) or not 0 <= tolerance <= 90:
                raise ValueError('Invalid plane angle rule')
    candidate_ids = set()
    for row in candidate['sections']:
        if not isinstance(row, dict):
            raise ValueError('Candidate section must be an object')
        name = _id(row.get('view_id'))
        if name in candidate_ids:
            raise ValueError('Duplicate candidate view ID')
        candidate_ids.add(name)
        plane = row.get('plane', {})
        if not isinstance(plane, dict):
            raise ValueError('Candidate plane must be an object')
        _vector(plane.get('point_mm'))
        normal = _vector(plane.get('normal'))
        if abs(math.hypot(*normal) - 1) > 1e-6:
            raise ValueError('Candidate normal must be unit length')
    return {'status': 'compatible_inputs_matching_not_implemented',
            'expert_schema_version': expert['schema_version'],
            'matching_basis': 'geometric_constraints_not_ids_or_array_order',
            'evidence_eligible': False, 'score': None, 'formal_score': False}
