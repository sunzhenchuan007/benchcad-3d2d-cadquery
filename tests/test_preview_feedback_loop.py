"""Exercise execution -> immutable observation -> next multimodal request.

Set BENCHCAD_TEST_DOCKER_IMAGE on Linux to also run the real Docker check.
No model API calls or credentials are used.
"""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from benchcad_runtime.docker_executor import DockerExecutor
from benchcad_runtime.episode import Budget, Execution, Reply, run_episode
from benchcad_runtime.openai_api import encode_messages
from benchcad_runtime.stages import source_snapshot


class PreviewFeedbackLoopTests(unittest.TestCase):
    def exercise_loop(self, actual_docker=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            (workspace / 'output').mkdir(parents=True)
            executor = DockerExecutor(workspace, root / 'records',
                                      source_snapshot(root / 'source'),
                                      os.environ.get('BENCHCAD_TEST_DOCKER_IMAGE', 'test-image'),
                                      uid=os.getuid() if actual_docker else 1000,
                                      gid=os.getgid() if actual_docker else 1000)
            calls = []
            snapshots = []

            def check_image(content, color):
                self.assertEqual(content['type'], 'input_image')
                data = base64.b64decode(content['image_url'].split(',', 1)[1])
                with Image.open(io.BytesIO(data)) as img:
                    self.assertEqual(img.getpixel((0, 0)), color)

            def model(system, messages, timeout):
                request = encode_messages(messages)
                calls.append(request)
                if len(calls) == 1:
                    self.assertEqual(len(request[-1]['content']), 1)
                    return Reply("```python\nfrom PIL import Image\n"
                                 "Image.new('RGB', (8, 8), 'red').save('output/preview.png')\n"
                                 "raise ValueError('repair-me')\n```")
                snapshots.append(Path(messages[-1]['images'][0]))
                self.assertIn('Attached 1', messages[-1]['text'])
                if len(calls) == 2:
                    self.assertIn('Exit code 1', messages[-1]['text'])
                    self.assertIn('repair-me', messages[-1]['text'])
                    check_image(request[-1]['content'][1], (255, 0, 0))
                    return Reply("```python\nfrom PIL import Image\n"
                                 "Image.new('RGB', (8, 8), 'blue').save('output/preview.png')\n```")
                self.assertEqual(len(calls), 3)
                check_image(request[-1]['content'][1], (0, 0, 255))
                check_image(request[2]['content'][1], (255, 0, 0))
                self.assertNotEqual(snapshots[0], snapshots[1])
                return Reply('```submit\npass\n```')

            def fake_run(command, timeout, directory):
                directory.mkdir()
                code = (workspace / Path(command[-1]).name).read_text()
                if 'Image.new' in code:
                    Image.new('RGB', (8, 8), 'red' if 'repair-me' in code else 'blue').save(
                        workspace / 'output/preview.png')
                return Execution(1, stderr='ValueError: repair-me') if 'repair-me' in code else Execution(0)

            def run():
                return run_episode(model, executor, lambda **kw: 'test-only', 'task',
                                   root / 'episode', Budget(rounds=3, wall_seconds=120))

            if actual_docker:
                result = run()
            else:
                with patch.object(executor, 'run_command', side_effect=fake_run):
                    result = run()
            self.assertTrue(result['submitted'])
            self.assertEqual([r['image_count'] for r in result['events']], [1, 1, 0])
            journal = [json.loads(line) for line in (root / 'episode/journal.jsonl').read_text().splitlines()]
            feedback = [r for r in journal if r['event'] == 'execution_finished']
            self.assertEqual([len(r['images']) for r in feedback], [1, 1, 0])

    def test_changed_preview_and_error_reach_next_request(self):
        self.exercise_loop()

    @unittest.skipUnless(sys.platform.startswith('linux') and os.environ.get('BENCHCAD_TEST_DOCKER_IMAGE'),
                         'Requires explicitly selected local Docker image on Linux')
    def test_real_docker_feedback_loop(self):
        self.exercise_loop(actual_docker=True)


if __name__ == '__main__':
    unittest.main()
