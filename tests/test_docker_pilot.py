"""No Docker daemon, network, API, or local CAD packages required."""
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from benchcad_cadquery.cli import configure, parser, run, stage_episode, summarize
from benchcad_runtime.artifacts import read_regular, sha256, validate_png, write_new
from benchcad_runtime.docker_executor import DockerExecutor, DockerLimits
from benchcad_runtime.episode import Budget, Execution, ReplayModel, Reply, run_episode
from benchcad_runtime.stages import StageFinalizer, prepare_workspace, source_snapshot, transfer_drawing


def png(color='red', size=(20, 10)):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return buffer.getvalue()


class PilotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.source = source_snapshot(self.root / 'source')
        for target in ('socket.create_connection', 'socket.socket'):
            guard = patch(target, side_effect=AssertionError('Network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def executor(self, **kwargs):
        return DockerExecutor(self.workspace, self.root / 'records', self.source,
                              'pilot:test', uid=1234, gid=1234, **kwargs)

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest('OS does not permit symlink creation for this account')


class DockerTests(PilotTest):
    def test_command_least_mounts_and_limits(self):
        executor = self.executor()
        cmd = executor.create_command('benchcad-owned', ['-B', '-P', '/workspace/code.py'])
        def option(key):
            return cmd[cmd.index(key)+1]
        self.assertEqual(option('--network'), 'none')
        self.assertIn('--read-only', cmd)
        self.assertEqual(option('--cap-drop'), 'ALL')
        self.assertEqual(option('--security-opt'), 'no-new-privileges')
        self.assertEqual(option('--memory'), option('--memory-swap'))
        self.assertEqual(option('--memory'), '4096m')
        self.assertEqual(option('--user'), '1234:1234')
        self.assertEqual(option('--cpus'), '1')
        self.assertEqual(option('--pids-limit'), '128')
        self.assertEqual(option('--pull'), 'never')
        self.assertEqual(option('--log-driver'), 'none')
        self.assertIn('noexec', option('--tmpfs'))
        mounts = [cmd[i+1] for i, v in enumerate(cmd) if v == '--mount']
        self.assertEqual(len(mounts), 2)
        self.assertTrue(mounts[0].endswith('dst=/workspace'))
        self.assertTrue(mounts[1].endswith('dst=/opt/benchcad,readonly'))
        self.assertNotIn(str(executor.records), ' '.join(mounts))
        self.assertNotIn('docker.sock', ' '.join(mounts))
        self.assertNotIn('OPENAI_API_KEY', ' '.join(cmd))
        self.assertEqual(set(executor.environment()), {'PATH', 'LANG'})
        self.assertEqual(option('--config'), str(executor.config))
        self.assertEqual(list(executor.config.iterdir()), [])

    def test_bad_resources_and_overlapping_directories(self):
        for kwargs in ({'cpus': float('nan')}, {'memory_mib': 0}, {'pids': -1}, {'log_bytes': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DockerLimits(**kwargs)
        with self.assertRaises(ValueError):
            DockerExecutor(self.workspace, self.workspace / 'records', self.source, 'pilot:test')

    def test_timeout_removes_owned_container_not_only_client(self):
        executor = self.executor()
        with patch('benchcad_runtime.docker_executor.sys.platform', 'linux'), \
                patch.object(executor, '_process', side_effect=[(0, None), (124, 'timeout')]) as process, \
                patch('benchcad_runtime.docker_executor.subprocess.run',
                      side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)]) as cleanup:
            result = executor.run_command(['-c', 'pass'], .1, self.root / 'call')
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, 124)
        create = process.call_args_list[0].args[0]
        owned = create[create.index('--name')+1]
        self.assertEqual(cleanup.call_args_list[0].args[0][-3:], ['rm', '--force', owned])
        self.assertEqual(cleanup.call_args_list[1].args[0][-3:], ['container', 'inspect', owned])
        self.assertEqual(process.call_args_list[0].args[2], process.call_args_list[1].args[2])

    def test_create_timeout_also_attempts_owned_cleanup(self):
        executor = self.executor()
        with patch('benchcad_runtime.docker_executor.sys.platform', 'linux'), \
                patch.object(executor, '_process', return_value=(124, 'timeout')), \
                patch.object(executor, '_remove_owned') as remove:
            result = executor.run_command(['-c', 'pass'], .1, self.root / 'call')
        remove.assert_called_once()
        self.assertTrue(result.timed_out)

    def test_log_overflow_also_removes_container_and_is_not_timeout(self):
        executor = self.executor()
        with patch('benchcad_runtime.docker_executor.sys.platform', 'linux'), \
                patch.object(executor, '_process', side_effect=[(0, None), (125, 'log_limit')]), \
                patch.object(executor, '_remove_owned') as remove:
            result = executor.run_command(['-c', 'pass'], 1, self.root / 'call')
        remove.assert_called_once()
        self.assertFalse(result.timed_out)
        self.assertEqual(result.exit_code, 125)
        self.assertIn('log_limit', result.stderr)

    def test_cleanup_failure_prevents_snapshot_reads(self):
        executor = self.executor()
        with patch('benchcad_runtime.docker_executor.sys.platform', 'linux'), \
                patch.object(executor, '_process', return_value=(0, None)), \
                patch.object(executor, '_remove_owned', side_effect=RuntimeError('cleanup failed')), \
                patch.object(executor, 'observe') as observe:
            with self.assertRaises(RuntimeError):
                executor.execute('pass', 1)
        observe.assert_not_called()

    def test_cleanup_errors_are_bounded_and_fail_closed(self):
        executor = self.executor()
        with patch('benchcad_runtime.docker_executor.subprocess.run',
                   return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(RuntimeError):
                executor._remove_owned('benchcad-owned')
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all(c.kwargs['timeout'] == 5 for c in run.call_args_list))

    def test_disk_capture_is_strictly_bounded_combined(self):
        executor = self.executor(limits=DockerLimits(log_bytes=1000))
        class Process:
            stdout = io.BytesIO(b'x' * 100000)
            stderr = io.BytesIO(b'y' * 100000)
            returncode = 0
            def poll(self): return 0
            def wait(self, timeout): return 0
            def kill(self): pass
        directory = self.root / 'capture'
        directory.mkdir()
        with patch('benchcad_runtime.docker_executor.subprocess.Popen', return_value=Process()):
            code, stop = executor._process(['mock'], directory, __import__('time').monotonic()+5)
        self.assertEqual(stop, 'log_limit')
        self.assertEqual(code, 125)
        self.assertEqual(sum(p.stat().st_size for p in directory.iterdir()), 1000)

    def test_supervisor_deadline_kills_client_without_waiting_for_client_timeout(self):
        executor = self.executor()
        class Process:
            stdout = io.BytesIO()
            stderr = io.BytesIO()
            returncode = None
            killed = False
            def poll(self): return self.returncode
            def wait(self, timeout): return self.returncode
            def kill(self):
                self.killed = True
                self.returncode = -9
        proc = Process()
        directory = self.root / 'capture'
        directory.mkdir()
        with patch('benchcad_runtime.docker_executor.subprocess.Popen', return_value=proc), \
                patch('benchcad_runtime.docker_executor.time.monotonic', side_effect=[0, 2]):
            code, stop = executor._process(['mock'], directory, deadline=1)
        self.assertTrue(proc.killed)
        self.assertEqual((code, stop), (124, 'timeout'))

    def test_unique_names_and_external_code_snapshot(self):
        executor = self.executor()
        with patch('benchcad_runtime.docker_executor.sys.platform', 'linux'), \
                patch.object(executor, '_process', return_value=(0, None)) as process, \
                patch.object(executor, '_remove_owned'):
            executor.execute('print(1)', 1)
            executor.execute('print(2)', 1)
        creates = [call.args[0] for call in process.call_args_list if 'create' in call.args[0]]
        names = [c[c.index('--name')+1] for c in creates]
        self.assertEqual(len(set(names)), 2)
        self.assertEqual(sorted(p.read_text() for p in executor.records.glob('*.py')), ['print(1)', 'print(2)'])
        self.assertEqual(len(list(self.workspace.glob('_execution_*.py'))), 2)


class ArtifactTests(PilotTest):
    def test_symlink_mode_is_rejected_without_open_even_on_windows(self):
        target = self.workspace / 'submission'
        target.write_bytes(b'data')
        original = Path.lstat
        def linked(path, *args, **kwargs):
            if path == target:
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', linked), patch('benchcad_runtime.artifacts.os.open') as opened:
            with self.assertRaises(ValueError):
                read_regular(self.workspace, 'submission', 100)
        opened.assert_not_called()

    def test_special_file_and_reparse_point_rejected_without_open(self):
        target = self.workspace / 'submission'
        target.write_bytes(b'data')
        original = Path.lstat
        for mode, attributes in ((stat.S_IFIFO, 0), (stat.S_IFREG, 0x400)):
            def special(path, *args, **kwargs):
                if path == target:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_nlink=1, st_size=4)
                return original(path, *args, **kwargs)
            with self.subTest(mode=mode), patch.object(Path, 'lstat', special), \
                    patch('benchcad_runtime.artifacts.os.open') as opened:
                with self.assertRaises(ValueError):
                    read_regular(self.workspace, 'submission', 100)
            opened.assert_not_called()

    def test_image_snapshot_survives_replacement_and_is_content_addressed(self):
        executor = self.executor()
        image = self.workspace / 'preview.png'
        image.write_bytes(png())
        images, metadata = executor.observe()
        first = Path(images[0])
        original = first.read_bytes()
        self.assertFalse(first.is_relative_to(self.workspace))
        self.assertEqual(first.stem, sha256(original))
        self.assertEqual(metadata[0]['sha256'], first.stem)
        self.assertEqual(executor.observe(), ([], []))
        image.write_bytes(png('blue'))
        later, _ = executor.observe()
        self.assertNotEqual(images, later)
        self.assertEqual(first.read_bytes(), original)
        image.unlink()
        self.assertTrue(first.exists())

    def test_image_dimensions_and_format_are_validated(self):
        with self.assertRaises(ValueError):
            validate_png(png(size=(4097, 1)))
        with self.assertRaises(ValueError):
            validate_png(b'not PNG')
        executor = self.executor()
        (self.workspace / 'bad.png').write_bytes(b'bad data')
        images, records = executor.observe()
        self.assertEqual(images, [])
        self.assertIn('rejected', records[0])

    def test_image_symlink_is_rejected(self):
        outside = self.root / 'outside.png'
        outside.write_bytes(png())
        self.symlink(self.workspace / 'preview.png', outside)
        images, records = self.executor().observe()
        self.assertEqual(images, [])
        self.assertIn('rejected', records[0])

    def test_size_traversal_and_hardlinks_are_rejected(self):
        (self.workspace / 'file').write_bytes(b'12345')
        with self.assertRaises(ValueError):
            read_regular(self.workspace, 'file', 4)
        with self.assertRaises(ValueError):
            read_regular(self.workspace, '../outside', 100)
        os.link(self.workspace / 'file', self.workspace / 'link')
        with self.assertRaises(ValueError):
            read_regular(self.workspace, 'link', 100)

    def test_symlink_submission_and_parent_directory_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'drawing.dxf').write_bytes(b'not important')
        self.symlink(self.workspace / 'output', outside, directory=True)
        finalizer = StageFinalizer('drawing', self.workspace, self.root / 'drawing', self.source,
                                   'pilot:test', DockerLimits())
        with patch('benchcad_runtime.stages.DockerExecutor') as executor:
            with self.assertRaises(ValueError):
                finalizer(timeout=10)
        executor.assert_not_called()


