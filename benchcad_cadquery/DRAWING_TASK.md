# Drawing task

Read `input/model.step` and create an accurate, professional engineering drawing
in `output/drawing.dxf` using millimetres. Aim for expert-level engineering judgment
and presentation. Represent the part's geometry and necessary feature information
completely and correctly. Dimension all necessary features without omissions or
redundant dimensioning; use a complete, consistent dimensioning scheme rather than
repeating the same requirement in several views. Inspect geometry using CadQuery/OCP.
Choose appropriate main projections and genuine sections, a consistent projection
convention, suitable scales, and a balanced layout. Keep dimensions and notes legible,
avoid overlapping annotations, and keep all drawing content within the sheet frame.
Do not invent cut faces or substitute a projected outline for a section.

Read TOOLS.md. You may write ordinary Python using the supplied libraries and
toolkit. Files persist between executions; variables do not. Choose view placement,
scale, dimensions, and annotations yourself. You may inspect your own generated
DXF and preview it. Before submitting, render and inspect the latest drawing image,
then correct visible omissions, overlaps, or layout errors. Save preview PNGs in
the workspace root or directly in `output/` so they are returned as image feedback.
There is no expert drawing, ground truth, or score feedback.

Each response is exactly one nonempty `python` or `submit` fenced code block.
The contents are executable Python. A normal block returns execution feedback;
a submit block executes once and ends the episode. Explicitly save the DXF before
submission. The controller checks the DXF and renders the final PNG automatically.
Do not submit a filename or prose. No submission repair loop is provided.

Example last response (only after the file has already been saved):

```submit
from pathlib import Path
assert Path('output/drawing.dxf').is_file()
```

The final deliverable is the DXF and its controller-rendered PNG.

Default limits: 30 decision responses, 1800 seconds for this stage, 300 seconds
per code execution, 1 CPU, 4096 MiB memory. The system prompt supplies any lower
limits. Invalid formats and failed code still consume decisions. Each execution
uses a fresh non-root Linux container with no network and a read-only system.
