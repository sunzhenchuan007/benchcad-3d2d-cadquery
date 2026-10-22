"""Regression checks for visible edge contact and the reported Case13 misses."""
import unittest
from tests import test_drawing_metrics as fixtures


class InkContactTests(unittest.TestCase):
    setUp = fixtures.CollisionTests.setUp
    score = fixtures.CollisionTests.score
    text = fixtures.CollisionTests.text
    def ink(self, entity):
        from evaluation.ink_geometry import InkRenderer
        return InkRenderer(self.doc).geometry(entity, entity.dxf.layer, text=True)

    def test_text_edge_contact_counts_but_positive_gap_does_not(self):
        a = self.m.add_text('I', dxfattribs={'height': 3})
        x0, y0, x1, y1 = self.ink(a).bounds
        b = self.m.add_text('I', dxfattribs={'height': 3, 'insert': (x1-x0, 0)})
        self.assertEqual(self.score()['counts']['text_text'], 1)
        b.dxf.insert = (x1-x0+.02, 0, 0)
        self.assertEqual(self.score()['pair_count'], 0)

    def test_stroke_width_touch_counts_but_gap_does_not(self):
        t = self.m.add_text('I', dxfattribs={'height': 3})
        x0, y0, x1, y1 = self.ink(t).bounds
        line = self.m.add_line((x0-.125, y0-1), (x0-.125, y1+1),
                               dxfattribs={'lineweight': 25})
        self.assertEqual(self.score()['pair_count'], 1)
        line.translate(-.02, 0, 0)
        self.assertEqual(self.score()['pair_count'], 0)

    def test_glyph_hole_is_not_filled_bounding_box(self):
        from shapely.geometry import Point
        t = self.m.add_text('O', dxfattribs={'height': 10})
        g = self.ink(t)
        x0, y0, x1, y1 = g.bounds
        x, y = (x0+x1)/2, (y0+y1)/2
        self.assertGreater(g.distance(Point(x,y)), .2)
        self.m.add_line((x-.05,y), (x+.05,y), dxfattribs={'lineweight': 5})
        self.assertEqual(self.score()['pair_count'], 0)

    def test_title_text_extension_line_is_checked(self):
        self.doc.layers.new('BC_TITLEBLOCK')
        self.m.add_text('ALL DIMENSIONS IN mm', dxfattribs={
            'height':3, 'layer':'BC_TITLEBLOCK'})
        self.doc.layers.new('DIMENSIONS')
        self.m.add_line((-1,1),(55,1),dxfattribs={'layer':'DIMENSIONS'})
        r = self.score()
        self.assertEqual(r['counts']['text_annotation'], 1)
        self.assertEqual(r['pairs'][0]['text'][0]['content'], 'ALL DIMENSIONS IN mm')

    def test_center_cutting_and_dimension_lines_cannot_hide_contacts(self):
        for layer in ['GEOM_CENTER', 'ANNO_SECTION', 'ANNO_DIM']:
            with self.subTest(layer=layer):
                self.doc.layers.new(layer)
                line = self.m.add_line((-1,1),(20,1),dxfattribs={'layer':layer})
                t = self.m.add_text('2X R8',dxfattribs={'height':3})
                self.assertEqual(self.score()['counts']['text_annotation'], 1)
                self.m.delete_entity(line)
                self.m.delete_entity(t)

    def test_own_dimension_text_not_exempt_and_group_lines_count_once(self):
        dim = self.m.add_linear_dim(base=(0,5),p1=(0,0),p2=(20,0))
        dim.render()
        entity = dim.dimension
        text = next(e for e in entity.virtual_entities() if e.dxftype() in {'TEXT','MTEXT'})
        x0,y0,x1,y1 = self.ink(text).bounds
        block = self.doc.blocks.get(entity.dxf.geometry)
        for y in ((y0+y1)/2, (y0+2*y1)/3):
            block.add_line((x0-1,y),(x1+1,y))
        r = self.score()
        self.assertEqual(r['counts']['text_annotation'], 1)
        self.assertEqual(len(r['pairs'][0]['contact_entities']), 2)

    def test_line_view_intersection_without_text_is_free(self):
        b = self.doc.blocks.new('VIEW_TEST')
        b.add_line((0,0),(10,10))
        self.m.add_blockref('VIEW_TEST',(0,0))
        self.m.add_line((0,10),(10,0))
        self.assertEqual(self.score()['score'], 1)

    def test_dash_gap_is_not_a_solid_line(self):
        self.doc.linetypes.new('LONG_GAP',dxfattribs={'pattern':[20,1,-19]})
        self.m.add_text('I',dxfattribs={'height':3,'insert':(8,0)})
        line = self.m.add_line((0,1),(20,1),dxfattribs={'linetype':'LONG_GAP'})
        self.assertEqual(self.score()['pair_count'], 0)
        line.dxf.linetype = 'CONTINUOUS'
        self.assertEqual(self.score()['pair_count'], 1)

    def test_missing_shx_without_available_mapping_is_unscored(self):
        self.doc.styles.new('missing-shx',dxfattribs={'font':'absent_benchcad_123.shx'})
        self.m.add_text('TEST',dxfattribs={'style':'missing-shx','height':3})
        self.assertIn('MISSING_EXPLICIT_FONT',self.score()['unsupported_types'])

    def test_missing_font_leaves_score_unavailable(self):
        self.doc.styles.new('missing',dxfattribs={'font':'absent_benchcad_font_123.ttf'})
        self.m.add_text('TEST',dxfattribs={'style':'missing','height':3})
        r = self.score()
        self.assertIsNone(r['score'])
        self.assertIn('MISSING_EXPLICIT_FONT',r['unsupported_types'])

    def test_unsupported_raster_not_silent_full_score(self):
        definition = self.doc.add_image_def(filename='unavailable.png',size_in_pixel=(10,10))
        self.m.add_image(definition,insert=(0,0),size_in_units=(10,10))
        self.assertIsNone(self.score()['score'])

    def test_review_layer_cannot_hide_submission_text(self):
        self.doc.layers.new('BC_COLLISION_CHECK')
        self.m.add_text('TEST',dxfattribs={'layer':'BC_COLLISION_CHECK','height':3})
        self.m.add_line((-1,1),(20,1))
        r = self.score()
        self.assertEqual(r['pair_count'], 1)
        self.assertIsNone(r['score'])
        self.assertIn('REVIEW_OVERLAY_IN_SUBMISSION',r['unsupported_types'])


if __name__ == '__main__':
    unittest.main()
