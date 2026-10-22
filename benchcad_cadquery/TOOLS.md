# Python tools available to either agent

Python 3.11; CadQuery 2.7.0/OCP; ezdxf 1.4.4; PyMuPDF 1.28.2; Pillow;
NumPy/SciPy. Raw CadQuery, OCP, and ezdxf are allowed. No network, shell-side
package installs, credentials, host home, GT, or Docker socket are available.
The working directory is `/workspace`. Import the helpers documented below.

## Drawing from a STEP (drawing stage only)

`from benchcad_cadquery.drawing import Drawing, render`

`Drawing(step='input/model.step', path='output/drawing.dxf', title='...')`
loads the shape and creates an A3-sized millimetre DXF with frame and dimension
style. `d.model` is the CadQuery shape; `d.doc` is the ezdxf document. `d.save()`
writes DXF and an explicitly untrusted `.state.json` companion. To continue in a
later execution: `d = Drawing.open()`; both files must still exist. Raw ezdxf
workflows do not need this state. State is not verified evidence or GT and is
never transferred to reconstruction. The toolkit also writes
`drawing.operations.jsonl` automatically. Keep this file when continuing a
drawing. Do not author a separate scoring JSON: the evaluator replays these
operations and regenerates final view geometry before using the derived JSON.
Finish edits with `d.save()` so the final DXF checksum is recorded. Do not rename
or directly alter generated view blocks; annotations may be edited through
ezdxf before that save. Unsupported custom view geometry is explicitly unscored.

When the controller supplies `input/template.dxf`, `Drawing()` automatically
loads its blank sheet instead of creating another frame. An explicit path is
also accepted: `Drawing(template='input/template.dxf')`. The loader copies the
single paper layout into model space at unchanged millimetre coordinates,
preserving title blocks, attributes, layers, fonts, and dimension styles. It
excludes viewports and non-printing guides. A template with nonempty model space,
non-mm units, or ambiguous paper layouts is rejected. For an explicitly chosen
layout, use `load_blank_template(path, layout_name=...)` from
`benchcad_cadquery.templates` in a raw ezdxf workflow. Existing styles/layers are
preserved; `Drawing` adds only missing toolkit styles/layers. The `title` argument
applies only to the fallback frame; update template title-block attributes yourself.
Inspect and clear unused view placeholders/product fields as appropriate. The
working state records the template SHA256, not its original local path. The
template is an optional drawing input and is never transferred separately to
the reconstruction agent.

The bottom-right product-specification table is optional. In the optional-table
template its cells are blank: you may fill, relabel, move, resize, retain, or
delete the table to suit the part. Do not invent specifications to fill cells.
This permission concerns the product table, not a requirement to remove the
sheet border, title block, technical notes, or actual dimensions.
After `Drawing()` loads the template, edit `d.doc.modelspace()`; the source
paper-layout name is not the working layout. For example, to remove only a
product-table block insert when you decide it is unnecessary:

```python
for entity in list(d.doc.modelspace().query('INSERT')):
    if 'PRODUCT_SPEC' in entity.dxf.name.upper():
        d.doc.modelspace().delete_entity(entity)
d.save()
```

`d.projection(name, normal=(0,0,1), xdir=(1,0,0), position=(100,180), scale=1)`
uses OCP hidden-line removal. The viewer is on the +normal side. Local drawing X
is xdir projected perpendicular to normal; Y is normal cross X. The origin is
the model origin, placed at `position` in paper millimetres. It creates a block
and returns view metadata. Visible straight lines and complete circles are
native entities; other curves are sampled. Hidden edges, seam/smooth-edge rules,
and deduplication are limited. Do not assume a production CAD drawing result.

Useful main-view orientations (choose positions/scales for the actual part):

| View | normal | xdir |
| --- | --- | --- |
| Top | (0,0,1) | (1,0,0) |
| Front | (0,-1,0) | (1,0,0) |
| Right | (1,0,0) | (0,1,0) |