class StageTests(PilotTest):
    def fake_bundle(self):
        bundle = self.root / 'bundle'
        bundle.mkdir()
        files = {'drawing.dxf': b'mock-dxf', 'drawing.png': png()}
        for name, data in files.items():
            (bundle / name).write_bytes(data)
        manifest = {'kind': 'drawing', 'files': {
            name: {'sha256': sha256(data), 'bytes': len(data)} for name, data in files.items()}}
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
        return bundle

    def test_transfer_only_frozen_dxf_png_and_generic_guides(self):
        bundle = self.fake_bundle()
        for name in ('model.step', 'drawing.state.json', 'code.py', 'log.txt', 'gt.json'):
            (bundle / name).write_text('must never transfer')
        workspace = prepare_workspace(self.root / 'reconstruction', 'reconstruction')
        images = transfer_drawing(bundle, workspace)
        self.assertEqual(sorted(p.name for p in (workspace / 'input').iterdir()), ['drawing.dxf', 'drawing.png'])
        self.assertEqual(set(p.name for p in workspace.iterdir()), {'input', 'output', 'TASK.md', 'TOOLS.md'})
        self.assertEqual(images, [str((bundle / 'drawing.png').resolve())])
        self.assertNotIn(str(self.root), (workspace / 'TOOLS.md').read_text())
        with self.assertRaises(ValueError):
            transfer_drawing(bundle, workspace)

    def test_transfer_rejects_hash_mismatch_before_any_copy(self):
        bundle = self.fake_bundle()
        (bundle / 'drawing.png').write_bytes(png('blue'))
        workspace = prepare_workspace(self.root / 'reconstruction', 'reconstruction')
        with self.assertRaises(ValueError):
            transfer_drawing(bundle, workspace)
        self.assertEqual(list((workspace / 'input').iterdir()), [])

    def test_finalizer_uses_clean_workspace_and_excludes_state(self):
        stage = self.root / 'drawing'
        workspace = prepare_workspace(stage, 'drawing')
        (workspace / 'output/drawing.dxf').write_bytes(b'mock-dxf')
        (workspace / 'output/drawing.state.json').write_text('untrusted')
        (workspace / 'output/drawing.operations.jsonl').write_text('tool log bytes')
        def validate(command, timeout, directory):
            clean = next((stage / 'finalize').glob('*/workspace'))
            self.assertEqual(set(p.name for p in clean.iterdir()), {'drawing.dxf'})
            self.assertEqual(command, ['-I', '/opt/benchcad/benchcad_cadquery/worker.py', 'validate-drawing'])
            self.assertLessEqual(timeout, 10)
            (clean / 'drawing.png').write_bytes(png())
            (clean / 'validation.json').write_text('{"kind":"drawing"}')
            return Execution(0)
        with patch('benchcad_runtime.stages.DockerExecutor') as executor:
            executor.return_value.run_command.side_effect = validate
            finalizer = StageFinalizer('drawing', workspace, stage, self.source, 'pilot:test', DockerLimits())
            bundle = Path(finalizer(timeout=10))
            before = {p.name: p.read_bytes() for p in bundle.iterdir()}
            self.assertEqual(set(before), {'drawing.dxf', 'drawing.png', 'manifest.json'})
            self.assertEqual((stage/'evidence/drawing.operations.jsonl').read_text(),'tool log bytes')
            self.assertFalse(json.loads((stage/'evidence/capture.json').read_text())['content_verified'])
            (workspace/'output/drawing.operations.jsonl').write_text('changed later')
            self.assertEqual((stage/'evidence/drawing.operations.jsonl').read_text(),'tool log bytes')
            with self.assertRaises(FileExistsError):
                finalizer(timeout=10)
            self.assertEqual(before, {p.name: p.read_bytes() for p in bundle.iterdir()})

    def test_reconstruction_freezes_step_only(self):
        stage = self.root / 'reconstruction'
        workspace = prepare_workspace(stage, 'reconstruction')
        (workspace / 'output/model.step').write_bytes(b'mock-step')
        def validate(*args):
            clean = next((stage / 'finalize').glob('*/workspace'))
            self.assertEqual(set(p.name for p in clean.iterdir()), {'model.step'})
            (clean / 'validation.json').write_text('{"kind":"reconstruction"}')
            return Execution(0)
        with patch('benchcad_runtime.stages.DockerExecutor') as executor:
            executor.return_value.run_command.side_effect = validate
            bundle = Path(StageFinalizer('reconstruction', workspace, stage, self.source,
                                         'pilot:test', DockerLimits())(timeout=10))
        self.assertEqual(set(p.name for p in bundle.iterdir()), {'model.step', 'manifest.json'})

    def test_failed_validation_never_publishes_bundle(self):
        stage = self.root / 'drawing'
        workspace = prepare_workspace(stage, 'drawing')
        (workspace / 'output/drawing.dxf').write_bytes(b'malformed')
        with patch('benchcad_runtime.stages.DockerExecutor') as executor:
            executor.return_value.run_command.return_value = Execution(1)
            with self.assertRaises(RuntimeError):
                StageFinalizer('drawing', workspace, stage, self.source, 'pilot:test', DockerLimits())(timeout=10)
        self.assertFalse((stage / 'bundle').exists())

    def test_validation_retry_uses_separate_clean_workspace(self):
        from benchcad_runtime.errors import SubmissionRejected
        stage = self.root / 'drawing'
        workspace = prepare_workspace(stage, 'drawing')
        (workspace / 'output/drawing.dxf').write_bytes(b'malformed')
        attempts = []
        def validate(command, timeout, directory):
            clean = directory.parents[1] / 'workspace'
            attempts.append(clean)
            if len(attempts) == 1:
                (clean / 'rejection.json').write_text('{"kind":"drawing","reason":"Cannot parse DXF"}')
                return Execution(2)
            (clean / 'drawing.png').write_bytes(png())
            (clean / 'validation.json').write_text('{"kind":"drawing"}')
            return Execution(0)
        with patch('benchcad_runtime.stages.DockerExecutor') as executor:
            executor.return_value.run_command.side_effect = validate
            finalize = StageFinalizer('drawing', workspace, stage, self.source, 'pilot:test', DockerLimits())
            with self.assertRaises(SubmissionRejected):
                finalize(timeout=10)
            self.assertFalse((stage/'bundle').exists())
            (workspace/'output/drawing.dxf').write_bytes(b'corrected')
            bundle = Path(finalize(timeout=10))
            self.assertNotEqual(*attempts)
            self.assertEqual((attempts[0]/'drawing.dxf').read_bytes(), b'malformed')
            self.assertEqual((bundle/'drawing.dxf').read_bytes(), b'corrected')

    def test_source_snapshot_is_explicit_allowlist(self):
        self.assertEqual(set(p.relative_to(self.source).as_posix() for p in self.source.rglob('*') if p.is_file()),
                         {'benchcad_cadquery/__init__.py', 'benchcad_cadquery/drawing.py',
                          'benchcad_cadquery/templates.py', 'benchcad_cadquery/worker.py',
                          'benchcad_cadquery/operations.py', 'benchcad_cadquery/inspection.py'})

    def test_reconstruction_guide_omits_irrelevant_drawing_workflow(self):
        workspace = prepare_workspace(self.root/'reconstruction', 'reconstruction')
        guide = (workspace/'TOOLS.md').read_text(encoding='utf-8')
        self.assertNotIn('render_step', guide)
        self.assertIn('crop_image', guide)
        self.assertNotIn('drawing.operations.jsonl', guide)
        self.assertNotIn('Drawing(step=', guide)
        drawing = prepare_workspace(self.root/'drawing', 'drawing')
        self.assertIn('drawing.operations.jsonl', (drawing/'TOOLS.md').read_text(encoding='utf-8'))

    def test_initial_images_backward_compatible_and_context_fresh(self):
        seen = []
        def model(system, messages, timeout):
            seen.append(list(messages))
            return Reply('```submit\npass\n```')
        executor = SimpleNamespace(execute=lambda *args: Execution(0))
        for name, images in (('drawing', None), ('reconstruction', ['frozen.png'])):
            run_episode(model, executor, lambda **kw: 'bundle', name, self.root / name,
                        initial_images=images)
        self.assertEqual(len(seen[1]), 1)
        self.assertEqual(seen[0][0]['images'], [])
        self.assertEqual(seen[1][0]['images'], ['frozen.png'])
        self.assertEqual(seen[1][0]['text'], 'reconstruction')

    def test_cli_stage_uses_separate_reconstruction_prompt_and_initial_image(self):
        bundle = self.fake_bundle()
        args = configure(parser().parse_args(['smoke', '--run-dir', str(self.root / 'run')]))
        with patch('benchcad_cadquery.cli.DockerExecutor'), patch('benchcad_cadquery.cli.run_episode') as episode:
            stage_episode('reconstruction', self.root / 'run', self.source, args, ReplayModel([]),
                          drawing_bundle=bundle)
        call = episode.call_args
        self.assertIn('reconstruction agent', call.kwargs['system_prompt'])
        self.assertEqual(call.kwargs['initial_images'], [str((bundle / 'drawing.png').resolve())])
        self.assertIn('only the supplied DXF/PNG', call.kwargs['system_prompt'])


    def test_native_stage_replaces_legacy_format_in_task_and_prompt(self):
        from benchcad_runtime.native_tools import PROTOCOL
        bundle = self.fake_bundle()
        step = self.root/'model.step'
        step.write_bytes(b'test input')
        for kind in ('drawing','reconstruction'):
            with self.subTest(kind=kind):
                root = self.root/('native-'+kind)
                args = configure(parser().parse_args(['smoke','--run-dir',str(root)]))
                model = SimpleNamespace(action_protocol=PROTOCOL)
                with patch('benchcad_cadquery.cli.DockerExecutor'), patch('benchcad_cadquery.cli.run_episode') as episode:
                    stage_episode(kind,root,self.source,args,model,step=step,drawing_bundle=bundle)
                task = episode.call_args.args[3]
                self.assertNotIn('```submit',task)
                self.assertNotIn('Each response is exactly one nonempty',task)
                self.assertIn('execute_python',task)
                self.assertIn('Default limits:',task)
                self.assertNotIn('```submit',episode.call_args.kwargs['system_prompt'])
                self.assertNotIn('```submit',(root/kind/'workspace/TASK.md').read_text())


