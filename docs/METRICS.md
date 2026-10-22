# Three scores and their product

[中文设计讲解](METRICS.zh-CN.md) · [Offline HTML](METRICS.zh-CN.html)

Policy: `drawing-product/3`. All three component scores and the final score
are in **[0, 1]**. `final = reconstruction * expert_preference * collision`.
An unavailable component makes the product `null`, even if another component
is zero. A successfully evaluated failure can legitimately score zero.

## Reconstruction consistency

Keep the existing primitive-baseline correction and 48-orientation IoU search.
`b = max(IoU(reference, box), IoU(reference, sphere), IoU(reference, cylinder))`.
`q = clamp((raw_iou-b)/(1-b), 0, 1)`. If b=1, q=1 only when raw IoU=1, else q=0.
Let q be corrected IoU. Score 0 for q <= 0.90, 1 for q >= 0.995, otherwise
(q - 0.90) / 0.095. Preserve raw IoU, baseline and q. The low-level IoU artifact
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

Policy: `rendered-ink-contact/1`. Start at 1, deduct 0.05 per distinct
contact pair, floor at zero. Text/text and text/visible-geometry contacts count,
including boundary touching. Line/line contacts never deduct; in particular,
annotation lines crossing views are allowed. There is no separate layer penalty.

All visible TEXT, MTEXT, ATTRIB and decomposed DIMENSION/MULTILEADER text is
protected, including title blocks and tables. Centre lines, cutting indicators,
dimension/extension lines, leaders and arrows can collide with text. Membership
in the same dimension is not an exemption. Empty text is ignored.

Use the ezdxf drawing frontend to render glyph contours, including glyph holes,
and actual dashed strokes with resolved lineweight. A spatial index filters
candidates, then Shapely tests ink intersection or boundary contact. Glyph and
curve flattening is 0.001 mm; numerical contact tolerance is 1e-8 mm, not a
minimum clearance requirement. A positive visible gap is not penalized. Stroke
caps/joins are approximated as round. This is a specified reference rendering,
not a guarantee of identical appearance in all CAD viewers or on all printers.

Deduplicate each text/text pair; each text/view pair across edges; each
text/native annotation pair across component lines. Texts in the same annotation
are still checked against each other. Each geometry entity belongs to one group,
so one entity contact is never counted under two categories. Exploded standalone
lines lacking trustworthy parentage remain separate objects; adjacency alone
cannot prove a shared annotation. This grouping limitation is reported.

CQ `VIEW_*` blocks and hash-bound SW native view outlines assist grouping.
Unknown roles and geometry outside view outlines remain checked as standalone
geometry; wrong layers cannot silently exclude their strokes. Off/frozen layers
and invisible entities are not rendered. Input containing the reserved review-overlay layer is flagged as an invalid
review/submission mix; it cannot hide submitted text or receive a full score. Hatch patterns and outlined/raster text remain outside current scope;
unsupported entities and nonempty paper space make the score unavailable.

Record requested/resolved font basenames and available font-file hashes.
Documented, available SHX font mappings are accepted. Missing requested fonts
(including SHX with no available mapping) leave the score null; fallback-rendered
contacts are diagnostic only. Font availability is an environment limitation,
not an AI penalty. Inline MTEXT font overrides and platform-dependent font
resolution still require calibration before cross-platform score comparisons.

Every contact records text content, entity IDs, layers, category and coordinates.
`drawing-collision-check.dxf` is a separate monochrome copy with red frames
around affected texts on `BC_COLLISION_CHECK`. A color PNG is produced when
available. Original submissions and historical reports are never overwritten.

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

## Local regrading of CQ and SolidWorks history

`python -m evaluation.rescore --dxf INPUT.dxf --iou-json IOU.json --output-dir NEW_DIRECTORY`
reuses a trusted historical raw IoU and primitive baseline, applies the new
0.90..0.995 mapping, and runs collision checks. It makes no model request and
writes a new metrics.json, marked DXF/PNG and index.html. It does not recompute
3D overlap. For SW add `--native-views drawing.json`. For CQ expert scoring also
supply `--expert-json`, `--reference-step` and `--operation-log`; final view
evidence is regenerated and checked. A native SW expert-evidence adapter remains
pending; providing an arbitrary JSON must not promote it to verified evidence.
Missing/unsupported expert evidence leaves both preference and product null.
