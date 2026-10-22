"""Replay tool log, then regenerate each final view against the frozen STEP.

Only supported tool-generated views are eligible. Arbitrary Python geometry is
not inferred from stdout, labels or an agent-supplied trust flag.
"""
from pathlib import Path
import hashlib
import math
import tempfile


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def geometry_signature(block):
    from ezdxf.lldxf.tagwriter import TagCollector
    def norm(v):
        if isinstance(v,float):
            if not math.isfinite(v):raise ValueError('Nonfinite CAD geometry')
            return round(v,6)
        if isinstance(v,(tuple,list)) or hasattr(v,'x'):
            return tuple(norm(x) for x in v)
        return v
    rows=[]
    for entity in block:
        writer=TagCollector(dxfversion='AC1024');entity.export_dxf(writer)
        # Entity handles, owners, and layer names do not change geometry.
        rows.append(tuple((tag.code,norm(tag.value)) for tag in writer.tags if tag.code not in (5,330,8)))
    return sorted(rows,key=repr)


def verify(dxf, step, operations, *, case_id):
    import ezdxf
    from benchcad_cadquery.operations import final_views
    from benchcad_cadquery.drawing import Drawing
    dxf,step,operations=map(Path,(dxf,step,operations))
    start,exported=final_views(operations.read_bytes())
    if start['source_sha256']!=digest(step) or exported['dxf_sha256']!=digest(dxf):
        raise ValueError('Operation source/export checksum mismatch')
    if start.get('units')!='mm' or start.get('coordinate_frame')!='source_step':
        raise ValueError('Unsupported operation units/frame')
    doc=ezdxf.readfile(dxf);m=doc.modelspace();views=exported['views']
    if len(views)>100:raise ValueError('View limit exceeded')
    by_name={v['block']:v for v in views}
    if len(by_name)!=len(views):raise ValueError('Duplicate view blocks')
    inserts=[e for e in m.query('INSERT') if e.dxf.name in by_name or e.dxf.name.startswith('VIEW_')]
    if len(inserts)!=len(views) or set(e.dxf.name for e in inserts)!=set(by_name):
        raise ValueError('Final view block coverage mismatch')
    for e in m:
        if e.dxftype() in ('LINE','LWPOLYLINE','POLYLINE','CIRCLE','ARC','ELLIPSE','SPLINE','HATCH','SOLID'):
            if e.dxf.layer not in {'FRAME','BC_SHEET','BC_TITLEBLOCK','CENTER','GEOM_CENTER','CUTTING','ANNO_SECTION','DIMENSIONS'}:
                raise ValueError('Unclassified modelspace geometry; view count is incomplete')
        if e.dxftype()=='INSERT' and e.dxf.name not in by_name and e.dxf.layer not in {'BC_TITLEBLOCK','ANNO_TEXT','ANNO_TABLE'}:
            raise ValueError('Unclassified block; view count is incomplete')
    result_views=[];sections=[]
    with tempfile.TemporaryDirectory() as tmp:
        drawing=Drawing(step,Path(tmp)/'check.dxf')
        for v in views:
            if v['type'] not in ('projection','full_section'):
                raise ValueError('View generator unsupported by evidence verifier')
            insert=next(e for e in inserts if e.dxf.name==v['block'])
            if insert.dxf.get('rotation',0)!=0 or insert.dxf.row_count!=1 or insert.dxf.column_count!=1:
                raise ValueError('Unrecorded view transform')
            if any(abs(insert.dxf.get(k,1)-v['scale'])>1e-9 for k in ('xscale','yscale','zscale')):
                raise ValueError('View scale mismatch')
            if any(abs(a-b)>1e-6 for a,b in zip(insert.dxf.insert, [*v['position'],0])):
                raise ValueError('View position mismatch')
            for e in [insert,*doc.blocks.get(v['block'])]:
                layer=doc.layers.get(e.dxf.layer)
                if e.dxf.get('invisible',0) or layer.is_off() or layer.is_frozen():
                    raise ValueError('Hidden view geometry')
            args={k:v[k] for k in ('normal','xdir','position','scale')}
            if v['type']=='full_section':
                fresh=drawing.full_section(v['id'],v['plane']['point'],v['plane']['normal'],
                          xdir=v['xdir'],position=v['position'],scale=v['scale'],keep_side=v['keep_side'])
                if any(abs(a-b)>1e-6 for a,b in zip(fresh['normal'],v['normal'])):
                    raise ValueError('Section viewing direction mismatch')
            else:
                if v.get('projection_scope')!='whole_model':raise ValueError('Unclassified custom projection')
                fresh=drawing.projection(v['id'],**args)
            if geometry_signature(doc.blocks.get(v['block']))!=geometry_signature(drawing.doc.blocks.get(fresh['block'])):
                raise ValueError('View geometry differs from logged operation')
            item={'id':v['id'],'kind':v['type'],'normal':v['normal']}
            if v['type']=='full_section':
                item['plane']={'point_mm':v['plane']['point'],'normal':v['plane']['normal'],'keep_side':v['keep_side']}
                sections.append({'view_id':v['id'],'plane':item['plane']})
            result_views.append(item)
    return {'schema':'benchcad-section-observations/1','case_id':case_id,
            'input_sha256':digest(step),'dxf_sha256':digest(dxf),'units':'mm',
            'coordinate_frame':'source_step','views':result_views,'sections':sections,
            'provenance':{'method':'tool-log-replay-plus-final-geometry-regeneration/1',
                          'operations_sha256':digest(operations),'dxf_consistency_verified':True,
                          'geometry_verified':True,'arbitrary_python_covered':False}}
