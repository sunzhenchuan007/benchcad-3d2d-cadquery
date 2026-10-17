"""Trusted smoke drawing example; execute from a fixture workspace, never on import."""
from benchcad_cadquery.drawing import Drawing

d = Drawing(title='Parametric plate fixture - mm')
d.projection('top', normal=(0, 0, 1), position=(100, 180))
d.projection('front', normal=(0, -1, 0), position=(100, 100))
d.projection('right', normal=(1, 0, 0), xdir=(0, 1, 0), position=(200, 100))
d.full_section('A_A', point=(0, 0, 0), normal=(0, 1, 0), position=(290, 180))
d.linear_dimension((70, 160), (130, 160), base=(100, 150), view='top')
d.linear_dimension((70, 160), (70, 200), base=(60, 180), angle=90, view='top')
d.linear_dimension((70, 96), (70, 104), base=(60, 100), angle=90, view='front')
dim = d.doc.modelspace().add_diameter_dim(center=(100, 180), radius=5, angle=45, dimstyle='BC_DIM')
dim.render()
d.note('THRU hole; full section A-A: plane Y=0, viewing from +Y', (230, 155), height=3)
d.save()
# Optional feedback in a normal python round; submit will render automatically.
# d.preview('preview.png')
