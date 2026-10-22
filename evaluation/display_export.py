"""Separate best-effort preview export, after scores are frozen."""
from pathlib import Path
import uuid
from benchcad_runtime.artifacts import read_regular,write_new,MAX_CAD_BYTES,MAX_PNG_BYTES,validate_png
from benchcad_runtime.docker_executor import DockerExecutor
from benchcad_runtime.workbench import atomic,dump

def prepare(root,image,limits,timeout=120):
    root=Path(root);dest=root/'display-export';output=dest/'comparison.png'
    if output.exists():validate_png(output.read_bytes());return output
    status={'status':'not_available'}
    try:
        evidence=root/'evaluation/candidate.json';source_models=root/'evaluation/workspace'
        if not evidence.exists():return None
        dest.mkdir(exist_ok=True);workspace=dest/'workspace';source=dest/'source'
        workspace.mkdir(exist_ok=True);source.mkdir(exist_ok=True)
        for name in ['reference.step','candidate.step']:
            target=workspace/name
            if not target.exists():write_new(target,read_regular(source_models,name,MAX_CAD_BYTES),readonly=True)
        if not (workspace/'metric.json').exists():write_new(workspace/'metric.json',read_regular(evidence.parent,evidence.name,1024*1024),readonly=True)
        script=Path(__file__).with_name('display_comparison.py')
        if not (source/'render.py').exists():write_new(source/'render.py',script.read_bytes(),readonly=True)
        records=dest/('records-'+uuid.uuid4().hex[:12])
        executor=DockerExecutor(workspace,records,source,image,limits)
        result=executor.run_command(['-I','/opt/benchcad/render.py','/workspace/reference.step','/workspace/candidate.step','/workspace/metric.json','/workspace/comparison.png'],timeout,records/'preview')
        if result.exit_code or result.timed_out:raise RuntimeError('Preview export failed')
        data=read_regular(workspace,'comparison.png',MAX_PNG_BYTES);validate_png(data);atomic(output,data);status={'status':'generated'}
    except Exception as exc:status={'status':'failed','error_type':type(exc).__name__}
    finally:
        dest.mkdir(exist_ok=True);atomic(dest/'status.json',dump(status))
    return output if output.exists() else None


def prepare_geometry(root,image,limits,timeout=300):
    """Build a display-only cache from the saved transform and pinned snapshot."""
    from benchcad_runtime.workbench import validate_geometry,sha,LIMIT
    import json
    root=Path(root);dest=root/'display-export';dest.mkdir(exist_ok=True)
    evidence=root/'evaluation/candidate.json'
    metric=json.loads(read_regular(evidence.parent,evidence.name,1024*1024))
    if metric.get('status')!='computed':return None
    output=dest/'geometry.json'
    if output.exists():
        validate_geometry(json.loads(read_regular(dest,output.name,LIMIT)),{'metrics':metric});return output
    workspace=dest/'geometry-workspace';source=dest/'geometry-source'
    workspace.mkdir(exist_ok=True);source.mkdir(exist_ok=True)
    for name in ['reference.step','candidate.step']:
        data=read_regular(root/'evaluation/workspace',name,MAX_CAD_BYTES)
        if sha(data)!=metric['reference' if name=='reference.step' else 'candidate']['sha256']:raise ValueError('frozen_step_mismatch')
        target=workspace/name
        if not target.exists():write_new(target,data,readonly=True)
        elif read_regular(workspace,name,MAX_CAD_BYTES)!=data:raise ValueError('cached_step_mismatch')
    data=read_regular(evidence.parent,evidence.name,1024*1024)
    if not (workspace/'metric.json').exists():write_new(workspace/'metric.json',data,readonly=True)
    elif read_regular(workspace,'metric.json',1024*1024)!=data:raise ValueError('cached_metric_mismatch')
    original=root/'evaluation/source'
    manifest=json.loads(read_regular(root/'evaluation','source-manifest.json',2*1024*1024));total=0
    for name,expected in manifest.items():
        if not name.startswith('envs/') or not name.endswith('.py'):raise ValueError('unexpected_evaluator_snapshot')
        data=read_regular(original/'vendor',name,2*1024*1024,allow_empty=True);total+=len(data)
        if sha(data)!=expected or total>64*1024*1024:raise ValueError('evaluator_snapshot_changed')
        target=source/'vendor'/name;target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():write_new(target,data,readonly=True)
        elif read_regular(source/'vendor',name,2*1024*1024,allow_empty=True)!=data:raise ValueError('display_snapshot_changed')
    if 'envs/common/part_metric.py' not in manifest:raise ValueError('missing_evaluator_snapshot')
    # Display code is versioned separately; the evaluator snapshot remains frozen.
    atomic(source/'render.py',Path(__file__).with_name('display_geometry.py').read_bytes())
    records=dest/('geometry-records-'+uuid.uuid4().hex[:12])
    executor=DockerExecutor(workspace,records,source,image,limits)
    result=executor.run_command(['-I','/opt/benchcad/render.py','/workspace/reference.step','/workspace/candidate.step','/workspace/metric.json','/opt/benchcad/vendor','/workspace/geometry.json'],timeout,records/'geometry')
    if result.exit_code or result.timed_out:raise RuntimeError('display_geometry_export_failed')
    data=read_regular(workspace,'geometry.json',LIMIT)
    validate_geometry(json.loads(data),{'metrics':metric});atomic(output,data)
    return output
