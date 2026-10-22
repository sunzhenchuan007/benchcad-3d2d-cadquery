"""Local-only re-evaluation of trusted frozen CQ/SW artifacts, without model calls.

Historical IoU evidence is explicitly reinterpreted under the new score policy;
this is not another geometry computation or an expert-DXF reconstruction.
"""
from pathlib import Path
import argparse
import hashlib
import html
import json
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation.scoring import VERSION, reconstruction_score
from evaluation.collisions import score_dxf, native_regions, render_check
from evaluation.drawing_metrics import aggregate, score_expert


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def regrade_iou(evidence):
    if evidence.get('status') != 'computed':
        return {'status': 'not_evaluated', 'score': None, 'reason': 'iou_not_computed'}
    baseline = evidence.get('primitive_baseline', evidence.get('upstream_diagnostics', {}).get('baseline'))
    if baseline is None:
        return {'status': 'not_evaluated', 'score': None, 'reason': 'primitive_baseline_missing'}
    raw = evidence.get('raw_iou', evidence.get('iou'))
    return {'status': 'computed', 'metric_version': VERSION, **reconstruction_score(raw, baseline),
            'source_metric_version': evidence.get('metric_version'), 'geometry_recomputed': False}


def evaluate(dxf, iou_json, output, *, native_views=None, expert=None,
             step=None, operations=None, render=True, deduction=.05):
    dxf, iou_json, output = map(Path, (dxf, iou_json, output))
    output.mkdir(parents=True, exist_ok=False)
    original = dxf.read_bytes()
    evidence = load(iou_json)
    regions = native_regions(load(native_views), dxf) if native_views else None
    metrics = {'reconstruction_iou': regrade_iou(evidence),
               'annotation_collision': score_dxf(dxf, check_path=output/'drawing-collision-check.dxf',
                                                 view_regions=regions, deduction_per_pair=deduction),
               'expert_preference': {'status': 'not_evaluated', 'score': None, 'reason': 'expert_json_not_configured'}}
    if expert:
        g = load(expert)
        if step and operations:
            try:
                from evaluation.view_evidence import verify
                candidate = verify(dxf, step, operations, case_id=g['case_id'])
                (output/'sections.json').write_text(json.dumps(candidate, indent=2), encoding='utf-8')
                metrics['expert_preference'] = score_expert(candidate, g, evidence_verified=True)
            except Exception as exc:
                metrics['expert_preference'].update(reason='expert_or_view_evidence_invalid', error_type=type(exc).__name__)
        else:
            metrics['expert_preference']['reason'] = ('native_expert_adapter_pending' if native_views
                                                      else 'verified_view_evidence_not_configured')
    aggregate(metrics)
    result = {'schema': 'benchcad-local-metric-review/1', 'metrics': metrics, 'api_requests': 0,
              'formal_score': False, 'historical_artifacts_unchanged': True,
              'inputs': {'dxf_sha256': hashlib.sha256(original).hexdigest(),
                         'iou_evidence_sha256': hashlib.sha256(iou_json.read_bytes()).hexdigest()},
              'notes': ['IoU raw value/baseline reused from operator-selected trusted evidence.',
                        'Collision checks rendered glyphs and visible strokes; boundary contact counts.',
                        'Missing expert evidence leaves the total unscored.']}
    if render:
        try:
            render_check(output/'drawing-collision-check.dxf', output/'drawing-collision-check.png')
            result['check_preview'] = 'drawing-collision-check.png'
        except Exception as exc:
            result['preview_error_type'] = type(exc).__name__
    if dxf.read_bytes() != original:
        raise RuntimeError('Source drawing changed')
    (output/'metrics.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    rows = ''
    for key, label in [('reconstruction_iou','重建一致性'),('annotation_collision','文字碰撞'),('expert_preference','专家 JSON')]:
        m = metrics[key]
        value = f"{m['score']:.6f}" if m['status'] == 'computed' else '未评分：' + m.get('reason','')
        rows += '<tr><td>'+label+'</td><td>'+html.escape(value)+'</td></tr>'
    total = metrics['overall_score']
    collision = metrics['annotation_collision']
    details = ''
    for pair in collision.get('pairs', []):
        names = ' / '.join(t['content'] + ' [' + t['id'] + ']' for t in pair.get('text', []))
        contacts = ', '.join(c['id'] if isinstance(c, dict) else c for c in pair.get('contact_entities', []))
        details += '<tr><td>'+html.escape(names)+'</td><td>'+html.escape(pair['reason'])+'</td><td>'+html.escape(contacts)+'</td></tr>'
    detail_table = ('<details><summary>碰撞位置明细（'+str(collision.get('pair_count',0))+' 组）</summary>'
                    '<table><tr><th>文字与编号</th><th>类别</th><th>接触对象</th></tr>'+details+'</table></details>')
    font_notice = ('<p><strong>缺失字体：'+html.escape(', '.join(collision['missing_fonts']))+
                   '。下图采用替代字体，仅用于定位；碰撞分数暂不计算。</strong></p>'
                   if collision.get('missing_fonts') else '')
    image = '<img src="drawing-collision-check.png" alt="红框标识碰撞文字">' if result.get('check_preview') else ''
    page = ('<!doctype html><meta charset="utf-8"><title>本地 Metric 检查</title>'
            '<style>body{font:16px/1.6 system-ui;margin:30px}td{padding:10px;border:1px solid #ccc}img{max-width:100%}</style>'
            '<h1>三项指标检查</h1><p>基准体校正后，0.90～0.995 线性映射为 0～1。最终分数为三项相乘。</p>'
            '<table>'+rows+'</table><p>总分：'+('未评分' if total is None else f'{total:.6f}')+'</p>'
            '<p>检查版原图线条统一黑色，只有碰撞文字的外框为红色。检查版独立保存；原图及历史成绩不变。文字边缘接触也算碰撞；检查渲染字形和线宽。字体缺失或不支持的实体会标记为未评分。</p>'
            '<a href="drawing-collision-check.dxf">检查版 DXF</a> · <a href="metrics.json">评分与碰撞明细</a>'
            +font_notice+detail_table+image)
    (output/'index.html').write_text(page, encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dxf', type=Path, required=True)
    p.add_argument('--iou-json', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--native-views', type=Path)
    p.add_argument('--expert-json', type=Path)
    p.add_argument('--reference-step', type=Path)
    p.add_argument('--operation-log', type=Path)
    p.add_argument('--deduction-per-pair', type=float, default=.05)
    p.add_argument('--no-preview', action='store_true')
    a = p.parse_args()
    r = evaluate(a.dxf, a.iou_json, a.output_dir, native_views=a.native_views, expert=a.expert_json,
                 step=a.reference_step, operations=a.operation_log, render=not a.no_preview, deduction=a.deduction_per_pair)
    print(json.dumps({'metrics': {k: v.get('score') for k,v in r['metrics'].items() if isinstance(v,dict)},
                      'overall_score': r['metrics']['overall_score']}))


if __name__ == '__main__':
    main()
