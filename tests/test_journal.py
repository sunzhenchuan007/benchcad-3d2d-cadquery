import json
from pathlib import Path
import tempfile
import unittest

from benchcad_runtime.episode import Execution, Reply, TransportError, run_episode


class JournalTests(unittest.TestCase):
    def test_timing_errors_and_messages_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            now, calls = [0.0], [0]

            def model(*args, **kwargs):
                calls[0] += 1
                now[0] += 2
                if calls[0] == 1:
                    raise TransportError('test network failure')
                return Reply('```submit\npass\n```')

            class Executor:
                def execute(self, code, timeout):
                    now[0] += 3
                    return Execution(0, stdout='test output')

            def finalize(**kwargs):
                now[0] += 1
                return 'frozen'

            root = Path(tmp) / 'episode'
            result = run_episode(model, Executor(), finalize, 'task', root, clock=lambda: now[0])
            rows = [json.loads(line) for line in (root / 'journal.jsonl').read_text().splitlines()]
            self.assertEqual(result['timing'], dict(api_seconds=4, execution_seconds=3, finalization_seconds=1))
            self.assertEqual([r['sequence'] for r in rows], list(range(1, len(rows) + 1)))
            self.assertEqual(len([r for r in rows if r['event'] == 'request_failed']), 1)
            self.assertEqual(rows[-1]['event'], 'episode_finished')
            self.assertTrue(any(r.get('stdout') == 'test output' for r in rows))
            self.assertTrue(any(r.get('message', {}).get('text') == 'task' for r in rows))

    def test_interruption_keeps_started_request_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'episode'

            def interrupted(*args, **kwargs):
                rows = (root / 'journal.jsonl').read_text().splitlines()
                self.assertEqual(json.loads(rows[-1])['event'], 'request_started')
                raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                run_episode(interrupted, None, None, 'task', root)
            self.assertTrue((root / 'journal.jsonl').is_file())


if __name__ == '__main__':
    unittest.main()
