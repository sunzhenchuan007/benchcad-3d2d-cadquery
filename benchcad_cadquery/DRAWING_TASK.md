# Drawing task

Read `input/model.step` and create a useful engineering drawing in
`output/drawing.dxf` using millimetres. Select main orthographic projections,
appropriate genuine sections, and sufficient native dimensions to describe the
part for an independent reconstruction agent. Inspect geometry using CadQuery/OCP.
Do not invent cut faces or substitute a projected outline for a section.

Read TOOLS.md. You may write ordinary Python using the supplied libraries and
toolkit. Files persist between executions; variables do not. Choose view placement,
scale, dimensions, and annotations yourself. You may inspect your own generated
DXF and preview it. There is no expert drawing, ground truth, or score feedback.

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

Only the final DXF and its trusted rendered PNG go to the reconstruction agent.
No source STEP, drawing Python, working-state JSON, or drawing logs are transferred.

Default limits: 30 decision responses, 1800 seconds for this stage, 300 seconds
per code execution, 1 CPU, 4096 MiB memory. The system prompt supplies any lower
limits. Invalid formats and failed code still consume decisions. Each execution
uses a fresh non-root Linux container with no network and a read-only system.
