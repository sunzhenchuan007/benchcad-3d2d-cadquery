"""Public example integrity and packaged generic task loading."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from benchcad_runtime.stages import PACKAGE, prepare_workspace


class PublicExampleTests(unittest.TestCase):
    def test_manifest_matches_input(self):
        example = Path(__file__).resolve().parents[1] / 'examples' / 'case13'
        manifest = json.loads((example / 'manifest.json').read_text(encoding='utf-8'))
        data = (example / manifest['input']['path']).read_bytes()
        self.assertEqual(len(data), manifest['input']['bytes'])
        self.assertEqual(hashlib.sha256(data).hexdigest(), manifest['input']['sha256'])
        self.assertEqual(hashlib.sha256(data.split(b'DATA;', 1)[1]).hexdigest(),
                         manifest['publication']['data_section_sha256'])

    def test_stage_loads_only_its_generic_task(self):
        with tempfile.TemporaryDirectory() as directory:
            for kind in ('drawing', 'reconstruction'):
                workspace = prepare_workspace(Path(directory) / kind, kind)
                self.assertEqual((workspace / 'TASK.md').read_bytes(),
                                 (PACKAGE / 'tasks' / (kind + '.md')).read_bytes())
                self.assertEqual(list((workspace / 'input').iterdir()), [])
                self.assertFalse((workspace / 'model.step').exists())
