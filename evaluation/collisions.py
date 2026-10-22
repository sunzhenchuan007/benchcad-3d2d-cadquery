"""DXF annotation-pair collisions; model geometry and line/line crossings excluded.

Text uses ezdxf's oriented text extent estimate, not OCR or exact glyph ink.
The fixed footprint policy is reported with every result for reproducibility.
"""
from __future__ import annotations

import math
import time

from .drawing_metrics import VERSION

EPS = .05  # millimetres; touching alone is not a collision


def hull(points):
    points = sorted(set((float(p[0]), float(p[1])) for p in points))
    def cross(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    sides = []
    for seq in (points, reversed(points)):
        side = []
        for p in seq:
            while len(side)>1 and cross(side[-2],side[-1],p)<=0: side.pop()
            side.append(p)
        sides.append(side[:-1])
    return sides[0]+sides[1]


def overlap(a,b):
    if len(a)<3 or len(b)<3:return False
    for poly in (a,b):
        for p,q in zip(poly,poly[1:]+poly[:1]):
            x,y=-(q[1]-p[1]),q[0]-p[0]; norm=math.hypot(x,y)
            if norm==0:continue
            pa=[(v[0]*x+v[1]*y)/norm for v in a]; pb=[(v[0]*x+v[1]*y)/norm for v in b]
            if min(max(pa),max(pb))-max(min(pa),min(pb))<=EPS:return False
    return True


def crosses_text(a,b,poly):
    # Clip the segment against a convex CCW text polygon inset by EPS.
    lo,hi=0.,1.
    for p,q in zip(poly,poly[1:]+poly[:1]):
        dx,dy=q[0]-p[0],q[1]-p[1]; norm=math.hypot(dx,dy)
        f=dx*(a[1]-p[1])-dy*(a[0]-p[0])-EPS*norm
        g=dx*(b[1]-a[1])-dy*(b[0]-a[0])
        if abs(g)<1e-12:
            if f<0:return False
        elif g>0:lo=max(lo,-f/g)
        else:hi=min(hi,-f/g)
        if hi<=lo:return False
    return hi>lo and math.dist(a,b)*(hi-lo)>EPS


def score_dxf(path):
    import ezdxf
    from ezdxf.disassemble import make_primitive
    start=time.perf_counter();doc=ezdxf.readfile(path)
    if doc.units != ezdxf.units.MM:raise ValueError('Collision metric requires millimetre DXF')
    groups=[];unsupported=[];processed=0
    ignored={'BC_SHEET','BC_TITLEBLOCK','BC_GUIDE_NOPLOT','BC_PRODUCT_SPEC_TABLE','ANNO_TABLE','FRAME'}
    def visible(e):
        if e.dxf.get('invisible',0):return False
        layer=doc.layers.get(e.dxf.layer) if e.dxf.layer in doc.layers else None
        return layer is None or not (layer.is_off() or layer.is_frozen())
    def components(e,g,depth=0):
        nonlocal processed
        processed+=1
        if processed>250000 or depth>12:raise ValueError('Annotation complexity limit')
        if not visible(e):return
        kind=e.dxftype()
        if kind=='INSERT':
            for child in e.virtual_entities():components(child,g,depth+1)
            for child in e.attribs:components(child,g,depth+1)
        elif kind in ('TEXT','MTEXT','ATTRIB','ATTDEF'):
            if kind=='ATTDEF':return
            points=list(make_primitive(e).vertices());poly=hull([(p.x,p.y) for p in points])
            if len(poly)>=3:g['texts'].append(poly)
        elif kind in ('LINE','LWPOLYLINE','POLYLINE','ARC','CIRCLE','ELLIPSE','SPLINE'):
            pts=[(p.x,p.y) for p in make_primitive(e,max_flattening_distance=.02).vertices()]
            g['lines'].extend(zip(pts,pts[1:]))
        elif kind in ('SOLID','TRACE','3DFACE'):
            poly=hull([(p.x,p.y) for p in make_primitive(e).vertices()])
            if len(poly)>=3:g['fills'].append(poly)
        elif kind not in ('POINT',):unsupported.append(kind)
    def visit(e,prefix='',depth=0):
        if depth>12:raise ValueError('Nested block limit')
        if not visible(e) or e.dxf.layer in ignored:return
        kind=e.dxftype();identifier=prefix+(e.dxf.get('handle') or str(len(groups)))
        if kind=='INSERT':
            if e.dxf.name.startswith('VIEW_'):return
            for child in e.virtual_entities():visit(child,identifier+'/',depth+1)
            for child in e.attribs:visit(child,identifier+'/',depth+1)
            return
        if kind in ('LINE','LWPOLYLINE','POLYLINE','ARC','SOLID') and e.dxf.layer in ('DIMENSIONS','ANNO_DIM'):
            unsupported.append('UNGROUPED_ANNOTATION_GEOMETRY');return
        if kind not in ('TEXT','MTEXT','ATTRIB','DIMENSION','LEADER','MULTILEADER'):return
        if kind in ('LEADER','MULTILEADER'):
            unsupported.append(kind);return
        g={'id':identifier,'texts':[],'lines':[],'fills':[]}
        if kind=='DIMENSION':
            if not e.dxf.get('geometry'):raise ValueError('Dimension has no rendered block')
            for child in e.virtual_entities():components(child,g)
        else:components(e,g)
        if g['texts'] or g['lines'] or g['fills']:groups.append(g)
        if len(groups)>5000:raise ValueError('Annotation count limit')
    for e in doc.modelspace():visit(e)
    result={'metric':'annotation_collision','metric_version':VERSION,'score_scale':'0..1',
            'coverage':'native_dimensions_and_text; model_geometry_excluded',
            'footprint_method':'ezdxf-oriented-text-extents/1','approximate_text_extents':True,
            'penetration_tolerance_mm':EPS,'deduction_per_pair':.05,'annotation_count':len(groups)}
    if unsupported:
        return {**result,'status':'not_evaluated','score':None,'reason':'unsupported_annotation_entities',
                'unsupported_types':sorted(set(unsupported))}
    pairs=[]
    # Sweep by X bounds to avoid testing every pair on a typical sparse drawing.
    def bounds(g):
        pts=[p for k in ('texts','lines','fills') for poly in g[k] for p in poly]
        return min(p[0] for p in pts),max(p[0] for p in pts),min(p[1] for p in pts),max(p[1] for p in pts)
    rows=sorted([(bounds(g),g) for g in groups],key=lambda r:r[0][0])
    for i,(a,A) in enumerate(rows):
        for b,B in rows[i+1:]:
            if b[0]>a[1]:break
            if b[2]>a[3] or a[2]>b[3]:continue
            reason=None
            if any(overlap(x,y) for x in A['texts'] for y in B['texts']):reason='text_text'
            if reason is None:
                for first,second in ((A,B),(B,A)):
                    if any(crosses_text(x,y,t) for x,y in first['lines'] for t in second['texts']) or any(overlap(f,t) for f in first['fills'] for t in second['texts']):
                        reason='annotation_geometry_text';break
            if reason:pairs.append({'annotations':[A['id'],B['id']],'reason':reason,
                                    'region_mm':[max(a[0],b[0]),max(a[2],b[2]),min(a[1],b[1]),min(a[3],b[3])]})
    return {**result,'status':'computed','score':max(0.,1-.05*len(pairs)),
            'reason':'annotation_pairs_only','pair_count':len(pairs),'pairs':pairs,
            'seconds':time.perf_counter()-start}
