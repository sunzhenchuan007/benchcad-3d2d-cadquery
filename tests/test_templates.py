import importlib.util
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchcad_cadquery.templates import load_blank_template


@unittest.skipUnless(importlib.util.find_spec("ezdxf"), "ezdxf is not installed")
class BlankTemplateTests(unittest.TestCase):
    def setUp(self):
        import ezdxf
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "blank.dxf"
        self.doc = ezdxf.new("R2013")
        self.doc.units = 4
        self.layout = self.doc.layouts.get("Layout1")
        self.layout.add_line((5, 5), (415, 5))

    def load(self, **kwargs):
        self.doc.saveas(self.path)
        before = self.path.read_bytes()
        result = load_blank_template(self.path, **kwargs)
        self.assertEqual(before, self.path.read_bytes())
        return result

    def test_imports_frame_title_and_resources_without_scaling(self):
        self.doc.styles.new("TITLE_FONT", dxfattribs={"font": "DejaVuSans.ttf"})
        self.doc.linetypes.new("FRAME_DASH", dxfattribs={"pattern": [0.5, 0.25, -0.25]})
        self.doc.layers.new("FRAME", dxfattribs={"linetype": "FRAME_DASH"})
        self.doc.dimstyles.new("TEMPLATE_DIM", dxfattribs={"dimtxt": 3.5})
        block = self.doc.blocks.new("TITLE")
        block.add_text("PART TITLE", dxfattribs={"style": "TITLE_FONT", "height": 3.5})
        block.add_attdef("NAME", (0, 5), text="<PART>")
        self.layout.add_blockref("TITLE", (300, 10)).add_auto_attribs({"NAME": "<PART>"})
        self.layout.add_line((10, 10), (410, 10), dxfattribs={"layer": "FRAME"})
        loaded = self.load()
        self.assertEqual(loaded.units, 4)
        self.assertEqual(len(loaded.modelspace()), 3)
        self.assertEqual(tuple(loaded.modelspace().query("LINE")[0].dxf.end), (415, 5, 0))
        insert = loaded.modelspace().query("INSERT")[0]
        self.assertEqual(tuple(insert.dxf.insert), (300, 10, 0))
        self.assertEqual(insert.get_attrib_text("NAME"), "<PART>")
        self.assertIn("TITLE_FONT", loaded.styles)
        self.assertIn("FRAME_DASH", loaded.linetypes)
        self.assertEqual(loaded.dimstyles.get("TEMPLATE_DIM").dxf.dimtxt, 3.5)
        self.assertFalse(loaded.audit().errors)

    def test_excludes_viewports_and_guides_including_nested_blocks(self):
        self.doc.layers.new("BC_GUIDE_NOPLOT")
        self.doc.layers.new("NO_PRINT", dxfattribs={"plot": 0})
        self.layout.add_viewport((100, 100), (50, 50), (0, 0), 50)
        for layer in ("BC_GUIDE_NOPLOT", "NO_PRINT"):
            self.layout.add_circle((20, 20), 2, dxfattribs={"layer": layer})
        nested = self.doc.blocks.new("NESTED")
        nested.add_circle((20, 20), 2, dxfattribs={"layer": "NO_PRINT"})
        nested.add_line((0, 0), (1, 1))
        self.layout.add_blockref("NESTED", (0, 0))
        loaded = self.load()
        self.assertEqual(len(loaded.modelspace()), 2)
        self.assertEqual(len(loaded.blocks["NESTED"]), 1)
        self.assertFalse(loaded.modelspace().query("VIEWPORT CIRCLE"))

    def test_rejects_nonempty_modelspace(self):
        self.doc.modelspace().add_line((0, 0), (1, 1))
        with self.assertRaisesRegex(ValueError, "model space is not empty"):
            self.load()

    def test_ambiguous_layout_requires_explicit_name(self):
        self.doc.layouts.new("A3_SCALE_READY").add_circle((10, 10), 2)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.load()
        loaded = self.load(layout_name="A3_SCALE_READY")
        self.assertEqual(len(loaded.modelspace().query("CIRCLE")), 1)
        self.assertFalse(loaded.modelspace().query("LINE"))

    def test_rejects_wrong_units_missing_layout_and_empty_sheet(self):
        self.doc.units = 1
        with self.assertRaisesRegex(ValueError, "millimetres"):
            self.load()
        self.doc.units = 4
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.load(layout_name="missing")
        self.layout.delete_all_entities()
        with self.assertRaisesRegex(ValueError, "no printable"):
            self.load()


@unittest.skipUnless(importlib.util.find_spec("ezdxf") and importlib.util.find_spec("cadquery"),
                     "ezdxf and CadQuery are required")
class DrawingTemplateTests(unittest.TestCase):
    def setUp(self):
        try:
            from benchcad_cadquery.drawing import Drawing
        except ImportError as exc:
            self.skipTest(f"CAD dependency import failed: {type(exc).__name__}")
        import ezdxf
        self.Drawing = Drawing
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.step = self.root / 'model.step'
        self.step.write_bytes(b'fixture STEP; geometry importer mocked in constructor test')
        self.template = self.root / 'template.dxf'
        doc = ezdxf.new('R2013')
        doc.units = 4
        doc.layers.new('FRAME', dxfattribs={'color': 3})
        doc.styles.new('BENCHCAD', dxfattribs={'font': 'DejaVuSans.ttf'})
        doc.dimstyles.new('BC_DIM', dxfattribs={'dimtxt': 4})
        doc.layouts.get('Layout1').add_line((5, 5), (415, 5), dxfattribs={'layer': 'FRAME'})
        doc.saveas(self.template)
        mocked = patch('benchcad_cadquery.drawing.cq.importers.importStep')
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_explicit_template_no_duplicate_frame_and_preserved_resources(self):
        drawing = self.Drawing(step=self.step, path=self.root / 'output.dxf', template=self.template)
        self.assertEqual(len(drawing.doc.modelspace()), 1)
        self.assertEqual(drawing.doc.layers.get('FRAME').dxf.color, 3)
        self.assertEqual(drawing.doc.styles.get('BENCHCAD').dxf.font, 'DejaVuSans.ttf')
        self.assertEqual(drawing.doc.dimstyles.get('BC_DIM').dxf.dimtxt, 4)
        self.assertEqual(drawing.state['template_sha256'], hashlib.sha256(self.template.read_bytes()).hexdigest())
        self.assertNotIn(str(self.template), str(drawing.state))

    def test_default_discovers_input_template_and_fallback_keeps_frame(self):
        original = Path.cwd()
        try:
            os.chdir(self.root)
            plain = self.Drawing(step=self.step)
            self.assertIsNone(plain.state['template_sha256'])
            self.assertEqual(len(plain.doc.modelspace()), 2)
            (self.root / 'input').mkdir()
            (self.root / 'input/template.dxf').write_bytes(self.template.read_bytes())
            drawing = self.Drawing(step=self.step)
            self.assertEqual(len(drawing.doc.modelspace()), 1)
            self.assertIsNotNone(drawing.state['template_sha256'])
        finally:
            os.chdir(original)

    def test_explicit_missing_template_does_not_silently_fallback(self):
        with self.assertRaises(OSError):
            self.Drawing(step=self.step, path=self.root / 'output.dxf', template=self.root / 'absent.dxf')


if __name__ == "__main__":
    unittest.main()
