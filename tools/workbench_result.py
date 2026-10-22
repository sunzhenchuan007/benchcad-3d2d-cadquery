"""Export/retry display results. This entry point never runs a model or scoring."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchcad_runtime.workbench import export_cli,sync_package,validate_package

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    export=sub.add_parser('export-cli');export.add_argument('--run-dir',type=Path,required=True)
    export.add_argument('--case-number',type=int,required=True);export.add_argument('--run-id',required=True)
    export.add_argument('--title',default='');export.add_argument('--label',default='Codex CLI · 6.1-sol')
    export.add_argument('--comparison',type=Path);export.add_argument('--geometry',type=Path)
    export.add_argument('--config',type=Path)
    retry=sub.add_parser('retry');retry.add_argument('--package',type=Path,required=True);retry.add_argument('--config',type=Path,required=True);retry.add_argument('--attempts',type=int,default=3)
    check=sub.add_parser('validate');check.add_argument('--package',type=Path,required=True)
    a=p.parse_args()
    if a.action=='export-cli':
        package=export_cli(a.run_dir,a.run_dir/'workbench-result',a.case_number,a.run_id,title=a.title,label=a.label,comparison=a.comparison,geometry=a.geometry)
        result=sync_package(package,a.config,attempts=3) if a.config else {'status':'pending_configuration'}
    elif a.action=='retry':
        if not 1<=a.attempts<=10:p.error('attempts must be 1..10')
        result=sync_package(a.package,a.config,attempts=a.attempts)
    else:
        _,run,_=validate_package(a.package);result={'status':'validated','case_number':run['case_number'],'run_id':run['id']}
    print(json.dumps(result));return 0 if result['status'] in ('synced','validated','pending_configuration') else 1
if __name__=='__main__':raise SystemExit(main())
