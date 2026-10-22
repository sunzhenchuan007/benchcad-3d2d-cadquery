"""Controller-only display packages and a durable, model-independent sync queue.

This module never imports an agent, a provider, CAD execution, or evaluation.
Only selected statistics, normalized images and validated display geometry leave
the run directory. The receiver must validate again before importing.
"""
from __future__ import annotations
import base64,hashlib,io,json,math,os,re,struct,subprocess,uuid
from datetime import datetime,timezone
from pathlib import Path
from contextlib import contextmanager
from .artifacts import read_regular

SCHEMA='benchcad-workbench-result/1'
BENCHMARK='BenchCAD 3Dto2D CADQuery'
FILES={'run.json','drawing.png','comparison.png','view-operations.json','geometry.json'}
LIMIT=64*1024*1024
ID=re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z')
PRIVATE=re.compile(r'(?i)((?<![a-z0-9])[a-z]:[\\/]|file://|/users/|/home/|/opt/|(?:\d{1,3}\.){3}\d{1,3}|access_token|refresh_token|api_key|bearer\s|BEGIN [^\r\n]{0,80}PRIVATE KEY|auth\.json|\.pem\b|wxid_|S-1-5-|(?<![\w.+-])[\w.+-]{1,64}@[\w.-]{1,255}\.[a-z]{2,24}\b|sk-[a-zA-Z0-9_-]{16,})')
RUN_KEYS=set('id label case_number title benchmark agent_type agent_label channel model reasoning_effort run_type continued_from_round usage_metric usage_label budget_unit budget_label note closeout stages metrics diagnostics formal_score actual_fee section_match annotation_collision overall_score created_at evaluation_seconds limits'.split())
STAGE_KEYS=set('label submitted decision_rounds code_executions wall_seconds executor_seconds input_tokens output_tokens cached_input_tokens reasoning_output_tokens'.split())
METRIC_KEYS=set('status metric_version raw_iou iou24 primitive_baseline corrected_iou reconstruction_score_100 reflection_used'.split())

def dump(value):return (json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')
def sha(data):return hashlib.sha256(data).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def atomic(path,data):
    path=Path(path);tmp=path.with_name(path.name+'.tmp')
    with tmp.open('wb') as stream:stream.write(data)
    os.replace(tmp,path)
def scan(value):
    if PRIVATE.search(json.dumps(value,ensure_ascii=False,allow_nan=False)):
        raise ValueError('private_information_in_display_data')
def identifier(value):
    if not isinstance(value,str) or not ID.fullmatch(value):raise ValueError('invalid_public_id')
    return value
def keys(obj,allowed):
    if not isinstance(obj,dict) or set(obj)-set(allowed):raise ValueError('unsupported_display_field')
def vector(value):
    if not isinstance(value,list) or len(value)!=3 or any(type(x) not in (int,float) or not math.isfinite(x) for x in value):raise ValueError('invalid_display_vector')
def number(value):
    if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0):raise ValueError('invalid_display_number')

@contextmanager
def publication_lock(path):
    """OS-owned advisory lock, automatically released if the publisher exits."""
    stream=Path(path).open('a+b')
    try:
        stream.seek(0,2)
        if not stream.tell():stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
    finally:stream.close()

