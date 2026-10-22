# Generic task definitions

`drawing.md` tells the drawing agent how to turn the supplied STEP into a DXF.
`reconstruction.md` tells a separate agent how to reconstruct a STEP from the
frozen DXF and PNG. Neither file defines a dataset or contains Case13 answers.

The controller copies the selected definition to the stage's `TASK.md` and
provides the relevant parts of the shared `../TOOLS.md`. These resources ship
inside the Python package. The reconstruction workspace receives no source STEP
or drawing-agent history.

The public input example lives separately in `examples/case13/` at repository
root. Runtime budgets are defined in `config.example.toml` and validated by the
controller; they are not case-specific task instructions.
