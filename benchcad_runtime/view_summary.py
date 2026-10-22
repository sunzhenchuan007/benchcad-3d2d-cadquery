"""Compact final-view records. Never execute drawing code or infer hidden geometry."""
from __future__ import annotations

import ast
import hashlib
import html
import json
import math
from pathlib import Path
import zipfile


def _calls(sources):
    calls = {}
    scopes = {}
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        assignments = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets)==1 and isinstance(stmt.targets[0],ast.Name):
                assignments.setdefault(stmt.targets[0].id, []).append(stmt.value)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method not in ('projection', '_project', 'full_section'):
                continue
            index = 1 if method == '_project' else 0
            name = node.args[index] if len(node.args) > index else next(
                (k.value for k in node.keywords if k.arg == 'name'), None)
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                calls.setdefault(name.value, set()).add(method)
                if method == '_project' and node.args:
                    shape = node.args[0]
                    if isinstance(shape,ast.Name) and len(assignments.get(shape.id,[]))==1:
                        shape = assignments[shape.id][0]
                    scope = None
                    try:
                        if isinstance(shape,ast.Subscript) and isinstance(shape.value,ast.Call) and isinstance(shape.value.func,ast.Attribute) and shape.value.func.attr=='Faces':
                            scope={'operation':'select_face','index':ast.literal_eval(shape.slice),'index_base':0}
                        elif isinstance(shape,ast.Call) and isinstance(shape.func,ast.Attribute) and shape.func.attr=='intersect':
                            chain=list(ast.walk(shape))
                            box=next(n for n in chain if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='box')
                            translation=next(n for n in chain if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='translate')
                            if len(box.args)==3 and not box.keywords:
                                scope={'operation':'intersect_box','size':[ast.literal_eval(a) for a in box.args], 'center':ast.literal_eval(translation.args[0])}
                    except (ValueError,TypeError,StopIteration,IndexError):
                        pass
                    if scope is not None:
                        scopes.setdefault(name.value,set()).add(json.dumps(scope,sort_keys=True))
    return calls, scopes


def _inserts(data):
    # Frozen exports are ASCII DXF. No CAD dependency is needed by the controller.
    if data.startswith(b'AutoCAD Binary DXF'):
        raise ValueError('binary_dxf_not_supported')
    lines = data.decode('utf-8', errors='replace').splitlines()
    section = None
    entity = None
    result = set()
    for i in range(0, len(lines)-1, 2):
        code, value = lines[i].strip(), lines[i+1].strip()
        if code == '0':
            entity = value
            if value == 'ENDSEC':
                section = None
        elif code == '2' and entity == 'SECTION':
            section = value
        elif code == '2' and entity == 'INSERT' and section == 'ENTITIES':
            result.add(value)
    return result


def summarize(state, dxf, sources=()):
    if state.get('dxf_sha256') != hashlib.sha256(dxf).hexdigest():
        raise ValueError('state_dxf_mismatch')
    blocks = _inserts(dxf)
    calls, scopes = _calls(sources)
    views = []
    ids = set()
    for view in state.get('views', []):
        name = view['id']
        if name in ids or view.get('block') not in blocks:
            raise ValueError('duplicate_or_missing_view')
        ids.add(name)
        normal, xdir = view['normal'], view['xdir']
        for vector in (normal, xdir):
            if len(vector) != 3 or not all(type(n) in (int, float) and math.isfinite(n) for n in vector) or not any(vector):
                raise ValueError('invalid_direction')
        kind = view.get('type')
        row = {'id': name, 'operation': 'unclassified', 'normal': normal, 'xdir': xdir}
        if kind == 'full_section':
            row.update(operation='full_section', plane=view['plane'], keep_side=view['keep_side'])
        elif kind == 'projection':
            scope = view.get('projection_scope')
            methods = calls.get(name, set())
            if scope == 'whole_model' or (scope is None and methods == {'projection'}):
                n = [abs(v) / math.hypot(*normal) for v in normal]
                row['operation'] = ('orthographic_projection' if sum(v > 1e-7 for v in n) == 1
                                    else 'isometric_projection' if max(n)-min(n) < 1e-7
                                    else 'axonometric_projection')
            else:
                # Raw projection may be a face, clipped solid, custom section, or
                # an entirely different shape. Do not label it a half section.
                row['operation'] = 'custom_projection'
                row['scope'] = 'requires_review'
                if methods == {'_project'} and len(scopes.get(name,set())) == 1:
                    row['operation'] = 'partial_projection'
                    row['scope'] = json.loads(next(iter(scopes[name])))
        views.append(row)
    return {'record_scope': 'final_retained_views', 'status': 'extracted',
            'coverage': 'recorded_views_only',
            'coordinates': '输入 STEP 坐标系，长度单位 mm；normal 指向观察者，xdir 指向图面右方；keep_side=-1 保留剖切面法向负侧。',
            'views': views}


