"""Deferred section/collision scorers and an existing-IoU process adapter."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol
import json
import math
import subprocess


@dataclass(frozen=True)
class MetricResult:
    metric: str
    status: str
    score: float | None = None
    reason: str = ""


class SectionMatcher(Protocol):
    def evaluate(self, drawing_json: Path, gt_json: Path) -> MetricResult: ...


class CollisionScorer(Protocol):
    def evaluate(self, drawing_dxf: Path) -> MetricResult: ...


class DeferredSectionMatcher:
    def evaluate(self, drawing_json: Path, gt_json: Path | None = None) -> MetricResult:
        # Deliberately do not read GT or calculate any geometric match.
        return MetricResult("section_match", "not_evaluated", reason="deferred_by_user")


class DeferredCollisionScorer:
    def evaluate(self, drawing_dxf: Path) -> MetricResult:
        return MetricResult("annotation_collision", "not_evaluated", reason="deferred_by_user")


def pending_metrics(bundle: Path) -> dict:
    return {
        "section_match": asdict(DeferredSectionMatcher().evaluate(bundle / "drawing.json")),
        "annotation_collision": asdict(DeferredCollisionScorer().evaluate(bundle / "drawing.dxf")),
        "reconstruction_iou": asdict(MetricResult("reconstruction_iou", "not_evaluated",
                                                  reason="awaiting_independent_AI_reconstruction")),
        "overall_score": None, "formal_score": False,
    }


def run_existing_iou(command: list[str], reference: Path, candidate: Path,
                     output: Path, timeout: float = 600) -> dict:
    """Call the existing evaluator's CLI, without copying local configs/secrets.

    command is an argv prefix, e.g. [python, evaluate_iou.py]. This lives on
    the evaluator side only; neither reference nor command enters model input.
    """
    if not command or not reference.is_file() or not candidate.is_file():
        raise ValueError("A command, reference STEP and independently reconstructed STEP are required")
    if output.exists():
        raise FileExistsError("Refusing to accept or overwrite a stale IoU result")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([*command, str(reference), str(candidate), "--output", str(output)],
                   check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    result = json.loads(output.read_text(encoding="utf-8"))
    validate_iou_result(result)
    return result


def validate_iou_result(result: dict) -> None:
    score = result.get("iou")
    if result.get("status") != "computed" or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("IoU evaluator did not return a finite computed score")
    from .scoring import VERSION, reconstruction_score
    if result.get("metric_version") != VERSION:
        raise ValueError("Unexpected IoU metric version")
    expected = reconstruction_score(score, result.get('primitive_baseline'))
    for key in ('raw_iou', 'corrected_iou', 'reconstruction_score_100'):
        value = result.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not math.isclose(value, expected[key], abs_tol=1e-10):
            raise ValueError('Inconsistent reconstruction scoring fields')
