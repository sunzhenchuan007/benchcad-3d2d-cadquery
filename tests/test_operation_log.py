import json
import contextlib
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from benchcad_runtime.operation_log import (OperationRecorder, convert_working_state,
    event, prepare_comparison, read_jsonl, replay, sha256)
from tools.operations_to_json import convert, main


def metadata(**overrides):
    return dict(case_id='case_demo', source_sha256='a'*64, units='mm',
                coordinate_frame='source_step', producer='tool_adapter',
                coverage='structured_calls_only', **overrides)


def section(point=None):
    return {'kind': 'full_section', 'plane': {'point': point or [0, 0, 0],
            'normal': [0, 0, 2], 'keep_side': -1}}


def log():
    return [event(1, 'log.started', metadata()),
            event(2, 'view.created', {'view_id': 'A', 'view': section()}, round_number=1)]


def expert():
    return {'schema_version': 'benchcad-section-position-draft/5', 'status': 'draft',
            'case_id': 'case_demo', 'input_sha256': 'a'*64, 'units': 'mm',
            'view_policy': {'required_groups': [], 'reference_schemes': []},
            'sections': [{'id': 'expert_other_id', 'view': {'type': 'full_section', 'category': 'major_views'},
                          'requirement': {'kind': 'required'},
                          'target': {'mode': 'contains_axis',
                                     'axis': {'origin_mm': [0, 0, 0], 'direction': [1, 0, 0]},
                                     'position_tolerance_mm': .1,
                                     'angle_rule': {'mode': 'unrestricted'}}}]}


