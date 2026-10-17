"""Offline bootstrap contract tests: no Docker/network/API is invoked."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'benchcad_bootstrap', Path(__file__).resolve().parents[1] / 'tools/benchcad.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.python = self.root / '.venv/bin/python'
        for target, value in (('ROOT', self.root), ('PYTHON', self.python)):
            mock = patch.object(bootstrap, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        for mock in (patch.object(bootstrap.sys, 'platform', 'linux'),
                     patch.object(bootstrap.sys, 'version_info', (3, 11)),
                     patch.object(bootstrap.os, 'getuid', return_value=1000, create=True)):
            mock.start()
            self.addCleanup(mock.stop)

    def ready(self):
        self.python.parent.mkdir(parents=True)
        self.python.touch()

    def test_setup_never_calls_api_or_inherits_model_credentials(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only-placeholder',
                                     'DOCKER_HOST': 'tcp://invalid:2375',
                                     'UNRELATED_SECRET': 'test-only-placeholder'}), \
                patch.object(bootstrap, 'invoke') as call:
            self.assertEqual(bootstrap.main(['setup']), 0)
        commands = [list(map(str, c.args[0])) for c in call.call_args_list]
        self.assertEqual(len(commands), 5)
        self.assertIn('venv', commands[1])
        self.assertIn('pip', commands[2])
        self.assertIn('tools/provision_tokenizer.py', commands[3])
        self.assertEqual(commands[4][-3:], ['-t', bootstrap.IMAGE, 'sandbox'])
        for command in (commands[0], commands[4]):
            self.assertIn('unix:///var/run/docker.sock', command)
            self.assertIn('--config', command)
        for c in call.call_args_list:
            env = c.args[1]
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('UNRELATED_SECRET', env)
            self.assertNotIn('DOCKER_HOST', env)
        self.assertFalse(any('benchcad_cadquery.cli' in cmd for cmd in commands))

    def test_check_runs_real_doctor_and_fixture_in_distinct_fresh_directories(self):
        self.ready()
        with patch.object(bootstrap, 'invoke') as call:
            bootstrap.main(['check'])
            bootstrap.main(['check'])
        checks = [c.args[0] for c in call.call_args_list if '-m' in c.args[0]]
        self.assertEqual([x[3] for x in checks], ['doctor', 'smoke', 'doctor', 'smoke'])
        paths = [x[x.index('--run-dir') + 1] for x in checks]
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(p.parent == self.root / '.runs' for p in paths))

    def test_run_preserves_literal_arguments_and_key_only_for_controller(self):
        self.ready()
        extra = ['--step', 'case with spaces.step', '--model', 'chosen-model',
                 '--run-dir', '.runs/one', '--base-url', 'https://example.invalid/v1']
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only-placeholder'}), \
                patch.object(bootstrap, 'invoke') as call:
            bootstrap.main(['run', *extra])
        self.assertNotIn('OPENAI_API_KEY', call.call_args_list[0].args[1])
        self.assertEqual(call.call_args.args[0],
                         [self.python, '-m', 'benchcad_cadquery.cli', 'run', *extra])
        self.assertEqual(call.call_args.args[1]['OPENAI_API_KEY'], 'test-only-placeholder')

    def test_failure_stops_without_building_or_running_paid_work(self):
        with patch.object(bootstrap, 'invoke', side_effect=subprocess.CalledProcessError(1, ['docker'])) as call:
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.main(['setup'])
        self.assertEqual(call.call_count, 1)

    def test_rejects_unsupported_host_and_root_before_subprocess(self):
        for platform, uid in [('win32', 1000), ('linux', 0)]:
            with self.subTest(platform=platform, uid=uid), \
                    patch.object(bootstrap.sys, 'platform', platform), \
                    patch.object(bootstrap.os, 'getuid', return_value=uid), \
                    patch.object(bootstrap, 'invoke') as call:
                with self.assertRaises(RuntimeError):
                    bootstrap.main(['setup'])
                call.assert_not_called()

    def test_setup_reuses_existing_venv(self):
        self.ready()
        with patch.object(bootstrap, 'invoke') as call:
            bootstrap.main(['setup'])
        self.assertFalse(any('venv' in c.args[0] for c in call.call_args_list))

    def test_missing_setup_and_invalid_arguments_fail_without_model_request(self):
        with patch.object(bootstrap, 'invoke') as call:
            with self.assertRaises(RuntimeError):
                bootstrap.main(['run', '--model', 'chosen-model'])
            self.assertEqual(call.call_count, 1)  # Docker availability check only.
        with patch.object(bootstrap, 'invoke') as call:
            for args in (['other'], ['setup', '--unexpected'], ['check', '--unexpected']):
                with self.assertRaises(ValueError):
                    bootstrap.main(args)
            call.assert_not_called()

    def test_invoke_uses_argument_vector_and_repository_directory(self):
        with patch.object(bootstrap.subprocess, 'run') as run:
            bootstrap.invoke(['python', 'file with spaces.py', '; not a shell command'], {'PATH': '/bin'})
        run.assert_called_once_with(['python', 'file with spaces.py', '; not a shell command'],
                                    cwd=self.root, env={'PATH': '/bin'}, check=True)


if __name__ == '__main__':
    unittest.main()
