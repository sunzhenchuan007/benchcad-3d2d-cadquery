# Independent reconstruction evaluation

`normalized_iou.py` runs after the reconstruction episode has ended. It reads
the reference STEP and frozen reconstructed STEP in a separate evaluator.
Neither the reference nor this evaluator is mounted into the reconstruction
worker. Expert preference and annotation collision now run separately on the
final drawing; see [the three-component policy](../docs/METRICS.md). All new
summary scores are in 0..1 and the aggregate is their product; missing evidence
leaves the aggregate null. Low-level historical IoU artifacts retain their
explicitly named `reconstruction_score_100` field.

The adapter uses the geometry modules from
[`BenchCAD-org/benchcad-2-main`](https://github.com/BenchCAD-org/benchcad-2-main)
at commit `8732095d5cf6f36875d02253cf85f8ab7e41ae78`. Supply that checkout as
`--vendor`; third-party source is not bundled here. Review its license before
redistributing it.

The reported IoU is the maximum of 24 axis-aligned proper rotations and 24
reflected orientations on a 128-cell-per-axis voxel grid. Each solid is centered
and uniformly scaled by its own longest bounding-box extent. Reflection is
applied across XY before the second rotation search. Both search results and
the selected orientation are retained. This is not arbitrary-angle registration.
Absolute size and handedness are intentionally removed from this metric;
volume and sorted-extent ratios are separate diagnostics.

The final reconstruction score uses the BenchCAD 2 primitive baseline:
`b = max(IoU(reference, box), IoU(reference, sphere), IoU(reference, cylinder))`
from the pinned reference context. The corrected value is
`q = clamp((raw_iou - b) / (1 - b), 0, 1)`. When `b == 1`, the upstream rule
returns 1 only for raw IoU 1, and 0 otherwise. Then:

* `q <= 0.90`: 0 points.
* `q >= 0.99`: 100 points.
* Otherwise: `100 * (q - 0.90) / 0.09` points.

Raw IoU, all primitive baselines, corrected IoU, and the 0..100 reconstruction
score are all retained. Metric version: `normalized-iou48-primitive-threshold/2`.
This reconstruction score is not an aggregate engineering score. The separate
expert-DXF-to-STEP baseline correction remains disabled.

Build an evaluator image separately from the drawing image:

```sh
docker build --build-arg CAD_IMAGE=benchcad-cadquery:local \
  -t benchcad-cadquery:evaluator evaluation
```

Inside a trusted evaluator environment with those dependencies:

```sh
python evaluation/normalized_iou.py /reference/model.step /submission/model.step \
  --vendor /vendor/benchcad-2-main --output /results/iou.json
```

Use read-only reference/submission/vendor mounts, a separate writable results
directory, no network or credentials, and resource/time limits. Before reporting
model scores, run the same adapter with the reference file as both inputs.
That identity control must produce 1.0. Geometry, dependency, timeout, and memory
errors are infrastructure failures; do not replace them with a fabricated zero.
The adapter refuses to overwrite existing results.

## Automatic closeout

Pass `--reconstruct --iou-vendor /path/to/pinned-checkout` to the main run command.
The controller automatically validates an identity control, evaluates the frozen
reconstruction in a separate credential-free Docker container, merges the result
into `summary.json`, and writes `report.html`. `tools/benchcad.py setup` now builds
the separate evaluator image. The vendor checkout is a controller-side prerequisite;
it is never supplied to either model stage. Without a vendor path, the report
explicitly says `evaluation_not_configured`.

Both identity and candidate evaluations share `--evaluation-seconds` (default 400).
Infrastructure errors produce a null score with failure status, not a zero score.
For a previously finished run, `benchcad evaluate --run-dir ... --reference-step ...
--iou-vendor ...` adds evaluation records and an evaluated summary/report without
overwriting the original summary or calling the model API. Existing evaluation
directories are never silently reused.
