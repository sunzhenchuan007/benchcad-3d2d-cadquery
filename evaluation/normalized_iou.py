"""Evaluator-only adapter to the previously pinned BenchCAD 2 IoU functions.

The external vendor checkout is never exposed to the drawing/reconstruction
agent. No expert-DXF baseline or topology/composite score is computed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

VERSION = "normalized-iou48-mirror-max/1"
UPSTREAM_COMMIT = "8732095d5cf6f36875d02253cf85f8ab7e41ae78"


def inspect(path):
    import cadquery as cq
    shape = cq.importers.importStep(str(path)).val()
    bounds = shape.BoundingBox()
    info = {"sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "valid": shape.isValid(), "solids": len(shape.Solids()),
            "volume_mm3": shape.Volume(),
            "extents_mm": [bounds.xlen, bounds.ylen, bounds.zlen]}
    return shape, info


def evaluate(reference, candidate, vendor):
    sys.path.insert(0, str(Path(vendor).resolve()))
    from envs.common import part_metric
    a, source = inspect(reference)
    b, submission = inspect(candidate)
    for label, info in (("reference", source), ("candidate", submission)):
        if not info["valid"] or not info["solids"] or info["volume_mm3"] <= 0:
            raise ValueError(f"Invalid {label} solid")
    context = part_metric.reference_iou_context(a, frame="own")
    proper = part_metric.candidate_iou(context, b, search=True, frame="own")
    mirrored = part_metric.candidate_iou(context, b.mirror("XY"), search=True, frame="own")
    reflected = float(mirrored["iou24"]) > float(proper["iou24"])
    selected = mirrored if reflected else proper
    import numpy as np
    transform = np.asarray(selected["rotation"])
    if reflected:
        transform = transform @ np.diag([1., 1., -1.])
    import math
    scores = [float(proper["iou24"]), float(mirrored["iou24"])]
    if any(not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
        raise ValueError("Invalid IoU result")
    return {"status": "computed", "metric_version": VERSION,
            "upstream_commit": UPSTREAM_COMMIT, "grid": part_metric.GRID,
            "iou": max(scores), "iou24": scores[0], "iou24_reflected": scores[1],
            "reflection_used": reflected, "selected_transform": transform.tolist(),
            "normalization": "own bbox centre; uniform longest-extent scaling",
            "search": {"proper_rotations": 24, "reflected_rotations": 24,
                       "tie_break": "rotation_only"},
            "reference": source, "candidate": submission,
            "absolute_size_diagnostics": {
                "volume_ratio": submission["volume_mm3"] / source["volume_mm3"],
                "sorted_extent_ratios": [y / x for x, y in zip(
                    sorted(source["extents_mm"]), sorted(submission["extents_mm"]))]},
            "expert_baseline_correction": "not_evaluated",
            "formal_score": False, "overall_score": None,
            "limitations": ["IoU removes absolute scale and translation",
                            "48 axis-aligned orientations, not arbitrary registration",
                            "Mirror-max IoU ignores chirality",
                            "No section, collision, topology or composite score"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--vendor", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Refusing to replace an existing evaluation")
    result = evaluate(args.reference, args.candidate, args.vendor)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "iou", "iou24", "reflection_used")}))


if __name__ == "__main__":
    main()
