"""Host-side CLI for a custom Responses agent; never tests a Codex/CLI agent."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import tomllib

from benchcad_runtime.accounting import summarize_usage
from benchcad_runtime.artifacts import MAX_CAD_BYTES, read_regular, write_new
from benchcad_runtime.docker_executor import DockerExecutor, DockerLimits
from benchcad_runtime.episode import Budget, ReplayModel, run_episode
from benchcad_runtime.local_accounting import _load_encoding, summarize_local_usage
from benchcad_runtime.openai_api import OpenAIResponses
from benchcad_runtime.stages import (PACKAGE, StageFinalizer, prepare_workspace,
                                     source_snapshot, transfer_drawing)
from evaluation.interfaces import pending_metrics


def system_prompt(kind: str, budget: Budget) -> str:
    target = 'output/drawing.dxf' if kind == 'drawing' else 'output/model.step'
    return (f'You are the {kind} agent in a CAD development pilot. '
            f'Maximum {budget.rounds} decision responses, {budget.wall_seconds:g} seconds for this stage, '
            f'and {budget.execution_seconds:g} seconds per execution, limited by remaining stage time. '
            'Reply with exactly one nonempty fenced python or submit block and no other text. '
            'Each block is Python executed in a fresh Linux container; only workspace files persist. '
            'A submit block executes Python and requests validation. Failed submit code or rejected artifacts return feedback for repair within the SAME remaining rounds/time. Only successful validation ends submission. '
            'No explicit submit means no answer. Feedback includes bounded stdout/stderr and changed '
            'PNG observations saved at workspace root or directly in output/. Read TASK.md and TOOLS.md. '
            f'Save {target}. Scores, ground truth, and expert references are unavailable. '
            + ('Use only the supplied DXF/PNG to infer the solid; there is no source STEP in this stage.'
               if kind == 'reconstruction' else 'Drawing is rendered automatically on submission.'))


def stage_episode(kind, root, source, args, model, step=None, drawing_bundle=None):
    stage = root / kind
    workspace = prepare_workspace(stage, kind)
    initial_images = []
    if kind == 'drawing':
        step = Path(step).absolute()
        write_new(workspace / 'input/model.step', read_regular(step.parent, step.name, MAX_CAD_BYTES))
        if getattr(args, 'template', None) is not None:
            template = Path(args.template).absolute()
            write_new(workspace / 'input/template.dxf',
                      read_regular(template.parent, template.name, MAX_CAD_BYTES))
    else:
        initial_images = transfer_drawing(Path(drawing_bundle), workspace)
    limits = DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes)
    executor = DockerExecutor(workspace, stage / 'executions', source, args.image, limits)
    budget = Budget(args.rounds, args.wall_seconds, args.execution_seconds, args.max_transport_failures)
    finalizer = StageFinalizer(kind, workspace, stage, source, args.image, limits, args.execution_seconds)
    task = (workspace / 'TASK.md').read_text(encoding='utf-8')
    # Embed the tool guide so the first decision has a self-contained contract.
    task += '\n\n' + (workspace / 'TOOLS.md').read_text(encoding='utf-8')
    return run_episode(model, executor, finalizer, task, stage / 'episode', budget,
                       system_prompt=system_prompt(kind, budget), initial_images=initial_images)


def summarize(stages: dict, elapsed: float, smoke: bool, model: str | None) -> dict:
    local = [r for stage in stages.values() for r in stage['local_usage_records']]
    provider = [r for stage in stages.values() for r in stage['usage_records']]
    return {'schema': 'benchcad-server-pilot/1', 'status': 'development_pilot',
            'model': model, 'trusted_replay': smoke, 'expert_baseline_enabled': False,
            'wall_seconds': elapsed,
            'stages': {name: {key: value[key] for key in
                       ('submitted', 'ended_by', 'decision_rounds', 'code_executions', 'wall_seconds', 'bundle')}
                       for name, value in stages.items()},
            'usage_metric': 'local_visible_text', 'local_visible_text': summarize_local_usage(local),
            'provider_usage_diagnostic': summarize_usage(provider), 'actual_fee': None,
            'metrics': pending_metrics(Path('.'))}


def run(args, smoke=False):
    start = time.monotonic()
    root = args.run_dir.absolute()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)  # exclusive and private
    source = source_snapshot(root / 'runtime-source')
    stages = {}
    error = None
    try:
        if args.command == 'reconstruct':
            _load_encoding()
            model = OpenAIResponses(args.model, args.max_output_tokens, args.reasoning_effort,
                                    args.base_url)
            stages['reconstruction'] = stage_episode('reconstruction', root, source, args, model,
                                                     drawing_bundle=args.drawing_bundle)
        elif smoke:
            fixture = root / 'fixture'
            fixture.mkdir()
            limits = DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes)
            executor = DockerExecutor(fixture, root / 'fixture-records', source, args.image, limits)
            result = executor.run_command(['-I', '/opt/benchcad/benchcad_cadquery/worker.py', 'fixture'],
                                          args.execution_seconds, root / 'fixture-records/create')
            if result.exit_code:
                raise RuntimeError('Trusted fixture creation failed; see fixture records')
            step = fixture / 'model.step'
            code = (PACKAGE / 'plate_example.py').read_text(encoding='utf-8')
            drawing_model = ReplayModel(['```submit\n' + code + '\n```'])
        else:
            _load_encoding()  # fail before any API call; never download a vocabulary here
            drawing_model = OpenAIResponses(args.model, args.max_output_tokens, args.reasoning_effort,
                                             args.base_url)
            step = args.step
        if args.command != 'reconstruct':
            stages['drawing'] = stage_episode('drawing', root, source, args, drawing_model, step=step)
        if getattr(args, 'reconstruct', False) and stages.get('drawing', {}).get('submitted'):
            if smoke:
                # Deliberately known fixture dimensions: plumbing replay, NOT an AI reconstruction score.
                code = "import cadquery as cq\nm=cq.Workplane('XY').box(60,40,8).faces('>Z').workplane().hole(10)\ncq.exporters.export(m, 'output/model.step')"
                model = ReplayModel(['```submit\n' + code + '\n```'])
            else:
                model = OpenAIResponses(args.model, args.max_output_tokens, args.reasoning_effort, args.base_url)
            stages['reconstruction'] = stage_episode('reconstruction', root, source, args, model,
                                                     drawing_bundle=stages['drawing']['bundle'])
    except Exception as exc:
        # Avoid copying provider/OS exception messages or env values to public diagnostics.
        error = type(exc).__name__
    summary = summarize(stages, time.monotonic()-start, smoke, None if smoke else args.model)
    if error:
        summary['controller_error_type'] = error
    expected = ({'reconstruction'} if args.command == 'reconstruct' else
                {'drawing', 'reconstruction'} if args.reconstruct else {'drawing'})
    summary['success'] = not error and set(stages) == expected and all(s['submitted'] for s in stages.values())
    write_new(root / 'summary.json', json.dumps(summary, indent=2).encode('utf-8'))
    print(json.dumps(summary, indent=2))
    return 0 if summary['success'] else 1


def doctor(args):
    root = args.run_dir.absolute()
    root.mkdir(parents=True, exist_ok=False)
    source = source_snapshot(root / 'runtime-source')
    workspace = root / 'workspace'
    workspace.mkdir()
    checks = {'linux_host': sys.platform.startswith('linux'), 'nonroot': hasattr(os, 'getuid') and os.getuid() != 0,
              'controller_python': sys.version.split()[0], 'offline_tokenizer': False}
    try:
        _load_encoding()
        checks['offline_tokenizer'] = True
    except Exception as exc:
        checks['tokenizer_error_type'] = type(exc).__name__
    if checks['linux_host'] and checks['nonroot']:
        try:
            executor = DockerExecutor(workspace, root / 'records', source, args.image)
            result = executor.run_command(['-I', '/opt/benchcad/benchcad_cadquery/worker.py', 'doctor'],
                                          60, root / 'records/check')
            checks['worker'] = json.loads(result.stdout) if result.exit_code == 0 else {'exit_code': result.exit_code}
            checks['worker_ok'] = result.exit_code == 0
        except Exception as exc:
            checks['worker_error_type'] = type(exc).__name__
    checks['success'] = all(checks.get(k) for k in ('linux_host', 'nonroot', 'offline_tokenizer', 'worker_ok'))
    write_new(root / 'doctor.json', json.dumps(checks, indent=2).encode())
    print(json.dumps(checks, indent=2))
    return 0 if checks['success'] else 1


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('doctor', 'smoke', 'run', 'reconstruct'):
        s = sub.add_parser(command)
        s.add_argument('--run-dir', type=Path, required=True)
        s.add_argument('--image', default=None, help='Already built Linux Docker image (no implicit pull)')
        s.add_argument('--config', type=Path, help='Explicit TOML only; no automatic local-config search')
        if command == 'doctor':
            continue
        if command != 'reconstruct':
            s.add_argument('--reconstruct', action=argparse.BooleanOptionalAction, default=command == 'smoke')
        for flag, typ in (('rounds', int), ('wall-seconds', float), ('execution-seconds', float),
                          ('max-transport-failures', int), ('memory-mib', int), ('cpus', float),
                          ('pids', int), ('log-bytes', int)):
            s.add_argument('--' + flag, type=typ, default=None)
        if command in ('run', 'reconstruct'):
            if command == 'run':
                s.add_argument('--step', type=Path, required=True)
                s.add_argument('--template', type=Path,
                               help='Curated blank millimetre DXF template, drawing stage only')
            else:
                s.add_argument('--drawing-bundle', type=Path, required=True)
            s.add_argument('--model', default=None, help='Exact parent-supplied model ID; no fallback')
            s.add_argument('--base-url', default=None)
            s.add_argument('--max-output-tokens', type=int, default=None)
            s.add_argument('--reasoning-effort', default=None)
    return p


def configure(args):
    config = {}
    if args.config:
        with args.config.open('rb') as stream:
            config = tomllib.load(stream)
    if config.get('executor', {}).get('backend', 'docker') != 'docker':
        raise ValueError('This pilot supports only the Docker backend')
    if config.get('api', {}).get('protocol', 'responses') != 'responses':
        raise ValueError('This pilot supports only the Responses API')
    if config.get('api', {}).get('api_key_env', 'OPENAI_API_KEY') != 'OPENAI_API_KEY':
        raise ValueError('Only OPENAI_API_KEY is supported')
    sections = {'executor': {'image': 'benchcad-cadquery:local', 'memory_mib': 4096,
                            'cpus': 1, 'pids': 128, 'log_bytes': 8*1024*1024},
                'budget': {'rounds': 30, 'wall_seconds': 1800, 'execution_seconds': 300, 'max_transport_failures': 3},
                'api': {'model': '', 'base_url': None, 'max_output_tokens': 8192, 'reasoning_effort': None}}
    for section, defaults in sections.items():
        for key, value in defaults.items():
            if getattr(args, key, None) is None:
                setattr(args, key, config.get(section, {}).get(key, value))
    if args.command != 'doctor':
        Budget(args.rounds, args.wall_seconds, args.execution_seconds, args.max_transport_failures)
        DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes)
        if args.rounds > 30 or args.wall_seconds > 1800 or args.execution_seconds > 300:
            raise ValueError('Pilot maxima: 30 decisions, 1800 stage seconds, 300 execution seconds')
    if args.command in ('run', 'reconstruct'):
        if not args.model or not args.model.strip():
            raise ValueError('Supply an exact --model or api.model; there is no default or fallback')
        if args.command == 'run' and (not args.step.is_file() or args.step.is_symlink()):
            raise ValueError('--step must be a regular STEP file')
        if args.command == 'run' and args.template is not None:
            if not args.template.is_file() or args.template.is_symlink():
                raise ValueError('--template must be a regular DXF template file')
        if args.command == 'reconstruct' and (not args.drawing_bundle.is_dir() or args.drawing_bundle.is_symlink()):
            raise ValueError('--drawing-bundle must be a frozen drawing bundle directory')
        args.base_url = args.base_url or os.environ.get('BASE_URL') or os.environ.get('OPENAI_BASE_URL')
    return args


def main(argv=None):
    p = parser()
    try:
        args = configure(p.parse_args(argv))
        if not sys.platform.startswith('linux'):
            p.error('Execution is supported on Linux only. Windows is for development/unit tests.')
        return doctor(args) if args.command == 'doctor' else run(args, smoke=args.command == 'smoke')
    except (ValueError, OSError) as exc:
        # Generic message avoids exposing paths or external diagnostic payloads.
        p.exit(2, f'Configuration/filesystem failure ({type(exc).__name__}); check arguments and use a new run directory.\n')


if __name__ == '__main__':
    raise SystemExit(main())