def generate(root):
    """Accept a local CLI report or a runtime run directory; always emit a status."""
    root = Path(root)
    dxf = next((p for p in (root/'drawing.dxf', root/'drawing/bundle/drawing.dxf') if p.is_file()), None)
    state_path = root/'drawing/workspace/output/drawing.state.json'
    state, sources = None, []
    result = {'record_scope': 'final_retained_views', 'status': 'unavailable', 'reason': 'drawing_or_state_not_available', 'views': []}
    try:
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            sources = [p.read_text(encoding='utf-8') for p in (root/'drawing/workspace').glob('*.py') if not p.name.startswith('_execution_')]
        else:
            archive = next((p for p in (root/'private-records.zip', root/'private-server-records.zip') if p.is_file()), None)
            if archive:
                with zipfile.ZipFile(archive) as z:
                    names = z.namelist()
                    target = 'run/drawing/workspace/output/drawing.state.json'
                    if target in names:
                        if z.getinfo(target).file_size > 16*1024*1024:
                            raise ValueError('state_too_large')
                        state = json.loads(z.read(target))
                    sources = [z.read(n).decode('utf-8') for n in names
                               if n.startswith('run/drawing/workspace/') and n.endswith('.py')
                               and n.count('/') == 3 and not Path(n).name.startswith('_execution_')
                               and z.getinfo(n).file_size < 1024*1024]
        if dxf and state is not None:
            result = summarize(state, dxf.read_bytes(), sources)
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as exc:
        result.update(status='needs_review', reason=type(exc).__name__)
    # One view per line; runtime, token, dimensions, and retry history stay elsewhere.
    header = {k:v for k,v in result.items() if k != 'views'}
    lines = ['{'] + ['  '+json.dumps(k)+': '+json.dumps(v, ensure_ascii=False)+',' for k,v in header.items()]
    lines += ['  "views": [']
    lines += ['    '+json.dumps(v, ensure_ascii=False, allow_nan=False)+(',' if i+1<len(result['views']) else '') for i,v in enumerate(result['views'])]
    lines += ['  ]', '}']
    (root/'view-operations.json').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return result


def section(result):
    labels = {'orthographic_projection':'正投影', 'isometric_projection':'等轴测投影',
              'axonometric_projection':'轴测投影', 'full_section':'全剖',
              'partial_projection':'局部几何投影', 'custom_projection':'自定义投影（范围待确认）', 'unclassified':'未识别'}
    rows = ''.join('<tr><td>'+html.escape(str(v['id']))+'</td><td>'+labels[v['operation']]+'</td></tr>' for v in result['views'])
    text = ('仅列最终记录的视图，不含标注与修改历史。工作状态可由模型修改；未记录的手工图线、半剖或局部剖不能自动保证识别。'
            if result['status']=='extracted' else '视图记录尚不可用或与最终 DXF 不一致；空列表不代表没有视图。')
    return '<section><h2>精简视图操作</h2><p><a href="view-operations.json">查看视图操作 JSON</a></p><table><tr><th>视图</th><th>操作</th></tr>'+rows+'</table><p>'+text+'</p></section>'
