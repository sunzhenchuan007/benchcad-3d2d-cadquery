"""Offline operation conversion; retain raw input in a new private output dir."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchcad_runtime.operation_log import (convert_working_state, parse_json,
    read_bounded, read_jsonl, replay, sha256)


def convert(source: Path, output_dir: Path, *, working_state=False, dxf=None, units='mm', case_id=None):
    raw = read_bounded(source)
    digest = sha256(read_bounded(dxf, 256 * 1024 * 1024)) if dxf else None
    result = (convert_working_state(parse_json(raw), case_id=case_id, units=units, final_dxf_sha256=digest)
              if working_state else replay(read_jsonl(raw), final_dxf_sha256=digest))
    filename = 'raw-working-state.json' if working_state else 'raw-operations.jsonl'
    result['provenance']['raw_input'] = {'filename': filename, 'sha256': sha256(raw), 'bytes': len(raw)}
    # New directory only, so neither old evidence nor prior results are overwritten.
    output_dir = Path(output_dir)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    payloads = {filename: raw, 'sections.json': json.dumps(
        result, indent=2, ensure_ascii=False, allow_nan=False).encode('utf-8')}
    for name, data in payloads.items():
        # POSIX: owner-only directory/files from creation, without a public gap.
        # Windows mode bits are not an ACL isolation guarantee.
        descriptor = os.open(output_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--working-state', action='store_true', help='Explicitly convert untrusted Drawing state')
    parser.add_argument('--case-id', help='Required for working-state mode, e.g. case_013')
    parser.add_argument('--dxf', type=Path, help='Frozen DXF bytes to compare; does not verify geometry')
    parser.add_argument('--units', choices=('mm', 'cm', 'm', 'in'), default='mm', help='Working-state source units only')
    args = parser.parse_args()
    try:
        result = convert(args.source, args.output_dir, working_state=args.working_state,
                         dxf=args.dxf, units=args.units, case_id=args.case_id)
    except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        # Never echo input contents or local absolute paths into shareable output.
        parser.exit(2, f'Conversion failed ({type(exc).__name__}); inspect private input locally.\n')
    print(json.dumps({'sections': len(result['sections']),
                      'evidence_status': result['provenance']['evidence_status'],
                      'dxf_binding': result['artifact_binding']['status'],
                      'scoring_eligible': False}))


if __name__ == '__main__':
    main()