def validate_run(run):
    keys(run,RUN_KEYS);scan(run);identifier(run['id'])
    if type(run['case_number']) is not int or not 0<run['case_number']<=999:raise ValueError('invalid_case')
    if run['benchmark']!=BENCHMARK:raise ValueError('invalid_benchmark')
    for k in ['label','title','model','agent_type','agent_label','channel','reasoning_effort','run_type','usage_metric','usage_label','budget_unit','budget_label','note','closeout']:
        if not isinstance(run[k],str) or len(run[k])>1000:raise ValueError('invalid_display_label')
    if 'created_at' in run and (not isinstance(run['created_at'],str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+Z',run['created_at'])):raise ValueError('invalid_public_timestamp')
    number(run.get('evaluation_seconds'));number(run['continued_from_round'])
    if len(run['stages'])>2:raise ValueError('too_many_stages')
    for s in run['stages']:
        keys(s,STAGE_KEYS)
        if not isinstance(s['label'],str) or len(s['label'])>100:raise ValueError('invalid_stage_label')
        if type(s['submitted']) is not bool:raise ValueError('invalid_stage_status')
        for k,v in s.items():
            if k not in ('label','submitted'):number(v)
    m=run['metrics'];keys(m,METRIC_KEYS)
    if m['reflection_used'] is not None and type(m['reflection_used']) is not bool:raise ValueError('invalid_reflection_status')
    if m['metric_version'] is not None and (not isinstance(m['metric_version'],str) or len(m['metric_version'])>100):raise ValueError('invalid_metric_version')
    if m['status'] not in ('computed','not_evaluated','failed','blocked'):raise ValueError('invalid_metric_status')
    for k in ['raw_iou','iou24','primitive_baseline','corrected_iou']:
        number(m[k])
        if m[k] is not None and m[k]>1:raise ValueError('invalid_iou')
    number(m['reconstruction_score_100'])
    if m['status']=='computed':
        from evaluation.scoring import reconstruction_score
        expected=reconstruction_score(m['raw_iou'],m['primitive_baseline'])
        if any(abs(expected[k]-m[k])>1e-9 for k in ('corrected_iou','reconstruction_score_100')):raise ValueError('inconsistent_score')
    elif any(m[k] is not None for k in ('raw_iou','corrected_iou','reconstruction_score_100')):raise ValueError('unscored_is_not_zero')
    keys(run['diagnostics'],['reference_extents_mm','candidate_extents_mm','volume_ratio'])
    for k in ['reference_extents_mm','candidate_extents_mm']:
        if run['diagnostics'][k] is not None:vector(run['diagnostics'][k])
    number(run['diagnostics']['volume_ratio'])
    keys(run['limits'],['per_stage_limit','wall_seconds','execution_seconds','cpus','memory_mib'])
    for v in run['limits'].values():number(v)
    if run['formal_score'] is not False or run['actual_fee'] is not None:raise ValueError('unsupported_or_unscored_metric')
    for key in ('section_match','annotation_collision','overall_score'):
        number(run[key])
        if run[key] is not None and run[key]>1:raise ValueError('invalid_unit_score')
    if run['overall_score'] is not None:
        if m['status']!='computed' or run['section_match'] is None or run['annotation_collision'] is None:
            raise ValueError('incomplete_product')
        expected=m['reconstruction_score_100']/100*run['section_match']*run['annotation_collision']
        if abs(run['overall_score']-expected)>1e-9:raise ValueError('inconsistent_product')
    return run

def validate_views(data):
    keys(data,['record_scope','status','coverage','coordinates','reason','views']);scan(data)
    if data['record_scope']!='final_retained_views' or data['status'] not in ('extracted','unavailable','needs_review'):raise ValueError('invalid_view_status')
    for k in ['coverage','coordinates','reason']:
        if k in data and (not isinstance(data[k],str) or len(data[k])>500):raise ValueError('invalid_view_metadata')
    if len(data['views'])>500:raise ValueError('too_many_views')
    seen=set()
    for v in data['views']:
        keys(v,['id','operation','normal','xdir','plane','keep_side','scope']);identifier(v['id'])
        if v['id'] in seen:raise ValueError('duplicate_view')
        seen.add(v['id'])
        if v['operation'] not in ('orthographic_projection','isometric_projection','axonometric_projection','full_section','partial_projection','custom_projection','unclassified'):raise ValueError('unsupported_view_operation')
        for k in ['normal','xdir']:vector(v[k])
        if 'plane' in v:
            keys(v['plane'],['point','normal']);vector(v['plane']['point']);vector(v['plane']['normal'])
        if 'keep_side' in v and v['keep_side'] not in (-1,1):raise ValueError('invalid_keep_side')
        if 'scope' in v and v['scope']!='requires_review':
            scope=v['scope'];keys(scope,['operation','index','index_base','size','center'])
            if scope['operation']=='intersect_box':vector(scope['size']);vector(scope['center'])
            elif scope['operation']=='select_face':
                if type(scope['index']) is not int or scope['index']<0 or scope['index_base']!=0:raise ValueError('invalid_face_scope')
            else:raise ValueError('unsupported_scope')
    return data

def validate_geometry(data,run):
    keys(data,['summary','geometry','bounds','focus','triangles','surfaces','position_and_normal_bytes'])
    s=data['summary'];keys(s,['iou','grid','padded_grid','pitch_reference_mm','counts','reflection_used','metric_version','case','source','differing_fraction','selected_transform','largest_connected_difference_cells'])
    c=s['counts'];keys(c,['common','a_only','b_only','union'])
    if any(type(v) is not int or v<0 for v in c.values()) or c['union']<=0 or sum(c[k] for k in ['common','a_only','b_only'])!=c['union']:raise ValueError('invalid_voxel_counts')
    if abs(c['common']/c['union']-run['metrics']['raw_iou'])>1e-12 or s['iou']!=run['metrics']['raw_iou']:raise ValueError('geometry_score_mismatch')
    scan({k:v for k,v in data.items() if k not in ('geometry','surfaces')})
    if len(data['bounds'])!=2 or set(data['geometry'])!={'common','missing','extra'} or set(data['surfaces'])!={'gt','generated'}:raise ValueError('missing_geometry_component')
    for v in data['bounds']:vector(v)
    keys(data['focus'],['centre','span']);vector(data['focus']['centre']);number(data['focus']['span'])
    def decode(value,fmt):
        raw=base64.b64decode(value,validate=True)
        if len(raw)>LIMIT or len(raw)%4:raise ValueError('invalid_geometry_buffer')
        if fmt=='f' and any(not math.isfinite(x[0]) for x in struct.iter_unpack('<f',raw)):raise ValueError('nonfinite_geometry')
        return raw
    keys(data['geometry'],['common','missing','extra']);total=0
    for value in data['geometry'].values():
        raw=decode(value,'f')
        if len(raw)%36:raise ValueError('invalid_triangle_buffer')
        total+=len(raw)//36
    if total!=data['triangles']:raise ValueError('triangle_count_mismatch')
    keys(data['surfaces'],['gt','generated'])
    for surface in data['surfaces'].values():
        keys(surface,['positions','indices','triangles','bounds']);p=decode(surface['positions'],'f');ix=decode(surface['indices'],'I')
        if len(p)%12 or len(ix)%12 or len(ix)//12!=surface['triangles']:raise ValueError('invalid_surface_mesh')
        if any(x[0]>=len(p)//12 for x in struct.iter_unpack('<I',ix)):raise ValueError('invalid_surface_index')
        for v in surface['bounds']:vector(v)
    return data

def png(data):
    from PIL import Image
    if len(data)>24*1024*1024:raise ValueError('image_too_large')
    with Image.open(io.BytesIO(data)) as image:
        if image.format!='PNG' or image.width*image.height>32_000_000:raise ValueError('invalid_png')
        out=io.BytesIO();image.convert('RGB').save(out,format='PNG',optimize=True);return out.getvalue()

def validate_package(folder):
    folder=Path(folder);manifest=json.loads(read_regular(folder,'manifest.json',100000))
    keys(manifest,['schema','case_number','run_id','files'])
    if manifest['schema']!=SCHEMA:raise ValueError('unsupported_result_package')
    names=set(manifest['files'])
    if not {'run.json','view-operations.json'}<=names or names-FILES:raise ValueError('unexpected_package_file')
    if set(p.name for p in folder.iterdir())!=names|{'manifest.json'}:raise ValueError('unlisted_package_file')
    blobs={}
    for name,meta in manifest['files'].items():
        keys(meta,['sha256','bytes']);data=read_regular(folder,name,LIMIT)
        if meta!={'sha256':sha(data),'bytes':len(data)}:raise ValueError('display_hash_mismatch')
        blobs[name]=data
    run=validate_run(json.loads(blobs['run.json']))
    if (run['id'],run['case_number'])!=(manifest['run_id'],manifest['case_number']):raise ValueError('package_identity_mismatch')
    validate_views(json.loads(blobs['view-operations.json']))
    if 'geometry.json' in blobs:validate_geometry(json.loads(blobs['geometry.json']),run)
    for name,data in blobs.items():
        if name.endswith('.png'):
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                if image.info or image.getexif() or image.mode!='RGB':raise ValueError('image_metadata_not_stripped')
            png(data)
    return manifest,run,blobs

def make_package(destination,run,views,*,drawing=None,comparison=None,geometry=None):
    destination=Path(destination);validate_run(run);validate_views(views)
    blobs={'run.json':dump(run),'view-operations.json':dump(views)}
    for name,path in [('drawing.png',drawing),('comparison.png',comparison)]:
        if path is not None:blobs[name]=png(Path(path).read_bytes())
    if geometry is not None:
        data=read(geometry);validate_geometry(data,run);blobs['geometry.json']=dump(data)
    manifest={'schema':SCHEMA,'case_number':run['case_number'],'run_id':run['id'],'files':{n:{'sha256':sha(b),'bytes':len(b)} for n,b in blobs.items()}}
    if destination.exists():
        old,_,_=validate_package(destination)
        if old!=manifest:raise ValueError('immutable_run_id_conflict')
        return destination
    destination.parent.mkdir(parents=True,exist_ok=True)
    tmp=destination.parent/('.pending-'+uuid.uuid4().hex)
    tmp.mkdir(mode=0o755)
    try:
        for name,blob in blobs.items():(tmp/name).write_bytes(blob)
        (tmp/'manifest.json').write_bytes(dump(manifest));validate_package(tmp)
        os.replace(tmp,destination)
    finally:
        if tmp.exists():
            for name in FILES|{'manifest.json'}:(tmp/name).unlink(missing_ok=True)
            tmp.rmdir()
    return destination

def sync_package(package,config,*,attempts=1,timeout=600):
    """Only invokes a trusted sync command. No source run/model entry point."""
    package=Path(package);manifest,_,_=validate_package(package)
    state_path=package.parent/(package.name+'-sync.json')
    state=read(state_path) if state_path.exists() else {'package_sha256':sha(dump(manifest)),'attempts':0,'status':'pending'}
    if state['package_sha256']!=sha(dump(manifest)):raise ValueError('queued_package_changed')
    cfg=read(config);keys(cfg,['command','timeout_seconds'])
    target_hash=sha(dump(cfg))
    if state['status']=='synced' and state.get('target_sha256')==target_hash:return state
    state['target_sha256']=target_hash
    command=cfg['command']
    if not isinstance(command,list) or not command or any(not isinstance(s,str) for s in command) or not any('{package}' in s for s in command):raise ValueError('invalid_sync_command')
    for _ in range(attempts):
        state.update(attempts=state['attempts']+1,status='uploading');atomic(state_path,dump(state))
        try:
            result=subprocess.run([s.replace('{package}',str(package.resolve())) for s in command],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=min(timeout,float(cfg.get('timeout_seconds',timeout))),shell=False)
            state['status']='synced' if result.returncode==0 else 'pending_retry'
            state['error_type']=None if result.returncode==0 else 'SyncCommandFailed'
        except (OSError,subprocess.TimeoutExpired) as exc:state.update(status='pending_retry',error_type=type(exc).__name__)
        atomic(state_path,dump(state))
        if state['status']=='synced':break
    return state

def base_run(case_number,run_id,label,*,title='',model='',cli=False):
    return dict(benchmark=BENCHMARK,case_number=case_number,id=identifier(run_id),label=label,title=title or f'Case {case_number:02d}',model=model,reasoning_effort='high',agent_type='codex_cli' if cli else 'custom_api',agent_label='Codex CLI' if cli else '自建 Agent',channel='ChatGPT 登录账号' if cli else 'API',run_type='development_pilot',continued_from_round=None,usage_metric='cli_reported_usage' if cli else 'local_visible_text',usage_label='CLI 返回的 usage' if cli else '本地可见文本估算',budget_unit='cad_executions' if cli else 'model_decisions',budget_label='30 CAD 执行 / 阶段' if cli else '30 模型回复 / 阶段',note='独立开发试跑；不同 Agent 与用量口径分开记录。',closeout='结果包由控制程序生成，同步失败只重试上传。',stages=[],metrics=dict(status='not_evaluated',metric_version=None,raw_iou=None,iou24=None,primitive_baseline=None,corrected_iou=None,reconstruction_score_100=None,reflection_used=None),diagnostics=dict(reference_extents_mm=None,candidate_extents_mm=None,volume_ratio=None),formal_score=False,actual_fee=None,section_match=None,annotation_collision=None,overall_score=None,evaluation_seconds=None,limits=dict(per_stage_limit=30,wall_seconds=1800,execution_seconds=300,cpus=1,memory_mib=4096))

def apply_metric(run,metric):
    if metric.get('status','computed')!='computed':
        for k in ['raw_iou','iou24','primitive_baseline','corrected_iou','reconstruction_score_100','reflection_used']:run['metrics'][k]=None
    run['metrics'].update({k:metric.get(k) for k in METRIC_KEYS if k in metric})
    run['metrics']['status']=metric.get('status','computed')
    if 'score' in metric:
        run['metrics']['reconstruction_score_100']=(metric['score']*100 if metric.get('score_scale')=='0..1' and metric['score'] is not None else metric['score'])
    if 'reference' in metric:
        run['diagnostics']={**{k+'_extents_mm':metric[k].get('extents_mm') for k in ['reference','candidate']},'volume_ratio':metric.get('absolute_size_diagnostics',{}).get('volume_ratio')}

def export_cli(root,destination,case_number,run_id,*,title='',label='Codex CLI · 6.1-sol',comparison=None,geometry=None):
    root=Path(root);trial=read(root/'trial.json');run=base_run(case_number,run_id,label,title=title,model=trial['model'],cli=True)
    run['reasoning_effort']=trial['reasoning_effort']
    for folder,stage_label in [(root,'出图'),(root/'reconstruction','独立重建')]:
        if not (folder/'trial.json').exists():continue
        t=read(folder/'trial.json');b=read(folder/'bridge-summary.json')
        usage={k:sum((u or {}).get(k,0) or 0 for u in t.get('usage',[])) for k in ['input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens']}
        run['stages'].append(dict(label=stage_label,submitted=b['submitted'],decision_rounds=None,code_executions=b['cad_executions'],wall_seconds=t['wall_seconds'],executor_seconds=b['wall_seconds'],**usage))
    evidence=root/'reconstruction/reconstruction-iou.json'
    if evidence.exists():apply_metric(run,read(evidence))
    summary=root/'reconstruction/summary.json'
    if summary.exists():run['evaluation_seconds']=read(summary).get('evaluation_seconds')
    from .view_summary import generate
    views=generate(root)
    return make_package(destination,run,views,drawing=root/'drawing.png' if (root/'drawing.png').exists() else None,comparison=comparison,geometry=geometry)

def finalize_runtime(root,summary,*,case_number,run_id,title='',reasoning_effort='',config=None,limits=None):
    """Called AFTER the immutable local report is saved; publication cannot fail a run."""
    root=Path(root);status={'status':'not_configured'}
    try:
        if case_number is not None:
            id_path=root/'workbench-run-id.json'
            if not id_path.exists():atomic(id_path,dump({'id':run_id or 'run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}))
            run=base_run(case_number,read(id_path)['id'],'自建 Agent · '+str(summary.get('model') or 'replay'),title=title,model=summary.get('model') or 'trusted-replay')
            run['reasoning_effort']=reasoning_effort or '';run['evaluation_seconds']=summary.get('evaluation_seconds')
            if limits:run['limits']=limits;run['budget_label']=str(limits['per_stage_limit'])+' 模型回复 / 阶段'
            if summary.get('trusted_replay'):run['note']='固定代码流程检查，不是 AI 能力成绩。'
            for name,s in summary['stages'].items():
                ep=root/name/'episode/episode.json';usage=read(ep).get('local_visible_text',{}).get('known_usage_subtotal',{}) if ep.exists() else {}
                run['stages'].append(dict(label='出图' if name=='drawing' else '独立重建',**{k:s.get(k) for k in ['submitted','decision_rounds','code_executions','wall_seconds']},input_tokens=usage.get('input_tokens'),output_tokens=usage.get('output_tokens'),cached_input_tokens=None,reasoning_output_tokens=None))
            evidence=root/'evaluation/candidate.json'
            apply_metric(run,read(evidence) if evidence.exists() else summary['metrics']['reconstruction_iou'])
            # Legacy transport key section_match now carries expert preference;
            # detailed metric names and deductions remain in the local summary.
            run['section_match']=summary['metrics'].get('expert_preference',{}).get('score')
            run['annotation_collision']=summary['metrics'].get('annotation_collision',{}).get('score')
            run['overall_score']=summary['metrics'].get('overall_score')
            from .view_summary import generate
            image=root/'drawing/bundle/drawing.png'
            comparison=root/'display-export/comparison.png';geometry=root/'display-export/geometry.json'
            package=make_package(root/'workbench-result',run,generate(root),drawing=image if image.exists() else None,
                                 comparison=comparison if comparison.exists() else None,geometry=geometry if geometry.exists() else None)
            status=sync_package(package,config,attempts=3) if config else {'status':'pending_configuration'}
    except Exception as exc:status={'status':'export_or_sync_failed','error_type':type(exc).__name__}
    atomic(root/'workbench-status.json',dump(status));return status

def finalize_cli(root,*,case_number,run_id,title='',config=None,comparison=None,geometry=None):
    """CLI harness completion callback; reuse its existing immutable package."""
    root=Path(root);package=root/'workbench-result'
    try:
        if package.exists():validate_package(package)
        else:export_cli(root,package,case_number,run_id,title=title,comparison=comparison,geometry=geometry)
        status=sync_package(package,config,attempts=3) if config else {'status':'pending_configuration'}
    except Exception as exc:status={'status':'export_or_sync_failed','error_type':type(exc).__name__}
    atomic(root/'workbench-status.json',dump(status));return status
