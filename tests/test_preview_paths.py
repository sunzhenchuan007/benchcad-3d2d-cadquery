"""Preview discovery boundary tests; no Docker or model API required."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from benchcad_runtime.docker_executor import DockerExecutor
from benchcad_runtime.episode import Execution


class PreviewPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace, self.source = root / 'workspace', root / 'source'
        self.workspace.mkdir()
        self.source.mkdir()
        (self.workspace / 'output').mkdir()
        self.executor = DockerExecutor(self.workspace, root / 'records', self.source,
                                       'test-image', uid=1000, gid=1000)

    def png(self, name, color='red', size=(8, 8)):
        data = io.BytesIO()
        Image.new('RGB', size, color).save(data, format='PNG')
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.getvalue())
        return data.getvalue()

    def test_output_preview_is_returned_as_execution_feedback(self):
        expected = self.png('output/preview.png')
        def fake_run(command, timeout, directory):
            directory.mkdir()
            return Execution(0, 'ok')
        with patch.object(self.executor, 'run_command', side_effect=fake_run):
            result = self.executor.execute('print("ok")', 30)
        self.assertEqual(len(result.images), 1)
        self.assertEqual(Path(result.images[0]).read_bytes(), expected)
        observed = list(self.executor.records.glob('*/observations.json'))
        self.assertEqual(json.loads(observed[0].read_text())[0]['name'], 'output/preview.png')

    def test_same_basename_in_two_locations_has_independent_deduplication(self):
        self.png('preview.png', 'red')
        self.png('output/preview.png', 'blue')
        images, records = self.executor.observe()
        self.assertEqual(len(images), 2)
        self.assertEqual({r['name'] for r in records}, {'preview.png', 'output/preview.png'})
        self.assertEqual(self.executor.observe(), ([], []))
        self.png('output/preview.png', 'green')
        _, changed = self.executor.observe()
        self.assertEqual([r['name'] for r in changed], ['output/preview.png'])

    def test_output_symlink_is_rejected_before_scanning(self):
        output = self.workspace / 'output'
        original_lstat = Path.lstat
        original_scandir = os.scandir
        scanned = []
        def lstat(path, *args, **kwargs):
            if path == output:
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
            return original_lstat(path, *args, **kwargs)
        def scandir(path):
            scanned.append(Path(path))
            return original_scandir(path)
        with patch.object(Path, 'lstat', lstat), patch('os.scandir', side_effect=scandir):
            images, records = self.executor.observe()
        self.assertFalse(images)
        self.assertEqual(records, [{'name': 'output', 'rejected': 'ValueError'}])
        self.assertNotIn(output, scanned)

    def test_output_reparse_point_and_parent_escape_are_rejected(self):
        output = self.workspace / 'output'
        original_lstat, original_resolve = Path.lstat, Path.resolve
        def reparse(path, *args, **kwargs):
            if path == output:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', reparse):
            self.assertEqual(self.executor.observe()[1], [{'name': 'output', 'rejected': 'ValueError'}])
        def escape(path, *args, **kwargs):
            return self.source if path == output else original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', escape):
            self.assertEqual(self.executor.observe()[1], [{'name': 'output', 'rejected': 'ValueError'}])

    def test_subdirectories_are_not_recursed(self):
        self.png('output/nested/hidden.png')
        self.png('other/hidden.png')
        self.png('output/visible.png')
        _, records = self.executor.observe()
        self.assertEqual([r['name'] for r in records], ['output/visible.png'])

    def test_global_image_limit_applies_across_both_locations(self):
        for i in range(17):
            self.png(f'top-{i}.png', (i, 0, 0))
            self.png(f'output/out-{i}.png', (0, i, 0))
        images, records = self.executor.observe()
        self.assertEqual(len(images), 32)
        self.assertEqual(sum('snapshot' in r for r in records), 32)
        self.assertEqual(records[-1], {'rejected': 'PNG observation count limit'})
        self.assertTrue(any(r.get('name', '').startswith('output/') for r in records))

    def test_global_entry_limit_applies_across_both_locations(self):
        self.png('output/too-late.png')
        top_entries = [SimpleNamespace(name=f'file-{i}') for i in range(2000)]
        out_entries = [SimpleNamespace(name=f'file-{i}') for i in range(2096)]
        out_entries.append(SimpleNamespace(name='too-late.png'))
        def scandir(path):
            return contextlib.nullcontext(iter(top_entries if Path(path) == self.workspace else out_entries))
        with patch('os.scandir', side_effect=scandir):
            images, records = self.executor.observe()
        self.assertFalse(images)
        self.assertEqual(records, [{'rejected': 'entry scan limit'}])

    def test_old_snapshot_is_immutable_after_working_file_overwrite(self):
        old = self.png('output/preview.png', 'red')
        previous, _ = self.executor.observe()
        new = self.png('output/preview.png', 'blue')
        current, _ = self.executor.observe()
        self.assertNotEqual(previous, current)
        self.assertEqual(Path(previous[0]).read_bytes(), old)
        self.assertEqual(Path(current[0]).read_bytes(), new)

    def test_deadline_and_png_dimensions_still_apply(self):
        self.png('output/large.png', size=(4097, 1))
        images, records = self.executor.observe()
        self.assertFalse(images)
        self.assertEqual(records, [{'name': 'output/large.png', 'rejected': 'ValueError'}])
        self.assertEqual(self.executor.observe(time.monotonic() - 1),
                         ([], [{'rejected': 'observation deadline'}]))


if __name__ == '__main__':
    unittest.main()