class CLITests(PilotTest):
    def test_provider_request_cap_can_be_selected(self):
        (self.root/'bundle').mkdir()
        args = configure(parser().parse_args(['reconstruct', '--run-dir', str(self.root/'run'),
            '--drawing-bundle', str(self.root/'bundle'), '--model', 'deepseek-flash',
            '--max-request-bytes', str(48*1024*1024)]))
        self.assertEqual(args.max_request_bytes, 48*1024*1024)

    def test_sequential_api_orchestration_with_fake_transport_and_workers(self):
        step = self.root / 'input.step'
        step.write_bytes(b'original STEP must never reach reconstruction')
        template = self.root / 'template.dxf'
        template.write_bytes(b'curated blank template supplied by controller')
        run_dir = self.root / 'run'
        args = configure(parser().parse_args(['run', '--step', str(step), '--run-dir', str(run_dir),
                                              '--model', 'parent-model', '--reconstruct',
                                              '--template', str(template)]))
        seen = []
        def model(system, messages, timeout):
            seen.append((system, messages[0].copy()))
            return Reply('```submit\npass\n```')
        def worker(executor, command, timeout, directory):
            work = executor.workspace
            if command[-1] == 'validate-drawing':
                self.assertEqual(set(p.name for p in work.iterdir()), {'drawing.dxf'})
                (work / 'drawing.png').write_bytes(png())
                (work / 'validation.json').write_text('{"kind":"drawing"}')
            elif command[-1] == 'validate-reconstruction':
                self.assertEqual(set(p.name for p in work.iterdir()), {'model.step'})
                (work / 'validation.json').write_text('{"kind":"reconstruction"}')
            elif work.parent.name == 'drawing':
                self.assertEqual((work / 'input/model.step').read_bytes(), step.read_bytes())
                self.assertEqual((work / 'input/template.dxf').read_bytes(), template.read_bytes())
                (work / 'output/drawing.dxf').write_bytes(b'generic mock DXF')
            elif work.parent.name == 'reconstruction':
                self.assertEqual(set(p.name for p in (work / 'input').iterdir()), {'drawing.dxf', 'drawing.png'})
                self.assertFalse((work / 'input/model.step').exists())
                (work / 'output/model.step').write_bytes(b'reconstructed STEP')
            else:
                self.fail('Unexpected worker path')
            directory.mkdir(parents=True)
            return Execution(0)
        with patch('benchcad_cadquery.cli._load_encoding'), \
                patch('benchcad_cadquery.cli.OpenAIResponses', return_value=model) as transport, \
                patch.object(DockerExecutor, 'run_command', worker), \
                patch('os.getuid', return_value=1000, create=True), \
                patch('os.getgid', return_value=1000, create=True), patch('builtins.print'):
            self.assertEqual(run(args), 0)
            with self.assertRaises(FileExistsError):
                run(args)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][1]['images'], [])
        initial = Path(seen[1][1]['images'][0])
        self.assertEqual(initial, run_dir / 'drawing/bundle/drawing.png')
        report = json.loads((run_dir / 'summary.json').read_text())
        self.assertTrue(report['success'])
        self.assertEqual(set(report['stages']), {'drawing', 'reconstruction'})
        self.assertEqual(report['model'], 'parent-model')
        self.assertIsNone(report['actual_fee'])

    def test_budget_caps_no_default_model_and_config_compatibility(self):
        for option, value in (('--rounds', '61'), ('--wall-seconds', '7201'), ('--execution-seconds', '301')):
            with self.subTest(option=option), self.assertRaises(ValueError):
                configure(parser().parse_args(['smoke', '--run-dir', 'unused', option, value]))
        step = self.root / 'part.step'
        step.write_text('mock-step')
        with self.assertRaises(ValueError):
            configure(parser().parse_args(['run', '--step', str(step), '--run-dir', 'unused']))
        config = self.root / 'config.toml'
        config.write_text('[api]\nmodel="parent-model"\n[budget]\nrounds=12\n[executor]\nbackend="docker"\ncpus=1\n')
        args = configure(parser().parse_args(['run', '--step', str(step), '--run-dir', 'unused',
                                              '--config', str(config), '--rounds', '7']))
        self.assertEqual(args.model, 'parent-model')
        self.assertEqual(args.rounds, 7)
        self.assertEqual(args.max_consecutive_output_limits, 2)
        configured = configure(parser().parse_args(['smoke', '--run-dir', 'unused',
                                                   '--max-consecutive-output-limits', '3']))
        self.assertEqual(configured.max_consecutive_output_limits, 3)
        self.assertEqual((configured.rounds, configured.wall_seconds, configured.max_output_tokens),
                         (60, 7200, 500000))
        for cap in (0, -1, 500001, True):
            args = parser().parse_args(['smoke', '--run-dir', 'unused'])
            args.max_output_tokens = cap
            with self.subTest(output_cap=cap), self.assertRaises(ValueError):
                configure(args)
        with self.assertRaises(ValueError):
            configure(parser().parse_args(['smoke', '--run-dir', 'unused',
                                          '--max-consecutive-output-limits', '0']))

    def test_summary_does_not_invent_tokens_or_scores(self):
        value = summarize({}, 1, True, None)
        self.assertEqual(value['usage_metric'], 'local_visible_text')
        self.assertIsNone(value['actual_fee'])
        self.assertIsNone(value['local_visible_text']['known_usage_subtotal'])
        self.assertIsNone(value['metrics']['reconstruction_iou']['score'])
        self.assertFalse(value['expert_baseline_enabled'])


if __name__ == '__main__':
    unittest.main()
