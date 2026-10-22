"""Tool-generated operation records, verified against frozen CAD by the evaluator.

The agent workspace is writable. These records alone are never trusted evidence.
"""
import json
from pathlib import Path


def append(drawing_path, operation, payload, *, reset=False):
    path=Path(drawing_path).with_suffix('.operations.jsonl')
    with path.open('w' if reset else 'a',encoding='utf-8') as stream:
        stream.write(json.dumps({'schema':'benchcad-drawing-operation/1',
                                'operation':operation,'payload':payload},allow_nan=False)+'\n')


def final_views(data):
    if len(data)>16*1024*1024:raise ValueError('Operation log limit')
    views={};started=None;exported=None;last=None
    for i,line in enumerate(data.decode('utf-8').splitlines()):
        if i>10000:raise ValueError('Operation event limit')
        item=json.loads(line);op=item['operation'];p=item['payload'];last=op
        if item['schema']!='benchcad-drawing-operation/1':raise ValueError('Invalid operation schema')
        if i==0:
            if op!='drawing.started':raise ValueError('Missing start event')
            started=p;continue
        if op=='view.created':
            if p['id'] in views:raise ValueError('Duplicate view')
            views[p['id']]=p
        elif op=='view.deleted':
            del views[p['id']]
        elif op=='drawing.exported':
            if p['views']!=list(views.values()):raise ValueError('Unrecorded view edits')
            exported=p
        else:raise ValueError('Unsupported operation')
    if not started or last!='drawing.exported':raise ValueError('Missing final export event')
    return started,exported
