"""Register a task with the server's report worker; never launches a model."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchcad_runtime.progress import register
from benchcad_runtime.workbench import read

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--registry',type=Path,required=True);p.add_argument('--public-run',type=Path,required=True)
    p.add_argument('--drawing-run',type=Path,required=True);p.add_argument('--reconstruction-run',type=Path,required=True)
    p.add_argument('--heartbeat',type=Path);p.add_argument('--report-ready',action='store_true')
    p.add_argument('--evaluation-run',type=Path);a=p.parse_args()
    entry=register(a.registry,read(a.public_run),a.drawing_run,a.reconstruction_run,a.heartbeat,report_ready=a.report_ready,evaluation_run=a.evaluation_run)
    print(json.dumps({'registered':True,'case_number':entry['case_number'],'run_id':entry['run_id']}))
if __name__=='__main__':main()
