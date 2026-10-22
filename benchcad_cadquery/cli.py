"""Host-side CLI for a custom Responses agent; never tests a Codex/CLI agent."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import tomllib
from contextlib import nullcontext

from benchcad_runtime.accounting import summarize_usage
from benchcad_runtime.artifacts import MAX_CAD_BYTES, read_regular, write_new
from benchcad_runtime.docker_executor import DockerExecutor, DockerLimits
from benchcad_runtime.episode import Budget, ReplayModel, run_episode
from benchcad_runtime.local_accounting import _load_encoding, summarize_local_usage
from benchcad_runtime.openai_api import OpenAIResponses
from benchcad_runtime.native_tools import GUIDANCE, PROTOCOL
from benchcad_runtime.stages import (PACKAGE, StageFinalizer, prepare_workspace,
                                     source_snapshot, transfer_drawing)
from evaluation.interfaces import pending_metrics
from evaluation.pipeline import complete_evaluation
from evaluation.quality_pipeline import complete_quality
from benchcad_runtime.report import save_summary_and_report


def system_prompt(kind: str, budget: Budget, native_tools=False) -> str:
    target = 'output/drawing.dxf' if kind == 'drawing' else 'output/model.step'
    if native_tools:
        return (f'You are the {kind} agent. Maximum {budget.rounds} decision responses, '
                f'{budget.wall_seconds:g} seconds for this stage, {budget.execution_seconds:g} seconds '
                'per Python execution, limited by remaining stage time. '
                + GUIDANCE + ' Each Python execution uses a fresh Linux container; files persist. '
                'Feedback includes stdout/stderr and changed PNGs from workspace root or directly in output/. '
                'Read TASK.md and TOOLS.md; code examples there demonstrate libraries, not reply syntax. '
                f'Save {target}. No scores, ground truth, or expert references are available. '
                'Keep reusable code and brief notes in workspace files. Execution success is not correctness. '
                + ('Infer geometry only from supplied DXF/PNG; there is no source STEP in this stage.'
                   if kind == 'reconstruction' else 'Drawing is rendered automatically on submission.'))
    return (f'You are the {kind} agent in a CAD development pilot. '
            f'Maximum {budget.rounds} decision responses, {budget.wall_seconds:g} seconds for this stage, '
            f'and {budget.execution_seconds:g} seconds per execution, limited by remaining stage time. '
            'Reply with exactly one nonempty fenced python or submit block and no other text. '
            'Each response executes as one Python program in a fresh Linux container; only workspace files persist. '
            'A submit block executes Python and requests validation. Failed submit code or rejected artifacts return feedback for repair within the SAME remaining rounds/time. Only successful validation ends submission. '
            'No explicit submit means no answer. Feedback includes bounded stdout/stderr and changed '
            'PNG observations saved at workspace root or directly in output/. Read TASK.md and TOOLS.md. '
            f'Save {target}. Scores, ground truth, and expert references are unavailable. '
            + ('Use only the supplied DXF/PNG to infer the solid; there is no source STEP in this stage. '
               'Save reusable modeling code and brief working notes in workspace files; edit and rerun them across rounds. '
               'Use execution errors to repair the program. Valid STEP or exit code 0 does not mean correct geometry. '
               'Check the artifact against the input requirements before submitting; choose the checks yourself. '
               'Submit the saved, checked artifact rather than rewriting the model in the submit block.'
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
    budget = Budget(args.rounds, args.wall_seconds, args.execution_seconds, args.max_transport_failures,
                    args.max_consecutive_output_limits)
    finalizer = StageFinalizer(kind, workspace, stage, source, args.image, limits, args.execution_seconds)
    task = (workspace / 'TASK.md').read_text(encoding='utf-8')
    native = getattr(model, 'action_protocol', None) == PROTOCOL
    if native:
        # Both task files delimit their legacy response instructions this way.
        # Keep modeling requirements and resource limits, replace only the protocol.
        begin, end = task.index('Each response'), task.index('Default limits:')
        task = task[:begin] + GUIDANCE + '\n\n' + task[end:]
        (workspace / 'TASK.md').write_text(task, encoding='utf-8')
    # Embed the tool guide so the first decision has a self-contained contract.
    task += '\n\n' + (workspace / 'TOOLS.md').read_text(encoding='utf-8')
    context=nullcontext()
    if getattr(args,'progress_registry',None):
        from benchcad_runtime.heartbeat import Heartbeat
        context=Heartbeat(root/'workbench-heartbeat.json',stage=kind)
    with context:
        return run_episode(model, executor, finalizer, task, stage / 'episode', budget,
                           system_prompt=system_prompt(kind, budget, native), initial_images=initial_images)


def summarize(stages: dict, elapsed: float, smoke: bool, model: str | None) -> dict:
    local = [r for stage in stages.values() for r in stage['local_usage_records']]
    provider = [r for stage in stages.values() for r in stage['usage_records']]
    return {'schema': 'benchcad-server-pilot/1', 'status': 'development_pilot',
            'model': model, 'trusted_replay': smoke, 'expert_baseline_enabled': False,
            'wall_seconds': elapsed,
            'stages': {name: {key: value.get(key) for key in
                       ('submitted', 'ended_by', 'decision_rounds', 'code_executions', 'wall_seconds', 'bundle',
                        'failure_diagnostic')}
                       for name, value in stages.items()},
            'usage_metric': 'local_visible_text', 'local_visible_text': summarize_local_usage(local),
            'provider_usage_diagnostic': summarize_usage(provider), 'actual_fee': None,
            'metrics': pending_metrics(Path('.'))}


def run(args, smoke=False):
    if args.iou_vendor is not None:
        # Fail before a paid API request when the local evaluator image is missing.
        import subprocess
        import tempfile
        if not args.evaluator_image or args.evaluator_image.startswith('-') or any(c.isspace() for c in args.evaluator_image):
            raise ValueError('An explicit local evaluator image name is required')
        with tempfile.TemporaryDirectory(prefix='benchcad-eval-preflight-') as config:
            try:
                checked = subprocess.run(['docker', '--config', config, '--host', 'unix:///var/run/docker.sock',
                                          'image', 'inspect', args.evaluator_image],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         env=DockerExecutor.environment(), timeout=20)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('Evaluator image preflight timed out') from exc
        if checked.returncode:
            raise ValueError('Evaluator image is missing; run setup before paid evaluation')
    start = time.monotonic()
    root = args.run_dir.absolute()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)  # exclusive and private
    register_live_task(root,args)
    source = source_snapshot(root / 'runtime-source')
    stages = {}
    error = None
    try:
        if args.command == 'reconstruct':
            _load_encoding()
            model = OpenAIResponses(args.model, args.max_output_tokens, args.reasoning_effort,
                                    args.base_url, max_request_bytes=args.max_request_bytes,
                                    native_tools=args.action_protocol == 'native')
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
                                             args.base_url, max_request_bytes=args.max_request_bytes,
                                             native_tools=args.action_protocol == 'native')
            step = args.step
        if args.command != 'reconstruct':
            stages['drawing'] = stage_episode('drawing', root, source, args, drawing_model, step=step)
        if getattr(args, 'reconstruct', False) and stages.get('drawing', {}).get('submitted'):
            if smoke:
                # Deliberately known fixture dimensions: plumbing replay, NOT an AI reconstruction score.
                code = "import cadquery as cq\nm=cq.Workplane('XY').box(60,40,8).faces('>Z').workplane().hole(10)\ncq.exporters.export(m, 'output/model.step')"
                model = ReplayModel(['```submit\n' + code + '\n```'])
            else:
                model = OpenAIResponses(args.model, args.max_output_tokens, args.reasoning_effort,
                                        args.base_url, max_request_bytes=args.max_request_bytes,
                                        native_tools=args.action_protocol == 'native')
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
    reference = args.reference_step if args.command == 'reconstruct' else locals().get('step')
    complete_evaluation(summary, root, reference, args.iou_vendor, args.evaluator_image,
                        DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes),
                        args.evaluation_seconds)
    complete_quality(summary, root, reference, args.expert_json, args.image,
                     DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes),
                     args.drawing_evaluation_seconds)
    save_summary_and_report(root, summary)
    publish_result(root, summary, args)
    print(json.dumps(summary, indent=2))
    return 0 if summary['success'] else 1


def evaluate_existing(args):
    root = args.run_dir.absolute()
    if (root / 'evaluated-summary.json').exists() or (root / 'evaluation-report.html').exists():
        raise FileExistsError('Existing evaluation/report is never overwritten')
    summary = json.loads(read_regular(root, 'summary.json', 2*1024*1024))
    # Resolve the frozen candidate from this run, never from a stale absolute path.
    if summary['stages'].get('reconstruction', {}).get('submitted'):
        summary['stages']['reconstruction']['bundle'] = str(root / 'reconstruction/bundle')
    complete_evaluation(summary, root, args.reference_step, args.iou_vendor, args.evaluator_image,
                        DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes),
                        args.evaluation_seconds)
    complete_quality(summary, root, args.reference_step, args.expert_json, args.image,
                     DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes),
                     args.drawing_evaluation_seconds)
    save_summary_and_report(root, summary, existing_run=True)
    publish_result(root, summary, args)
    print(json.dumps(summary['metrics'], indent=2))
    return 0 if summary['metrics']['reconstruction_iou']['status'] == 'computed' else 1


def score_existing(args):
    """No model calls, preserve prior scores/reports, run drawing metrics once."""
    from evaluation.interfaces import upgrade_iou_result
    from benchcad_runtime.artifacts import sha256
    root=args.run_dir.absolute()
    if (root/'scored-summary.json').exists() or (root/'scoring-report.html').exists():
        raise FileExistsError('Existing scoring report is never overwritten')
    name='evaluated-summary.json' if (root/'evaluated-summary.json').exists() else 'summary.json'
    summary=json.loads(read_regular(root,name,2*1024*1024))
    if (root/'evaluation/candidate.json').exists():
        metric=upgrade_iou_result(json.loads(read_regular(root,'evaluation/candidate.json',1024*1024)))
        final_step=read_regular(root,'reconstruction/bundle/model.step',MAX_CAD_BYTES)
        if sha256(final_step)!=metric['candidate']['sha256']:
            raise ValueError('Existing IoU does not match frozen reconstruction')
        if args.reference_step is not None:
            ref=args.reference_step.absolute()
            if sha256(read_regular(ref.parent,ref.name,MAX_CAD_BYTES))!=metric['reference']['sha256']:
                raise ValueError('Existing IoU reference mismatch')
        summary['metrics']['reconstruction_iou']={'metric':'reconstruction_iou','status':'computed',
            'score':metric['reconstruction_score_100']/100,'score_scale':'0..1',
            **{k:metric[k] for k in ('raw_iou','primitive_baseline','corrected_iou','metric_version','reconstruction_score_100')}}
    else:
        summary['metrics']['reconstruction_iou']={'metric':'reconstruction_iou','status':'not_evaluated',
                                                 'score':None,'reason':'verified_iou_result_missing'}
    complete_quality(summary,root,args.reference_step,args.expert_json,args.image,
                     DockerLimits(args.memory_mib,args.cpus,args.pids,args.log_bytes),args.drawing_evaluation_seconds)
    write_new(root/'scored-summary.json',json.dumps(summary,indent=2).encode())
    from benchcad_runtime.report import write_report
    write_report(root,summary,'scoring-report.html')
    print(json.dumps(summary['metrics'],indent=2))
    return 0 if summary['scoring_complete'] else 1


def publish_result(root, summary, args):
    from benchcad_runtime.workbench import finalize_runtime
    if getattr(args,'case_number',None) is not None:
        from evaluation.display_export import prepare
        try:
            prepare(root,args.evaluator_image,DockerLimits(args.memory_mib,args.cpus,args.pids,args.log_bytes))
        except Exception:
            # Display transport/export failures cannot change generation/scoring.
            pass
    result=finalize_runtime(root,summary,case_number=getattr(args,'case_number',None),run_id=getattr(args,'run_id',None),
                            title=getattr(args,'case_title',''),reasoning_effort=getattr(args,'reasoning_effort',''),
                            config=getattr(args,'workbench_config',None),limits=dict(per_stage_limit=args.rounds,
                            wall_seconds=args.wall_seconds,execution_seconds=args.execution_seconds,
                            cpus=args.cpus,memory_mib=args.memory_mib))
    if getattr(args,'progress_registry',None) and (root/'workbench-result/run.json').exists():
        from benchcad_runtime.progress import register
        from benchcad_runtime.workbench import read,atomic,dump
        try:
            register(args.progress_registry,read(root/'workbench-result/run.json'),root,root,root/'workbench-heartbeat.json',report_ready=True)
        except Exception as exc:
            atomic(root/'progress-registration-status.json',dump({'status':'pending_retry','error_type':type(exc).__name__}))
    return result


def register_live_task(root,args):
    if not getattr(args,'progress_registry',None):return
    from benchcad_runtime.progress import register
    from benchcad_runtime.workbench import base_run,atomic,dump,read
    from datetime import datetime,timezone
    identity=root/'workbench-run-id.json'
    if not identity.exists():atomic(identity,dump({'id':args.run_id or 'run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}))
    public=base_run(args.case_number,read(identity)['id'],'自建 Agent · '+(args.model or 'replay'),title=args.case_title,model=args.model or 'trusted-replay')
    public['reasoning_effort']=getattr(args,'reasoning_effort','') or ''
    register(args.progress_registry,public,root,root,root/'workbench-heartbeat.json',report_ready=False)


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
    for command in ('doctor', 'smoke', 'run', 'reconstruct', 'evaluate', 'score'):
        s = sub.add_parser(command)
        s.add_argument('--run-dir', type=Path, required=True)
        s.add_argument('--image', default=None, help='Already built Linux Docker image (no implicit pull)')
        s.add_argument('--config', type=Path, help='Explicit TOML only; no automatic local-config search')
        if command == 'doctor':
            continue
        s.add_argument('--case-number', type=int, help='Public case identity; enables display-package export')
        s.add_argument('--case-title', default='')
        s.add_argument('--run-id', help='Public opaque run ID; generated once if omitted')
        s.add_argument('--workbench-config', type=Path, help='Controller-only local sync command configuration')
        s.add_argument('--progress-registry', type=Path, help='Server-private registry; automatically register live progress and final report')
        s.add_argument('--iou-vendor', type=Path, help='Pinned BenchCAD 2 checkout; enables automatic scoring')
        s.add_argument('--evaluator-image', default=None)
        s.add_argument('--evaluation-seconds', type=float, default=None)
        s.add_argument('--reference-step', type=Path, help='Evaluator-only reference for reconstruct/evaluate')
        s.add_argument('--expert-json', type=Path, help='Evaluator-only expert v5 JSON; never sent to either model')
        s.add_argument('--drawing-evaluation-seconds', type=float, default=None)
        if command in ('run', 'smoke'):
            s.add_argument('--reconstruct', action=argparse.BooleanOptionalAction, default=command == 'smoke')
        for flag, typ in (('rounds', int), ('wall-seconds', float), ('execution-seconds', float),
                          ('max-transport-failures', int), ('max-consecutive-output-limits', int),
                          ('memory-mib', int), ('cpus', float),
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
            s.add_argument('--max-request-bytes', type=int, default=None,
                           help='HTTP request size cap including images/history; set for the provider')
            s.add_argument('--reasoning-effort', default=None)
            s.add_argument('--action-protocol', choices=('fenced', 'native'), default=None,
                           help='Explicit action interface; native uses execute_python and submit tools')
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
                'budget': {'rounds': 60, 'wall_seconds': 7200, 'execution_seconds': 300,
                           'max_transport_failures': 3, 'max_consecutive_output_limits': 2},
                'api': {'model': '', 'base_url': None, 'max_output_tokens': 500000, 'reasoning_effort': None,
                        'action_protocol': 'fenced',
                        'max_request_bytes': 64*1024*1024},
                'evaluation': {'iou_vendor': None, 'evaluator_image': 'benchcad-cadquery:evaluator',
                               'evaluation_seconds': 400, 'reference_step': None,
                               'expert_json': None, 'drawing_evaluation_seconds': 300}}
    for section, defaults in sections.items():
        for key, value in defaults.items():
            if getattr(args, key, None) is None:
                setattr(args, key, config.get(section, {}).get(key, value))
    if args.command != 'doctor':
        import math
        if args.progress_registry is not None and args.case_number is None:
            raise ValueError('Live progress requires --case-number')
        if (not math.isfinite(args.evaluation_seconds) or not 0 < args.evaluation_seconds <= 600):
            raise ValueError('Evaluation budget must be between 0 and 600 seconds')
        if not math.isfinite(args.drawing_evaluation_seconds) or not 0 < args.drawing_evaluation_seconds <= 600:
            raise ValueError('Drawing evaluation budget must be between 0 and 600 seconds')
        for key in ('iou_vendor', 'reference_step', 'expert_json'):
            if getattr(args, key, None) is not None:
                setattr(args, key, Path(getattr(args, key)))
        if args.expert_json is not None:
            if not args.expert_json.is_file() or args.expert_json.is_symlink():
                raise ValueError('Expert JSON must be a regular evaluator-only file')
            if args.command in ('reconstruct', 'evaluate', 'score') and args.reference_step is None:
                raise ValueError('Expert verification requires --reference-step')
        if args.iou_vendor is not None:
            if not (args.iou_vendor / 'envs/common/part_metric.py').is_file():
                raise ValueError('Missing pinned BenchCAD 2 metric source')
            if args.command == 'run' and not args.reconstruct:
                raise ValueError('Automatic IoU requires --reconstruct')
            if args.command in ('reconstruct', 'evaluate') and (
                    args.reference_step is None or not args.reference_step.is_file() or args.reference_step.is_symlink()):
                raise ValueError('Supply --reference-step for evaluator only')
        if args.command == 'evaluate' and args.iou_vendor is None:
            raise ValueError('Supply --iou-vendor to evaluate a completed run')
        Budget(args.rounds, args.wall_seconds, args.execution_seconds, args.max_transport_failures,
               args.max_consecutive_output_limits)
        DockerLimits(args.memory_mib, args.cpus, args.pids, args.log_bytes)
        if args.rounds > 60 or args.wall_seconds > 7200 or args.execution_seconds > 300:
            raise ValueError('Pilot maxima: 60 decisions, 7200 stage seconds, 300 execution seconds')
        if type(args.max_output_tokens) is not int or not 0 < args.max_output_tokens <= 500000:
            raise ValueError('Requested output-token limit must be an integer between 1 and 500000')
    if args.command in ('run', 'reconstruct'):
        if args.action_protocol not in ('fenced', 'native'):
            raise ValueError('api.action_protocol must be fenced or native')
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
        if args.command == 'doctor':
            return doctor(args)
        if args.command == 'evaluate':
            return evaluate_existing(args)
        if args.command == 'score':
            return score_existing(args)
        return run(args, smoke=args.command == 'smoke')
    except (ValueError, OSError) as exc:
        # Generic message avoids exposing paths or external diagnostic payloads.
        p.exit(2, f'Configuration/filesystem failure ({type(exc).__name__}); check arguments and use a new run directory.\n')


if __name__ == '__main__':
    raise SystemExit(main())
