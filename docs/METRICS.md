# Three scores and their product

Policy: `drawing-product/1`. All three component scores and the final score
are in **[0, 1]**. `final = reconstruction * expert_preference * collision`.
An unavailable component makes the product `null`, even if another component
is zero. A successfully evaluated failure can legitimately score zero.

## Reconstruction consistency

Keep the existing primitive-baseline correction and 48-orientation IoU search.
Let q be corrected IoU. Score 0 for q <= 0.90, 1 for q >= 0.99, otherwise
(q - 0.90) / 0.09. Preserve raw IoU, baseline and q. The low-level IoU artifact
keeps `reconstruction_score_100` for historical compatibility; new summaries
divide it by 100 and declare `score_scale: "0..1"`.

## Expert preference

Expert input is the annotation page's `benchcad-section-position-draft/5` JSON.
It stays evaluator-only, never in drawing or reconstruction model inputs.
Use `--expert-json <expert.json>` (or `[evaluation].expert_json` in TOML).

For each complete reference scheme, G is its major plus local view count.
Compare the final AI count A, allowing one extra local view only when the
major counts are equal. `count_deduction = max(0, A-G-allowance)/G`.
Choose the least deduction across whole reference schemes; do not mix the
major count of one scheme with the local count of another. G=0 is missing
expert configuration, not an infinite denominator or automatic full score.

Each missing required section deducts 0.2. An alternative `one_of` group counts
as one requirement, met if any member matches. Optional sections do not deduct.
One final section may satisfy multiple geometric requirements.
`preference = max(0, 1-count_deduction-0.2*missing_requirements)`.

Match geometric constraints, not labels, axis IDs or JSON row order:
- contains_axis: plane contains the axis origin within position tolerance and
  its unit normal is perpendicular to the axis within 1e-6 dot-product tolerance;
- contains_lines: both lines satisfy containment;
- crosses_axis_at_station: the plane intersects the axis at the specified
  station (distance along its unit direction), within position tolerance;
- optional plane-normal angle is compared sign-independently.

The toolkit automatically logs successful view creations/deletions and exports.
The controller captures the log on submission, outside the writable workspace.
Capture alone does not establish truth: the evaluator regenerates each final
view from the original STEP and compares block geometry and placement to the
frozen DXF. It then generates `drawing-evaluation/sections.json`.
No AI-written JSON answer or arbitrary stdout parsing is used.

Current evidence coverage: toolkit whole-model projections and full sections.
Local/detail/half-section counts are supported by the scoring formula, but their
raw custom geometry is not yet supported by this verifier. Unknown geometry,
missing logs, unrecorded view changes, or unsupported mandatory ordinary-view
requirements remain explicitly unscored. Historical state snapshots are not
silently promoted to trusted operation logs.

## Annotation collision

Start at 1, deduct 0.05 per distinct unordered annotation pair, floor at zero.
Count text/text overlap or another annotation's lines/arrows crossing text.
Multiple contacts between the same pair count once. Do not count model-view
geometry, line/line crossings, or contacts within one dimension object.

Use final DXF DIMENSION objects and text entities, including nested blocks.
Exclude sheet/title/table geometry and view blocks. Text footprints are ezdxf
oriented extent estimates, not exact glyph ink. Require >0.05 mm penetration;
simple boundary contact is ignored. The footprint method and pair locations
are saved with the result. Pin the CAD image/fonts when comparing runs.
Unresolved LEADER/MULTILEADER entities return unscored, not a partial full score.
Exploded annotations without semantic grouping cannot be treated as complete
dimension objects: retain DIMENSION entities via the supplied toolkit.

## Runtime and records

Drawing metrics run in a separate network-free CAD container, default 300 s,
using the configured CPU/RAM limits. Set `--drawing-evaluation-seconds` to change
this evaluator-only budget. This does not consume model decision rounds.
The final report lists all three scores and their product, with detailed
deductions in `drawing-evaluation/quality.json`.

To score an existing run without new model calls:

```sh
python -m benchcad_cadquery.cli score --run-dir runs/example \
  --image benchcad-cadquery:local --reference-step private/model.step \
  --expert-json private/expert.json
```

This creates `scored-summary.json` and `scoring-report.html`, preserves previous
summaries, and refuses to overwrite an existing drawing evaluation. It reuses
and validates the existing low-level IoU artifact. Missing expert evidence
leaves preference and the product null while still reporting collision results.
The workbench's legacy transport key `section_match` carries expert preference;
the main summary uses the explicit key `expert_preference`.
