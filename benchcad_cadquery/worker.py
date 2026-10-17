"""Trusted commands invoked with python -I, never imported from the workspace."""
from pathlib import Path
import argparse
import json
import math
import sys

# -I discards PYTHONPATH and the working directory. Only the allowlisted,
# read-only source snapshot is added; no agent modules participate in validation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def validate_drawing():
    import ezdxf
    from ezdxf import bbox
    from ezdxf.disassemble import recursive_decompose
    from benchcad_cadquery.drawing import render
    path = Path('drawing.dxf')
    doc = ezdxf.readfile(path)
    audit = doc.audit()
    if audit.errors or audit.fixes:
        raise ValueError('DXF audit must be clean; finalization never repairs drawings')
    kinds = {'LINE', 'CIRCLE', 'ARC', 'ELLIPSE', 'LWPOLYLINE', 'POLYLINE', 'SPLINE', 'HATCH', 'SOLID', '3DFACE'}
    geometry = []
    for count, entity in enumerate(recursive_decompose(doc.modelspace())):
        if count >= 250000:
            raise ValueError('DXF entity limit exceeded')
        if entity.dxftype() in kinds:
            geometry.append(entity)
    bounds = bbox.extents(geometry)
    if not geometry or not bounds.has_data or not math.isfinite(bounds.size.magnitude) or bounds.size.magnitude <= 0:
        raise ValueError('DXF must contain nonempty geometric extents')
    render(path, 'drawing.png')
    return {'kind': 'drawing', 'audit_errors': 0, 'audit_fixes': 0,
            'geometry_entities': len(geometry), 'engineering_correctness_verified': False}


def validate_reconstruction():
    import cadquery as cq
    shapes = cq.importers.importStep('model.step').vals()
    solids = [solid for shape in shapes for solid in shape.Solids()]
    volumes = [solid.Volume() for solid in solids]
    if (not solids or any(not shape.isValid() for shape in shapes)
            or any(not solid.isValid() for solid in solids)
            or any(not math.isfinite(volume) or volume <= 0 for volume in volumes)):
        raise ValueError('STEP must contain valid positive-volume solids')
    return {'kind': 'reconstruction', 'solids': len(solids), 'volume_mm3': sum(volumes),
            'geometric_similarity_verified': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['validate-drawing', 'validate-reconstruction', 'fixture', 'doctor'])
    args = parser.parse_args()
    if args.command == 'fixture':
        import cadquery as cq
        plate = cq.Workplane('XY').box(60, 40, 8).faces('>Z').workplane().hole(10)
        cq.exporters.export(plate, 'model.step')
        result = {'fixture': 'plate_60_40_8_hole_10', 'trusted_replay': True}
    elif args.command == 'doctor':
        import importlib.metadata
        import cadquery, ezdxf, pymupdf, PIL, tiktoken, numpy, scipy  # noqa: F401
        # Building the image also provisions this offline tokenizer cache.
        result = {'python': sys.version.split()[0], 'packages': {
            name: importlib.metadata.version(name) for name in
            ('cadquery', 'ezdxf', 'PyMuPDF', 'Pillow', 'tiktoken', 'numpy', 'scipy')}}
    else:
        result = validate_drawing() if args.command == 'validate-drawing' else validate_reconstruction()
        Path('validation.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
