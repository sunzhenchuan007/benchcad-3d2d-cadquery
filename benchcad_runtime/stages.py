"""Controller-owned source snapshots, submission freeze, and narrow stage transfer."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from .artifacts import (MAX_CAD_BYTES, MAX_PNG_BYTES, read_regular, sha256,
                        snapshot_png, validate_png, write_new)
from .docker_executor import DockerExecutor, DockerLimits
from .errors import SubmissionRejected

PACKAGE = Path(__file__).resolve().parent.parent / 'benchcad_cadquery'


def source_snapshot(destination: Path) -> Path:
    package = destination / 'benchcad_cadquery'
    package.mkdir(parents=True, exist_ok=False)
    for name in ('__init__.py', 'drawing.py', 'templates.py', 'worker.py', 'operations.py', 'inspection.py'):
        write_new(package / name, read_regular(PACKAGE, name, 1024 * 1024, allow_empty=True), readonly=True)
    return destination.resolve()


def prepare_workspace(stage: Path, kind: str) -> Path:
    if kind not in ('drawing', 'reconstruction'):
        raise ValueError('Unknown stage')
    workspace = stage / 'workspace'
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / 'input').mkdir()
    (workspace / 'output').mkdir()
    write_new(workspace / 'TASK.md', read_regular(PACKAGE, kind.upper() + '_TASK.md', 100000))
    guide = read_regular(PACKAGE, 'TOOLS.md', 100000).decode('utf-8')
    if kind == 'reconstruction':
        # Keep one canonical guide, but do not prompt reconstruction with drawing
        # state, section-operation logging or unavailable source-STEP workflows.
        common, drawing = guide.split('## Drawing from a STEP (drawing stage only)', 1)
        inspection = drawing.split('## Preview and DXF inspection (both stages)', 1)[1]
        guide = common + '## Preview and DXF inspection (both stages)' + inspection
    write_new(workspace / 'TOOLS.md', guide.encode('utf-8'))
    return workspace


def transfer_drawing(bundle: Path, workspace: Path) -> list[str]:
    """Only the DXF and PNG enter a NEW reconstruction workspace, never state/code/STEP."""
    expected = {'TASK.md', 'TOOLS.md', 'input', 'output'}
    if set(p.name for p in workspace.iterdir()) != expected:
        raise ValueError('Reconstruction workspace is not fresh')
    for name in ('input', 'output'):
        p = workspace / name
        if p.is_symlink() or not p.is_dir() or any(p.iterdir()):
            raise ValueError('Reconstruction input/output must be fresh directories')
    manifest = json.loads(read_regular(bundle, 'manifest.json', 100000))
    if manifest.get('kind') != 'drawing':
        raise ValueError('Expected a frozen drawing bundle')
    payloads = {}
    for name, bound in (('drawing.dxf', MAX_CAD_BYTES), ('drawing.png', MAX_PNG_BYTES)):
        data = read_regular(bundle, name, bound)
        if manifest['files'][name] != {'sha256': sha256(data), 'bytes': len(data)}:
            raise ValueError('Frozen artifact checksum/size mismatch')
        if name.endswith('.png'):
            validate_png(data)
        payloads[name] = data
    for name, data in payloads.items():
        write_new(workspace / 'input' / name, data)
    # Initial vision input uses the frozen file, not the agent-writable copy.
    return [str((bundle / 'drawing.png').resolve())]


class StageFinalizer:
    def __init__(self, kind: str, workspace: Path, stage: Path, source: Path,
                 image: str, limits: DockerLimits, execution_seconds: float = 300):
        if kind not in ('drawing', 'reconstruction'):
            raise ValueError('Unknown stage')
        self.kind, self.workspace, self.stage = kind, workspace, stage
        self.source, self.image, self.limits = source, image, limits
        self.execution_seconds = execution_seconds

    def __call__(self, timeout: float) -> str:
        start = time.monotonic()
        bundle = self.stage / 'bundle'
        if bundle.exists():
            raise FileExistsError('A finalized bundle is never overwritten')
        filename = 'drawing.dxf' if self.kind == 'drawing' else 'model.step'
        try:
            data = read_regular(self.workspace, 'output/' + filename, MAX_CAD_BYTES)
        except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
            raise SubmissionRejected('Expected a nonempty, regular output/' + filename
                                     + ' within the artifact size limit; links are not allowed.') from exc
        # Only the copied submission is visible to trusted validation, not the
        # agent's scripts, Python modules, state, logs, or original model.
        validation = self.stage / 'finalize' / uuid.uuid4().hex
        clean = validation / 'workspace'
        clean.mkdir(parents=True, exist_ok=False)
        write_new(clean / filename, data)
        executor = DockerExecutor(clean, validation / 'records', self.source, self.image, self.limits)
        result = executor.run_command(['-I', '/opt/benchcad/benchcad_cadquery/worker.py',
                                      'validate-' + self.kind],
                                     min(self.execution_seconds, timeout - (time.monotonic() - start)),
                                     validation / 'records' / 'validate')
        if result.exit_code == 2 and not result.timed_out:
            rejection = json.loads(read_regular(clean, 'rejection.json', 100000))
            if rejection.get('kind') != self.kind or not isinstance(rejection.get('reason'), str):
                raise RuntimeError('Invalid rejection record from validator')
            raise SubmissionRejected(rejection['reason'])
        if result.exit_code:
            raise RuntimeError('Validator failed without a recognized artifact rejection; see finalizer records')
        if read_regular(clean, filename, MAX_CAD_BYTES) != data:
            raise ValueError('Validator modified the submitted artifact')
        validation_record = json.loads(read_regular(clean, 'validation.json', 100000))
        if validation_record.get('kind') != self.kind:
            raise ValueError('Invalid validator result')
        files = {filename: data}
        if self.kind == 'drawing':
            png = read_regular(clean, 'drawing.png', MAX_PNG_BYTES)
            validate_png(png)
            files['drawing.png'] = png
        if time.monotonic() - start >= timeout:
            raise TimeoutError('Stage deadline exceeded during finalization')
        if self.kind == 'drawing':
            # Capture bytes outside the agent workspace. Conversion/geometry
            # verification is evaluator-owned and does not enter reconstruction.
            evidence = self.stage / 'evidence'
            evidence.mkdir(exist_ok=False)
            captured = {}
            for name in ('drawing.operations.jsonl', 'drawing.state.json'):
                try:
                    payload = read_regular(self.workspace, 'output/' + name, 16*1024*1024)
                    write_new(evidence / name, payload, readonly=True)
                    captured[name] = {'sha256': sha256(payload), 'bytes': len(payload)}
                except (OSError, ValueError) as exc:
                    captured[name] = {'status': 'unavailable', 'error_type': type(exc).__name__}
            write_new(evidence / 'capture.json', json.dumps({'dxf_sha256': sha256(data),
                      'files': captured, 'content_verified': False}).encode(), readonly=True)
        manifest = {'schema': 'benchcad-frozen-bundle/1', 'kind': self.kind,
                    'files': {name: {'sha256': sha256(value), 'bytes': len(value)}
                              for name, value in files.items()},
                    'validation': validation_record, 'working_state': 'excluded_untrusted',
                    'formal_score': False}
        # Exclusive publication, after validation. No subsequent controller writes.
        bundle.mkdir(exist_ok=False)
        for name, value in files.items():
            write_new(bundle / name, value, readonly=True)
        write_new(bundle / 'manifest.json', json.dumps(manifest, indent=2).encode(), readonly=True)
        return str(bundle.resolve())


def initial_observation(image: Path, records: Path) -> str:
    data = read_regular(image.parent, image.name, MAX_PNG_BYTES)
    return str(snapshot_png(data, records / 'initial_observations').resolve())
