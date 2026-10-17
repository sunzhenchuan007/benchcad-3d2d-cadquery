"""Offline regression checks for long reasoning requests and stage deadlines."""
import io
import json
import unittest
from unittest.mock import patch, MagicMock

from benchcad_runtime import api_worker


class RequestDeadlineTests(unittest.TestCase):
    def test_socket_uses_remaining_stage_budget(self):
        payload = {'url': 'https://example.invalid/v1/responses',
                   'request': {}, 'timeout_seconds': 480.5}
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"output": []}'
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-placeholder'}), \
                patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                patch('sys.stdout', io.StringIO()), \
                patch('urllib.request.build_opener', return_value=opener):
            api_worker.main()
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 480.5)

    def test_invalid_deadline_does_not_send_request(self):
        for deadline in [0, -1, float('inf'), float('nan')]:
            with self.subTest(deadline=deadline), \
                    patch('sys.stdin', io.StringIO(json.dumps({'timeout_seconds': deadline}))), \
                    patch('urllib.request.build_opener') as opener:
                with self.assertRaises(ValueError):
                    api_worker.main()
                opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
