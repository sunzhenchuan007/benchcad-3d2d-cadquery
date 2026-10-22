"""Trusted, network-free drawing evaluation entrypoint, run with python -I."""
from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

from evaluation.collisions import score_dxf
from evaluation.drawing_metrics import score_expert
from evaluation.view_evidence import verify


def run(root):
    result={}
    try:result['annotation_collision']=score_dxf(root/'drawing.dxf')
    except Exception as exc:
        result['annotation_collision']={'metric':'annotation_collision','status':'failed','score':None,
                                        'reason':'collision_evaluation_failed','error_type':type(exc).__name__}
    preference={'metric':'expert_preference','status':'not_evaluated','score':None,'score_scale':'0..1'}
    if not (root/'expert.json').exists():preference['reason']='expert_json_not_configured'
    elif not (root/'drawing.operations.jsonl').exists():preference['reason']='tool_operation_log_missing'
    else:
        try:
            expert=json.loads((root/'expert.json').read_text(encoding='utf-8-sig'))
            candidate=verify(root/'drawing.dxf',root/'reference.step',root/'drawing.operations.jsonl',case_id=expert['case_id'])
            (root/'sections.json').write_text(json.dumps(candidate,indent=2),encoding='utf-8')
            preference=score_expert(candidate,expert,evidence_verified=True)
        except Exception as exc:
            preference.update(status='not_evaluated',reason='expert_or_final_view_evidence_invalid',error_type=type(exc).__name__)
            # Evaluator-only diagnostic; no credentials or model-provider content.
            (root/'preference-diagnostic.txt').write_text(str(exc)[:1000],encoding='utf-8')
    result['expert_preference']=preference
    (root/'quality.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':run(Path.cwd())
