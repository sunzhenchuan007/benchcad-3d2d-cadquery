"""Public progress projection. Never export paths, process IDs or raw logs."""
from datetime import datetime,timezone
import json,math
from pathlib import Path
from .workbench import identifier,scan,keys,validate_run,publication_lock,atomic,dump

STAGES={'queued','drawing','reconstruction','evaluation','report','publication','completed','failed','stopped'}
STATES={'queued','running','completed','failed','stopped','unknown'}
PUBLICATION={'not_ready','pending','reporting','publishing','published','retry'}

def utc():return datetime.now(timezone.utc).isoformat()
def validate(value):
    keys(value,['schema','updated_at','interval_seconds','runs'])
    if value['schema']!='benchcad-live-progress/1' or value['interval_seconds']!=60:raise ValueError('unsupported_progress')
    datetime.fromisoformat(value['updated_at'])
    seen=set()
    for r in value['runs']:
        keys(r,['case_number','run_id','title','status','stage','publication','updated_at','elapsed_seconds','cad_executions','drawing_submitted','reconstruction_submitted','score','error_code'])
        identifier(r['run_id']);identity=(r['case_number'],r['run_id'])
        if identity in seen or type(r['case_number']) is not int or not 0<r['case_number']<1000:raise ValueError('duplicate_or_invalid_progress')
        seen.add(identity)
        if r['status'] not in STATES or r['stage'] not in STAGES or r['publication'] not in PUBLICATION:raise ValueError('invalid_progress_state')
        datetime.fromisoformat(r['updated_at'])
        for k in ['elapsed_seconds','cad_executions','score']:
            n=r[k]
            if n is not None and (type(n) not in (int,float) or not math.isfinite(n) or n<0):raise ValueError('invalid_progress_number')
        if r['score'] is not None and r['score']>100:raise ValueError('invalid_score')
        if r['error_code'] not in (None,'report_failed','publication_failed','invalid_result','source_unavailable'):raise ValueError('invalid_public_error')
        scan(r)
    return value

def project(case_number,run_id,title,*,drawing=None,reconstruction=None,metric=None,heartbeat=None,publication='not_ready',updated_at=None,error_code=None):
    d=drawing or {};r=reconstruction or {};h=heartbeat or {};stage='queued';status='queued';score=None
    if d:stage='drawing';status='running'
    if d.get('submitted'):stage='reconstruction'
    if r:stage='reconstruction';status='running'
    if r.get('submitted'):stage='evaluation'
    if h.get('status')=='running' and h.get('stage') in STAGES:stage=h['stage'];status='running'
    if any(s.get('ended_by') not in (None,'submitted') and not s.get('submitted') for s in (d,r)):stage='failed';status='failed'
    if h.get('status') in ('failed','stopped'):
        status=h['status'];stage=status
    # A completed score takes precedence over a stale/failed local controller.
    if metric and metric.get('status')=='computed':
        stage='completed' if publication=='published' else 'report' if publication in ('pending','reporting','not_ready') else 'publication'
        status='completed';score=metric.get('reconstruction_score_100',metric.get('score'))
    elif metric and metric.get('status') in ('failed','blocked'):status='failed';stage='failed'
    elif metric and metric.get('status')=='not_evaluated':status='completed';stage='completed' if publication=='published' else 'report'
    elapsed=sum(s.get('wall_seconds',0) or 0 for s in (d,r)) or h.get('elapsed_seconds')
    count=sum(s.get('cad_executions',0) or 0 for s in (d,r)) if d or r else None
    row=dict(case_number=case_number,run_id=run_id,title=title,status=status,stage=stage,publication=publication,updated_at=updated_at or h.get('updated_at') or utc(),elapsed_seconds=elapsed,cad_executions=count,drawing_submitted=bool(d.get('submitted')),reconstruction_submitted=bool(r.get('submitted')),score=score,error_code=error_code)
    validate(dict(schema='benchcad-live-progress/1',updated_at=utc(),interval_seconds=60,runs=[row]));return row

def register(registry,public_run,drawing_run,reconstruction_run,heartbeat=None,report_ready=False,evaluation_run=None):
    """Trusted controller callback at task creation; registry is PRIVATE."""
    validate_run(public_run);registry=Path(registry);registry.parent.mkdir(parents=True,exist_ok=True)
    entry=dict(case_number=public_run['case_number'],run_id=public_run['id'],title=public_run['title'],public_run=public_run,drawing_run=str(Path(drawing_run).resolve()),reconstruction_run=str(Path(reconstruction_run).resolve()))
    entry['report_ready']=bool(report_ready)
    if heartbeat is not None:entry['heartbeat']=str(Path(heartbeat).resolve())
    if evaluation_run is not None:entry['evaluation_run']=str(Path(evaluation_run).resolve())
    with publication_lock(registry.with_suffix('.lock')):
        data=json.loads(registry.read_text()) if registry.exists() else {'runs':[]}
        key=(entry['case_number'],entry['run_id'])
        old=next((r for r in data['runs'] if (r['case_number'],r['run_id'])==key),None)
        if old:
            if any(old[k]!=entry[k] for k in ['drawing_run','reconstruction_run']):raise ValueError('registered_run_identity_changed')
            old.update(entry)
        else:data['runs'].append(entry)
        atomic(registry,dump(data))
    return entry