`d.full_section(name, point, normal, xdir=(1,0,0), position=(280,180), scale=1, keep_side=-1)`
uses a real plane/solid intersection and half-space cut, then projects the
remaining solid and hatches material faces including inner loops. Point/normal
are in model coordinates. Default keep_side=-1 retains material opposite the
normal; +1 flips viewing direction. A plane missing material raises an error.
The helper does not automatically draw a cutting-plane indicator: add correct
labels/arrows yourself. There is no half-section or offset-section helper yet.
You may construct genuine half/offset sections with raw OCP/ezdxf when needed.
Never fake a section by hatching an uncut projection. Main views, full sections,
and genuine manually constructed half sections are permitted. The current
automatic evidence verifier supports toolkit `projection` and `full_section`
only. Manually constructed or modified view geometry cannot receive an expert
preference score until an appropriate geometry verifier exists; it is recorded
as unscored, never as a perfect score. Dimensions and notes can be edited freely.

`d.linear_dimension(p1, p2, base, angle=0, view=None, text='<>')` creates and renders
a native DXF DIMENSION. Points and base are paper-space coordinates, not 3D model
points. Angle is degrees. Passing a view name sets dimlfac=1/view.scale so the
displayed measurement is in model millimetres. Prefer automatic measurement text.
For radius/diameter/angular dimensions use `d.doc.modelspace().add_radius_dim`,
`add_diameter_dim`, or `add_angular_dim_3p`, followed by `.render()`. Account for
view scale in their dimension overrides. Add notes with `d.note(text, position)`.
`d.view(name)` returns metadata; `d.delete_view(name)` rejects dependent dimensions.

Small projection/dimension example for a centered 60 x 40 x 8 mm plate:

```python
from benchcad_cadquery.drawing import Drawing
d = Drawing()
d.projection('top', position=(100,180))
d.full_section('A_A', point=(0,0,0), normal=(0,1,0), position=(280,180))
d.linear_dimension((70,160), (130,160), base=(100,150), view='top')
d.save()
d.preview('preview.png')
```

These dimensions are a generic example, not facts about the current input.

## Preview and DXF inspection (both stages)

`from benchcad_cadquery.drawing import render`

`render('input/drawing.dxf', 'preview.png')` renders a DXF with ezdxf/PyMuPDF.
If model space is empty, it loads the single millimetre paper-space template
using the same rules as `Drawing()`. Thus `render('input/template.dxf', 'template.png')`
can preview the supplied blank sheet. Ambiguous paper layouts are rejected;
the source DXF is never modified.
`d.preview('preview.png')` first saves the current drawing. A changed PNG at the
workspace root or directly in `output/` is validated and snapshotted outside the writable workspace before it is
returned as feedback. Use PNG only: maximum 12 MiB, 4096 pixels per dimension,
16 million pixels, one frame; at most 32 changed/invalid observations per call
across both locations. Other nested directories are not scanned.
The API request including accumulated history and encoded images has a 64 MiB
limit. Exceeding it stops the run before sending; history is not silently trimmed.
The standard render is 2100 x 1500 pixels. Submission renders automatically.

```python
import ezdxf
doc = ezdxf.readfile('input/drawing.dxf')
for e in doc.modelspace().query('DIMENSION'):
    print(e.dxf.dimtype, e.dxf.text, e.get_measurement())
for e in doc.modelspace().query('INSERT'):
    print(e.dxf.name, e.dxf.insert, e.dxf.xscale)
    print([v.dxftype() for v in e.virtual_entities()][:30])
```

## Reconstruction with CadQuery

`from benchcad_cadquery.inspection import crop_image`

`crop_image('input/drawing.png', (left,top,right,bottom))` writes
an `output/detail_<id>.png` file per source/region, and returns its path and size.
Different crops in one response keep separate files and are returned together.
Pass a third `png` argument only when you want an explicit output name; reusing
that name overwrites the previous crop. Coordinates are integers in the original image, not its
on-screen display size. This preserves original pixels; it does not recover
missing detail. For detail absent from the PNG, inspect the corresponding DXF
entities or write a custom DXF rendering script.

```python
import cadquery as cq
# Replace the example parameters and features with your drawing interpretation.
model = cq.Workplane('XY').box(60,40,8).faces('>Z').workplane().hole(10)
cq.exporters.export(model, 'output/model.step')
```

Keep reusable modeling code in a workspace file, edit it between rounds, and
submit the saved result after checking it against the task requirements.

Use CadQuery solids, sketches, booleans, and raw OCP as needed. No helper imports
an absent original STEP for reconstruction. Only files supplied in this stage
may be inspected. Keep stdout concise; combined stdout/stderr is capped at 8 MiB
per Docker invocation and overflow terminates the container. Execution code,
controller records, and prior observation snapshots are outside agent access.