class OperationLogTests(unittest.TestCase):
    def test_update_delete_replay_final_only(self):
        events = log()
        events += [event(3, 'view.updated', {'view_id': 'A', 'expected_revision': 1, 'view': section([2, 3, 4])}),
                   event(4, 'view.created', {'view_id': 'B', 'view': {'kind': 'projection'}}),
                   event(5, 'view.deleted', {'view_id': 'B', 'expected_revision': 1})]
        result = replay(events)
        self.assertEqual(result['sections'][0]['plane'], {'point_mm': [2., 3., 4.], 'normal': [0., 0., 1.]})
        self.assertEqual(result['sections'][0]['evidence']['revision'], 2)
        self.assertEqual(len(result['views']), 1)
        self.assertEqual(len(result['audit']), 5)
        self.assertIsNone(result['section_match']['score'])

    def test_stale_unknown_or_reused_view_rejected(self):
        for extra in [event(3, 'view.updated', {'view_id': 'A', 'expected_revision': 2, 'view': section()}),
                      event(3, 'view.deleted', {'view_id': 'missing', 'expected_revision': 1}),
                      event(3, 'view.created', {'view_id': 'A', 'view': section()})]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                replay(log() + [extra])
        with self.assertRaises(ValueError):
            replay(log() + [event(3, 'view.deleted', {'view_id': 'A', 'expected_revision': 1}),
                            event(4, 'view.created', {'view_id': 'A', 'view': section()})])

    def test_sequence_and_round_monotonic(self):
        for key, value in [('seq', 3), ('seq', True), ('round', 0)]:
            events = log(); events[1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                replay(events)
        events = log(); events[1]['round'] = 2
        events.append(event(3, 'view.deleted', {'view_id': 'A', 'expected_revision': 1}, round_number=1))
        with self.assertRaises(ValueError):
            replay(events)

    def test_finite_nonzero_normal_and_keep_side(self):
        for key, value in [('normal', [0, 0, 0]), ('normal', [0, float('nan'), 1]),
                           ('point', [float('inf'), 0, 0]), ('point', [10**400, 0, 0]), ('point', [True, 0, 0]),
                           ('keep_side', True), ('keep_side', 0)]:
            events = log(); events[1]['payload']['view']['plane'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                replay(events)

    def test_large_normal_normalizes_without_overflow(self):
        events = log(); events[1]['payload']['view']['plane']['normal'] = [1e308, 1e308, 0]
        result = replay(events)
        self.assertAlmostEqual(result['sections'][0]['plane']['normal'][0], 2**-.5)

    def test_reference_cycle_and_parent_delete_rejected(self):
        events = log()
        events.append(event(3, 'view.created', {'view_id': 'B', 'view': {'kind': 'projection', 'parent_view_id': 'A'}}))
        for payload, operation in [({'view_id': 'A', 'expected_revision': 1}, 'view.deleted'),
                ({'view_id': 'A', 'expected_revision': 1, 'view': {'kind': 'projection', 'parent_view_id': 'B'}}, 'view.updated')]:
            with self.assertRaises(ValueError):
                replay(events + [event(4, operation, payload)])

    def test_bad_plane_or_type_not_silently_dropped(self):
        for view in [{'kind': 'half_section'}, {'kind': 'projection', 'plane': section()['plane']},
                     {'kind': 'projection', 'parent_view_id': 'missing'}]:
            events = log(); events[1]['payload']['view'] = view
            with self.assertRaises(ValueError):
                replay(events)

    def test_hash_match_is_not_attestation(self):
        events = log() + [event(3, 'artifact.exported', {'dxf_sha256': 'b'*64})]
        events[0]['payload']['producer'] = 'controller_capture'
        result = replay(events, final_dxf_sha256='b'*64)
        self.assertEqual(result['artifact_binding']['status'], 'matches_frozen_bytes')
        self.assertFalse(result['provenance']['capture_integrity_verified'])
        self.assertFalse(result['provenance']['geometry_verified'])
        self.assertEqual(result['provenance']['evidence_status'], 'unverified_declaration')
        self.assertEqual(replay(events, final_dxf_sha256='c'*64)['artifact_binding']['status'], 'mismatch')
        self.assertEqual(replay(log(), final_dxf_sha256='b'*64)['artifact_binding']['status'], 'missing_export_event')
        with self.assertRaises(ValueError):
            replay(events + [event(4, 'view.deleted', {'view_id': 'A', 'expected_revision': 1})])

    def test_forbidden_claims_rejected(self):
        events = log(); events[1]['verified'] = True
        with self.assertRaises(ValueError):
            replay(events)

    def test_json_duplicate_nonfinite_and_blank_lines(self):
        for raw in [b'{"seq":1,"seq":2}', b'{"seq":NaN}', b'\n']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                read_jsonl(raw)

    def test_working_state_adapter_no_fake_history(self):
        state = {'schema': 'benchcad-cadquery-working-state/1', 'source_sha256': 'a'*64,
                 'views': [{'id': 'A', 'type': 'full_section', 'plane': {'point': [0, 1, 0], 'normal': [0, 0, 1]},
                            'keep_side': -1, 'insert_handle': 'F2'}],
                 'dxf_sha256': 'b'*64, 'section_evidence': 'verified_by_agent'}
        result = convert_working_state(state, case_id='case_demo')
        self.assertEqual(result['audit'], [])
        self.assertFalse(result['provenance']['history_available'])
        self.assertEqual(result['sections'][0]['evidence']['kind'], 'agent_writable_working_state')
        self.assertNotIn('F2', json.dumps(result))
        self.assertNotIn('verified_by_agent', json.dumps(result))

    def test_raw_bytes_preserved_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'operations.jsonl'
            raw = ('\n'.join(json.dumps(x) for x in log()) + '\n').encode()
            source.write_bytes(raw)
            result = convert(source, root / 'converted')
            self.assertEqual((root/'converted/raw-operations.jsonl').read_bytes(), raw)
            self.assertEqual(result['provenance']['raw_input']['sha256'], sha256(raw))
            with self.assertRaises(FileExistsError):
                convert(source, root/'converted')

    def test_recorder_adapts_actual_drawing_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'ops.jsonl'
            recorder = OperationRecorder(path, metadata())
            recorder.drawing_view_created({'id': 'A', 'type': 'full_section',
                'plane': {'point': (0, 0, 0), 'normal': (0, 1, 0)}, 'keep_side': -1,
                'insert_handle': '10', 'section_area_mm2': 123}, round_number=2)
            recorder.append('artifact.exported', {'dxf_sha256': 'b'*64}, round_number=2)
            result = replay(read_jsonl(path.read_bytes()))
            self.assertEqual(len(result['sections']), 1)
            with self.assertRaises(ValueError):
                recorder.append('view.deleted', {'view_id': 'A', 'expected_revision': 1})
            self.assertEqual(len(read_jsonl(path.read_bytes())), 3)

    def test_gt_contract_mapping_units_and_independent_ids(self):
        events = log(); events[0]['payload']['units'] = 'm'
        events[1]['payload']['view']['plane']['point'] = [0, .03, 0]
        candidate = replay(events)
        self.assertEqual(candidate['sections'][0]['plane']['point_mm'], [0, 30, 0])
        self.assertEqual(candidate['views'][0]['plane']['point_mm'], [0, 30, 0])
        self.assertNotIn('point', candidate['views'][0]['plane'])
        result = prepare_comparison(candidate, expert())
        self.assertIsNone(result['score'])
        self.assertFalse(result['evidence_eligible'])

    def test_gt_wrong_source_and_bad_target_rejected(self):
        for key, value in [('input_sha256', 'b'*64), ('case_id', 'another'), ('units', 'm'),
                           ('schema_version', 'benchcad-section-position-draft/2')]:
            gt = expert(); gt[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepare_comparison(replay(log()), gt)
        gt = expert(); gt['sections'][0]['target']['mode'] = 'guessed_axis'
        with self.assertRaises(ValueError):
            prepare_comparison(replay(log()), gt)

    def test_gt_supported_rules_not_compared_as_text(self):
        for mode in ['crosses_axis_at_station', 'contains_lines']:
            gt = expert(); target = gt['sections'][0]['target']; target['mode'] = mode
            if mode == 'crosses_axis_at_station':
                target['station_mm'] = 30
                target['angle_rule'] = {'mode': 'plane_normal', 'normal': [1, 0, 0], 'tolerance_deg': 2}
            else:
                target['lines'] = [target.pop('axis'), {'origin_mm': [0, 20, 0], 'direction': [1, 0, 0]}]
            self.assertIsNone(prepare_comparison(replay(log()), gt)['score'])

    def test_existing_expert_unicode_ids_are_compatible(self):
        gt = expert()
        gt['sections'][0]['id'] = '剖切要求 A'
        gt['view_policy']['required_groups'] = [{'id': '替代组 一', 'label': '组'}]
        gt['sections'][0]['requirement'] = {'kind': 'one_of', 'group_id': '替代组 一'}
        self.assertFalse(prepare_comparison(replay(log()), gt)['evidence_eligible'])

    def test_missing_or_empty_log_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'missing.jsonl'; dest = root/'converted'
            with self.assertRaises(ValueError):
                convert(source, dest)
            source.write_bytes(b'')
            with self.assertRaises(ValueError):
                convert(source, dest)
            self.assertFalse(dest.exists())

    def test_cli_mismatch_is_visible_and_never_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'operations.jsonl'; drawing = root/'drawing.dxf'
            drawing.write_bytes(b'synthetic DXF bytes, not CAD validation')
            events = log() + [event(3, 'artifact.exported', {'dxf_sha256': 'b'*64})]
            source.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
            output = io.StringIO()
            with patch('sys.argv', ['operations_to_json', str(source), '--dxf', str(drawing),
                                   '--output-dir', str(root/'converted')]), contextlib.redirect_stdout(output):
                main()
            summary = json.loads(output.getvalue())
            self.assertEqual(summary['dxf_binding'], 'mismatch')
            self.assertFalse(summary['scoring_eligible'])
            saved = json.loads((root/'converted/sections.json').read_text())
            self.assertEqual(saved['artifact_binding']['status'], 'mismatch')
            self.assertEqual(saved['provenance']['evidence_status'], 'unverified_declaration')

    def test_private_directory_mode_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'operations.jsonl'
            source.write_text('\n'.join(json.dumps(e) for e in log()), encoding='utf-8')
            mkdir = Path.mkdir
            calls = []
            def observed_mkdir(path, *args, **kwargs):
                calls.append((path, kwargs))
                return mkdir(path, *args, **kwargs)
            with patch.object(Path, 'mkdir', observed_mkdir):
                convert(source, root/'converted')
            self.assertEqual(calls[0][1]['mode'], 0o700)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions require Linux/macOS')
    def test_posix_private_modes_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'operations.jsonl'
            source.write_text('\n'.join(json.dumps(e) for e in log()), encoding='utf-8')
            dest = root/'converted'; convert(source, dest)
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode) & 0o077, 0)
            for child in dest.iterdir():
                self.assertEqual(stat.S_IMODE(child.stat().st_mode) & 0o077, 0)


if __name__ == '__main__':
    unittest.main()
