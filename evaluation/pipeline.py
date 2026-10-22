"""Controller-owned evaluation after submission; never part of model context."""
from __future__ import annotations

import json
from pathlib import Path
import time

from benchcad_runtime.artifacts import MAX_CAD_BYTES, read_regular, sha256, write_new
from benchcad_runtime.docker_executor import DockerExecutor, DockerLimits
from .interfaces import validate_iou_result


def evaluate_frozen(root: Path, reference: Path, bundle: Path, vendor: Path,
                    image: str, limits: DockerLimits, timeout: float = 400) -> dict:
    started = time.monotonic()
    stage = root / 'evaluation'
    stage.mkdir(mode=0o700, exist_ok=False)
    workspace, source = stage / 'workspace', stage / 'source'
    workspace.mkdir()
    source.mkdir()
    reference = reference.absolute()
    ref = read_regular(reference.parent, reference.name, MAX_CAD_BYTES)
    manifest = json.loads(read_regular(bundle, 'manifest.json', 100000))
    candidate = read_regular(bundle, 'model.step', MAX_CAD_BYTES)
    if (manifest.get('kind') != 'reconstruction'
            or manifest['files'].get('model.step') != {'sha256': sha256(candidate), 'bytes': len(candidate)}):
        raise ValueError('Frozen reconstruction checksum/size mismatch')
    write_new(workspace / 'reference.step', ref, readonly=True)
    write_new(workspace / 'candidate.step', candidate, readonly=True)
    # Explicit evaluator code, separate from every model workspace and tool snapshot.
    package = Path(__file__).resolve().parent
    for name in ('normalized_iou.py', 'scoring.py'):
        write_new(source / name, read_regular(package, name, 1024*1024), readonly=True)
    hashes = {}
    total = 0
    for path in sorted((vendor / 'envs').rglob('*.py')):
        name = path.relative_to(vendor)
        data = read_regular(vendor, name, 2*1024*1024, allow_empty=True)
        total += len(data)
        if len(hashes) >= 4096 or total > 64*1024*1024:
            raise ValueError('Evaluator source snapshot exceeds bounds')
        target = source / 'vendor' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        write_new(target, data, readonly=True)
        hashes[name.as_posix()] = sha256(data)
    if 'envs/common/part_metric.py' not in hashes:
        raise ValueError('Supply the pinned BenchCAD 2 evaluator checkout')
    write_new(stage / 'source-manifest.json', json.dumps(hashes, indent=2).encode())
    executor = DockerExecutor(workspace, stage / 'records', source, image, limits)
    result = None
    for label, filename in (('identity', 'reference.step'), ('candidate', 'candidate.step')):
        left = timeout - (time.monotonic() - started)
        if left <= 0:
            raise TimeoutError('Evaluation budget exhausted')
        execution = executor.run_command([
            '-I', '/opt/benchcad/normalized_iou.py', '/workspace/reference.step',
            '/workspace/' + filename, '--vendor', '/opt/benchcad/vendor',
            '--output', '/workspace/' + label + '.json'], left, stage / 'records' / label)
        if execution.timed_out:
            raise TimeoutError('Evaluation budget exhausted')
        if execution.exit_code:
            raise RuntimeError('Evaluator failed; see private execution records')
        result = json.loads(read_regular(workspace, label + '.json', 1024*1024))
        validate_iou_result(result)
        expected_candidate = ref if label == 'identity' else candidate
        if (result['reference']['sha256'] != sha256(ref)
                or result['candidate']['sha256'] != sha256(expected_candidate)):
            raise ValueError('Evaluator artifact hashes do not match submitted files')
        if label == 'identity' and (result['raw_iou'] != 1 or result['reconstruction_score_100'] != 100):
            raise ValueError('Identity control failed')
        write_new(stage / (label + '.json'), json.dumps(result, indent=2).encode(), readonly=True)
    return result


def complete_evaluation(summary: dict, root: Path, reference: Path | None,
                        vendor: Path | None, image: str, limits: DockerLimits,
                        timeout: float = 400) -> dict:
    """Called after both agent loops end; failures remain unscored, never zero."""
    result = {'metric': 'reconstruction_iou', 'status': 'not_evaluated', 'score': None}
    reconstruction = summary['stages'].get('reconstruction', {})
    started = time.monotonic()
    if vendor is None:
        result['reason'] = 'evaluation_not_configured'
    elif not reconstruction.get('submitted'):
        result.update(status='blocked', reason='reconstruction_not_submitted')
    elif reference is None:
        result.update(status='failed', reason='reference_not_configured')
    else:
        try:
            metric = evaluate_frozen(root, reference, Path(reconstruction['bundle']), vendor,
                                     image, limits, timeout)
            result.update(status='computed', score=metric['reconstruction_score_100']/100, score_scale='0..1',
                          reconstruction_score_100=metric['reconstruction_score_100'],
                          reason='independent_reconstruction_evaluated', evidence='evaluation/candidate.json',
                          **{k: metric[k] for k in ('raw_iou', 'primitive_baseline', 'corrected_iou', 'metric_version')})
        except Exception as exc:
            result.update(status='failed', reason='evaluation_infrastructure_failed', error_type=type(exc).__name__)
    summary['metrics']['reconstruction_iou'] = result
    summary['evaluation_seconds'] = time.monotonic() - started
    summary['total_wall_seconds'] = summary['wall_seconds'] + summary['evaluation_seconds']
    summary['generation_success'] = summary['success']
    summary['success'] = summary['generation_success'] and (vendor is None or result['status'] == 'computed')
    return summary
