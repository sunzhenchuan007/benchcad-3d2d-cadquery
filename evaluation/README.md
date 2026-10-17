# Independent reconstruction evaluation

`normalized_iou.py` runs after the reconstruction episode has ended. It reads
the reference STEP and frozen reconstructed STEP in a separate evaluator.
Neither the reference nor this evaluator is mounted into the reconstruction
worker. Section matching and annotation collision remain unimplemented interfaces.

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
volume and sorted-extent ratios are separate diagnostics. No expert-drawing
baseline division or aggregate engineering score is applied.

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
