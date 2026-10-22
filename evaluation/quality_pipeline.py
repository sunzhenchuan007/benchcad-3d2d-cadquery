"""Controller integration. Expert files never enter either model workspace."""
from pathlib import Path
import json
import time

from benchcad_runtime.artifacts import MAX_CAD_BYTES, read_regular, sha256, write_new
from benchcad_runtime.docker_executor import DockerExecutor
from .drawing_metrics import aggregate, unit


def evaluate_quality(root, reference, expert, image, limits, timeout=300):
    stage=Path(root)/'drawing-evaluation';stage.mkdir(exist_ok=False)
    workspace=stage/'workspace';workspace.mkdir()
    source=stage/'source';source.mkdir()
    repo=Path(__file__).resolve().parent.parent
    files={'evaluation':['__init__.py','drawing_metrics.py','collisions.py','view_evidence.py','quality_worker.py'],
           'benchcad_cadquery':['__init__.py','drawing.py','templates.py','operations.py']}
    for package,names in files.items():
        (source/package).mkdir()
        for name in names:
            write_new(source/package/name,read_regular(repo/package,name,1024*1024,allow_empty=True),readonly=True)
    bundle=Path(root)/'drawing/bundle'
    manifest=json.loads(read_regular(bundle,'manifest.json',100000))
    data=read_regular(bundle,'drawing.dxf',MAX_CAD_BYTES)
    if manifest.get('kind')!='drawing' or manifest['files']['drawing.dxf']!={'sha256':sha256(data),'bytes':len(data)}:
        raise ValueError('Frozen drawing checksum mismatch')
    write_new(workspace/'drawing.dxf',data,readonly=True)
    if expert is not None:
        expert=Path(expert).absolute()
        write_new(workspace/'expert.json',read_regular(expert.parent,expert.name,4*1024*1024),readonly=True)
        if reference is None:raise ValueError('Expert scoring requires original STEP for geometry verification')
        reference=Path(reference).absolute()
        write_new(workspace/'reference.step',read_regular(reference.parent,reference.name,MAX_CAD_BYTES),readonly=True)
        evidence=Path(root)/'drawing/evidence'
        if (evidence/'drawing.operations.jsonl').exists():
            capture=json.loads(read_regular(evidence,'capture.json',100000))
            log=read_regular(evidence,'drawing.operations.jsonl',16*1024*1024)
            if capture['dxf_sha256']!=sha256(data) or capture['files']['drawing.operations.jsonl']!={'sha256':sha256(log),'bytes':len(log)}:
                raise ValueError('Operation capture checksum mismatch')
            write_new(workspace/'drawing.operations.jsonl',log,readonly=True)
    executor=DockerExecutor(workspace,stage/'records',source,image,limits)
    result=executor.run_command(['-I','/opt/benchcad/evaluation/quality_worker.py'],timeout,stage/'records/check')
    if result.timed_out or result.exit_code:raise RuntimeError('Drawing evaluator failed')
    payload=read_regular(workspace,'quality.json',4*1024*1024);scores=json.loads(payload)
    for name in ('expert_preference','annotation_collision'):
        if scores[name]['status']=='computed':unit(scores[name]['score'])
        elif scores[name]['score'] is not None:raise ValueError('Uncomputed metric must be null')
    write_new(stage/'quality.json',payload,readonly=True)
    if (workspace/'sections.json').exists():
        write_new(stage/'sections.json',read_regular(workspace,'sections.json',4*1024*1024),readonly=True)
    return scores


def complete_quality(summary,root,reference,expert,image,limits,timeout=300):
    start=time.monotonic()
    empty=lambda name,reason:{'metric':name,'status':'not_evaluated','score':None,'score_scale':'0..1','reason':reason}
    if not summary['stages'].get('drawing',{}).get('submitted'):
        scores={n:empty(n,'drawing_not_submitted') for n in ('expert_preference','annotation_collision')}
    else:
        try:scores=evaluate_quality(root,reference,expert,image,limits,timeout)
        except Exception as exc:
            scores={n:{**empty(n,'drawing_evaluation_failed'),'status':'failed','error_type':type(exc).__name__}
                    for n in ('expert_preference','annotation_collision')}
    summary['metrics'].pop('section_match',None)  # Retired placeholder; preference owns this policy now.
    summary['metrics'].update(scores)
    aggregate(summary['metrics'])
    summary['metrics']['formal_score']=summary['metrics']['overall_status']=='computed' and not summary.get('trusted_replay',False)
    summary['drawing_evaluation_seconds']=time.monotonic()-start
    summary['total_wall_seconds']=summary['wall_seconds']+summary.get('evaluation_seconds',0)+summary['drawing_evaluation_seconds']
    summary['scoring_complete']=summary['metrics']['overall_status']=='computed'
    return summary
