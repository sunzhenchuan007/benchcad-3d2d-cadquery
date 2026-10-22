"""DXF rendered text contact checks, including annotation and centre lines.

Text uses rendered glyph contours; line strokes include DXF lineweight.
The fixed footprint policy is reported with every result for reproducibility.
"""
from __future__ import annotations

import math
import time

from .drawing_metrics import VERSION

def native_regions(snapshot, dxf):
    """Bind controller-exported SW view regions to these exact DXF bytes."""
    from pathlib import Path
    import hashlib
    if snapshot.get('dxf_sha256') != hashlib.sha256(Path(dxf).read_bytes()).hexdigest():
        raise ValueError('Native view snapshot DXF hash mismatch')
    regions = []
    for i, row in enumerate(snapshot['views']):
        box = row['outline_m']
        if len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box):
            raise ValueError('Invalid native view outline')
        box = [v * 1000 for v in box]
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError('Invalid native view region')
        regions.append({'id': 'native_view_' + str(i), 'box': box})
    return regions


def render_check(dxf, png):
    """Render the review overlay in color, including on local SW Python installs."""
    import importlib.util
    from pathlib import Path
    if Path(png).exists():
        raise FileExistsError('Check preview already exists')
    if importlib.util.find_spec('pymupdf') is not None:
        from benchcad_cadquery.drawing import render
        return render(dxf, png, preserve_colors=True)
    import ezdxf
    from ezdxf.addons.drawing import Frontend, RenderContext, config
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    doc = ezdxf.readfile(dxf)
    fig = Figure(figsize=(14, 10), dpi=150, facecolor='white')
    FigureCanvasAgg(fig)
    ax = fig.add_axes([.02, .02, .96, .96])
    ax.set_axis_off()
    settings = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                                    color_policy=config.ColorPolicy.COLOR)
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=settings).draw_layout(doc.modelspace(), finalize=True)
    fig.savefig(png, dpi=150, facecolor='white')


