"""Vector ink geometry from the same ezdxf frontend used by the review renderer.

Contours are flattened at 0.001 mm. This is a rendering-based measurement, not
an exact model of every CAD viewer, printer or font rasterizer.
"""
from pathlib import Path
import hashlib

FLATTEN_MM = .001
CONTACT_EPS_MM = 1e-8  # numerical precision only, not a readability margin


def filled_geometry(paths):
    from shapely.geometry import GeometryCollection, Polygon
    from shapely import make_valid
    result = GeometryCollection()
    # Even/odd fill preserves glyph counters (the holes in O, B, 8, etc.).
    for path in paths:
        for sub in path.sub_paths():
            points = [(p.x, p.y) for p in sub.flattening(FLATTEN_MM)]
            if len(points) >= 3:
                result = result.symmetric_difference(make_valid(Polygon(points)))
    return result


class InkRenderer:
    def __init__(self, doc):
        from ezdxf.addons.drawing import RenderContext
        self.context = RenderContext(doc)
        self.doc = doc
        self.fonts = {}
        self.missing_fonts = set()

    def font_record(self, entity):
        from ezdxf.fonts import fonts
        face = self.context.resolve_font(entity)
        name = face.filename if face else ''
        style = self.doc.styles.get(entity.dxf.get('style', 'Standard'))
        requested = style.dxf.font
        # Allow an available documented SHX mapping, not an arbitrary fallback.
        expected = (fonts.resolve_shx_font_name(requested, self.context.shx_resolve_order)
                    if requested and fonts.is_shx_font_name(requested) else requested)
        if expected and not fonts.font_manager.has_font(expected):
            self.missing_fonts.add(Path(requested).name)
        digest = None
        try:
            actual = fonts.font_manager.get_ttf_font(name)
            filename = actual.reader.file.name
            digest = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
        except (AttributeError, OSError, TypeError, fonts.FontNotFoundError):
            pass
        record = {'requested': Path(requested).name, 'resolved': Path(name).name,
                  'sha256': digest, 'missing_requested_font': Path(requested).name in self.missing_fonts}
        self.fonts[(requested, name)] = record

    def geometry(self, entity, layer, inherited_weight=None, text=False):
        from ezdxf.addons.drawing import Frontend, config
        from ezdxf.addons.drawing.recorder import (
            Recorder, FilledPathsRecord, PathRecord, PointsRecord, SolidLinesRecord,
        )
        from shapely.geometry import LineString, Point, Polygon
        from shapely import make_valid, union_all
        clone = entity.copy()
        clone.dxf.layer = layer
        props = self.context.resolve_all(clone)
        if clone.dxf.lineweight == -2 and inherited_weight is not None:
            props.lineweight = inherited_weight
        if text:
            self.font_record(clone)
        recorder = Recorder()
        frontend = Frontend(self.context, recorder, config=config.Configuration(
            max_flattening_distance=FLATTEN_MM,
            text_policy=config.TextPolicy.FILLING,
        ))
        frontend.draw_entity(clone, props)
        shapes = []

        def stroke(points, width):
            if len(points) == 1:
                shapes.append(Point(points[0]).buffer(width / 2))
            elif len(points) > 1:
                shapes.append(LineString(points).buffer(width / 2, quad_segs=16))

        for record in recorder.records:
            weight = max(.01, recorder.properties[record.property_hash].lineweight)
            if isinstance(record, FilledPathsRecord):
                shapes.append(filled_geometry(record.paths))
            elif isinstance(record, PathRecord):
                for sub in record.path.sub_paths():
                    stroke([(p.x, p.y) for p in sub.flattening(FLATTEN_MM)], weight)
            elif isinstance(record, SolidLinesRecord):
                points = [(p.x, p.y) for p in record.lines.vertices()]
                for i in range(0, len(points), 2):
                    stroke(points[i:i+2], weight)
            elif isinstance(record, PointsRecord):
                points = [(p.x, p.y) for p in record.points.vertices()]
                if len(points) > 2:
                    shapes.append(make_valid(Polygon(points)))
                else:
                    stroke(points, weight)
            else:
                raise ValueError('Unsupported rendered primitive')
        return union_all(shapes)


def touches(a, b):
    return a.intersects(b) or a.distance(b) <= CONTACT_EPS_MM
