"""Launch a controller independently; this launcher never requests a model."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone


def launch(log_dir, command, cwd=None):
    log_dir=Path(log_dir).resolve()
    log_dir.mkdir(parents=True,exist_ok=False)
    flags={'creationflags':subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session':True}
    with (log_dir/'stdout.log').open('wb') as out,(log_dir/'stderr.log').open('wb') as err:
        child=subprocess.Popen(command,cwd=cwd,stdin=subprocess.DEVNULL,stdout=out,stderr=err,**flags)
    result={'pid':child.pid,'started_at':datetime.now(timezone.utc).isoformat(),'status':'launched'}
    (log_dir/'launch.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir',required=True,type=Path)
    parser.add_argument('--cwd',type=Path)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:parser.error('Supply a controller command after --')
    print(json.dumps(launch(args.log_dir,command,args.cwd)))