def score_dxf(path, *, check_path=None, view_regions=None, deduction_per_pair=.05):
    """Check visible ink contact with text; never penalize line/line intersections.

    Layers assist grouping only. Unknown line roles are checked and recorded as
    standalone geometry. Text in frames, tables and dimensions is not exempt.
    """
    from pathlib import Path
    import hashlib
    import ezdxf
    from shapely.geometry import box
    from shapely import union_all
    from shapely.strtree import STRtree
    from .ink_geometry import InkRenderer, touches, FLATTEN_MM, CONTACT_EPS_MM

    if type(deduction_per_pair) not in (int, float) or not 0 <= deduction_per_pair <= 1:
        raise ValueError('Invalid collision deduction')
    source = Path(path)
    output = Path(check_path) if check_path else None
    if output and (output.resolve() == source.resolve() or output.exists()):
        raise ValueError('Check drawing must be a new file distinct from the submission')
    start = time.perf_counter()
    original = source.read_bytes()
    doc = ezdxf.readfile(source)
    if doc.units != ezdxf.units.MM:
        raise ValueError('Collision metric requires millimetre DXF')
    ink = InkRenderer(doc)
    annotation_layers = {'DIMENSIONS', 'ANNO_DIM', 'ANNO_SECTION', 'ANNO_TEXT',
                         'GEOM_CENTER', 'CENTER', 'CUTTING', '10'}
    curves = {'LINE', 'LWPOLYLINE', 'POLYLINE', 'ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE',
              'LEADER', 'SOLID', 'TRACE', '3DFACE'}
    texts, objects, unsupported = [], {}, set()
    processed = skipped_hatches = unclassified = outside_native = 0

    def add_object(group, kind, geometry, identifier, layer):
        row = objects.setdefault(group, {'kind': kind, 'geometries': [], 'entities': []})
        row['geometries'].append(geometry)
        row['entities'].append({'id': identifier, 'layer': layer, 'geometry': geometry})

    def visit(e, identifier, layer='0', view=None, owner=None, depth=0, weight=None):
        nonlocal processed, skipped_hatches, unclassified, outside_native
        processed += 1
        if processed > 250000 or depth > 12:
            raise ValueError('Drawing complexity limit')
        layer = e.dxf.layer if e.dxf.layer != '0' else layer
        table = doc.layers.get(layer) if layer in doc.layers else None
        if (e.dxf.get('invisible', 0) or
                (table is not None and (table.is_off() or table.is_frozen()))):
            return
        if layer == 'BC_COLLISION_CHECK':
            unsupported.add('REVIEW_OVERLAY_IN_SUBMISSION')
        kind = e.dxftype()
        if kind in {'INSERT', 'DIMENSION', 'MULTILEADER', 'MLEADER'}:
            if kind == 'DIMENSION' and not e.dxf.get('geometry'):
                unsupported.add('DIMENSION_WITHOUT_RENDERED_BLOCK')
                return
            if kind == 'INSERT' and e.dxf.name.startswith('VIEW_'):
                view = identifier
            if kind in {'DIMENSION', 'MULTILEADER', 'MLEADER'}:
                owner = identifier
            try:
                children = list(e.virtual_entities())
                if kind == 'INSERT':
                    children += list(e.attribs)
                clone = e.copy()
                clone.dxf.layer = layer
                inherited = (weight if clone.dxf.lineweight == -2 and weight is not None
                             else ink.context.resolve_lineweight(clone))
                for j, child in enumerate(children):
                    visit(child, identifier + '/' + str(j), layer, view, owner, depth+1, inherited)
            except Exception:
                unsupported.add(kind + '_DECOMPOSITION_FAILED')
            return
        is_text = kind in {'TEXT', 'MTEXT', 'ATTRIB'}
        if is_text:
            content = e.plain_text() if kind == 'MTEXT' else e.dxf.get('text', '')
            if not content.strip():
                return
        elif kind in {'POINT', 'ATTDEF'}:
            return
        elif kind == 'HATCH':
            skipped_hatches += 1
            return
        elif kind not in curves:
            unsupported.add(kind)
            return
        try:
            geometry = ink.geometry(e, layer, weight, text=is_text)
        except Exception as exc:
            unsupported.add('UNRENDERED_' + kind + ':' + type(exc).__name__)
            return
        if geometry.is_empty:
            if is_text:
                unsupported.add('EMPTY_TEXT_INK')
            return
        if is_text:
            texts.append({'id': identifier, 'owner': owner or identifier,
                          'content': content, 'layer': layer, 'geometry': geometry})
            if len(texts) > 5000:
                raise ValueError('Text count limit')
            return
        if owner:
            add_object('annotation:' + owner, 'text_annotation', geometry, identifier, layer)
        elif view is not None:
            add_object('view:' + view, 'text_view', geometry, identifier, layer)
        elif layer in annotation_layers:
            add_object('entity:' + identifier, 'text_annotation', geometry, identifier, layer)
        elif view_regions is not None:
            # Assign only geometry covered by one native outline; uncertain roles
            # remain standalone geometry, still checked rather than discarded.
            hits = [r for r in view_regions if box(*r['box']).buffer(.05).covers(geometry)]
            if len(hits) == 1:
                add_object('view:' + hits[0]['id'], 'text_view', geometry, identifier, layer)
            else:
                if not hits:
                    outside_native += 1
                else:
                    unclassified += 1
                add_object('entity:' + identifier, 'text_geometry', geometry, identifier, layer)
        else:
            unclassified += 1
            add_object('entity:' + identifier, 'text_geometry', geometry, identifier, layer)

    for entity in doc.modelspace():
        visit(entity, entity.dxf.handle)
    pairs, marked = [], {}

    def add(a, b, kind, contacts):
        involved = [a, b] if kind == 'text_text' else [a]
        geometry = a['geometry'] if kind != 'text_text' else union_all([a['geometry'], b['geometry']])
        pairs.append({'annotations': [t['id'] for t in involved],
                      'text': [{'id': t['id'], 'owner': t['owner'], 'content': t['content'],
                                'layer': t['layer']} for t in involved],
                      'view': b if kind == 'text_view' else None,
                      'object': b if kind != 'text_text' else None,
                      'contact_entities': contacts, 'reason': kind,
                      'region_mm': list(geometry.bounds)})
        for t in involved:
            marked[t['id']] = t['geometry'].bounds

    # Spatial index only filters candidates; glyph geometry confirms each contact.
    tree = STRtree([t['geometry'] for t in texts])
    for i, a in enumerate(texts):
        for j in sorted(tree.query(a['geometry'].buffer(CONTACT_EPS_MM))):
            if j > i and touches(a['geometry'], texts[j]['geometry']):
                add(a, texts[j], 'text_text', [a['id'], texts[j]['id']])
    for group, row in objects.items():
        geometry = union_all(row['geometries'])
        for i in sorted(tree.query(geometry.buffer(CONTACT_EPS_MM))):
            text = texts[i]
            if touches(text['geometry'], geometry):
                contacts = [{'id': e['id'], 'layer': e['layer']} for e in row['entities']
                            if touches(text['geometry'], e['geometry'])]
                add(text, group, row['kind'], contacts)
    if ink.missing_fonts:
        unsupported.add('MISSING_EXPLICIT_FONT')
    counts = {kind: sum(p['reason'] == kind for p in pairs)
              for kind in ('text_text', 'text_view', 'text_annotation', 'text_geometry')}
    incomplete = bool(unsupported)
    result = {'metric': 'annotation_collision', 'metric_version': VERSION,
              'collision_policy': 'rendered-ink-contact/1', 'score_scale': '0..1',
              'status': 'not_evaluated' if incomplete else 'computed',
              'score': None if incomplete else max(0., 1-deduction_per_pair*len(pairs)),
              'reason': 'incomplete_rendering_coverage' if incomplete else 'visible_text_contact',
              'pair_count': len(pairs), 'counts': counts, 'pairs': pairs,
              'deduction_per_pair': deduction_per_pair, 'annotation_count': len(texts),
              'view_count': sum(row['kind'] == 'text_view' for row in objects.values()),
              'unsupported_types': sorted(unsupported), 'unclassified_geometry_objects': unclassified,
              'outside_native_view_objects': outside_native, 'ignored_hatches': skipped_hatches,
              'footprint_method': 'ezdxf-frontend-filled-glyphs-and-strokes/1',
              'approximate_text_extents': False, 'curve_flattening_mm': FLATTEN_MM,
              'contact_tolerance_mm': CONTACT_EPS_MM, 'edge_contact_counts': True,
              'fonts': list(ink.fonts.values()), 'missing_fonts': sorted(ink.missing_fonts),
              'source_sha256': hashlib.sha256(original).hexdigest(),
              'scope': 'modelspace text/text and text/all supported visible strokes; no line/line penalties',
              'limitations': ['glyph curves and round stroke caps are numerically approximated',
                              'font mappings and viewer rendering may differ; font identities recorded',
                              'hatch patterns, raster/outlined text and nonempty paper space are not covered',
                              'exploded ungrouped annotation segments count as separate objects']}
    if any(len(layout) for layout in doc.layouts if layout.name != 'Model'):
        result.update(status='not_evaluated', score=None, reason='nonempty_paperspace_not_supported')
    if output:
        from ezdxf.entities import DXFGraphic
        for entity in doc.entitydb.values():
            if isinstance(entity, DXFGraphic):
                entity.dxf.color = 7
                entity.dxf.discard('true_color')
                entity.dxf.discard('transparency')
        layer = 'BC_COLLISION_CHECK'
        if layer not in doc.layers:
            doc.layers.new(layer, dxfattribs={'color': 1, 'lineweight': 50})
        for x0, y0, x1, y1 in marked.values():
            doc.modelspace().add_lwpolyline([(x0,y0),(x1,y0),(x1,y1),(x0,y1)], close=True,
                dxfattribs={'layer': layer, 'color': 1, 'lineweight': 50})
        with output.open('x', encoding=doc.output_encoding, errors='dxfreplace') as stream:
            doc.write(stream)
        result['check_drawing'] = output.name
        result['marked_text_count'] = len(marked)
    if source.read_bytes() != original:
        raise RuntimeError('Submitted DXF changed during collision evaluation')
    result['seconds'] = time.perf_counter() - start
    return result
