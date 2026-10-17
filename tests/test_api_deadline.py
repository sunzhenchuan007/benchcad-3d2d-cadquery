"""Offline regression checks for long reasoning requests and stage deadlines."""
import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

from benchcad_runtime import api_worker


class RequestDeadlineTests(unittest.TestCase):
    def test_transport_diagnostics_do_not_copy_sensitive_error_details(self):
        examples = [
            (TimeoutError('private-detail'), 'socket_timeout'),
            (urllib.error.URLError(socket.gaierror('private-detail')), 'dns_error'),
            (urllib.error.HTTPError('https://example.invalid', 502, 'private-detail', {}, None), 'http_error'),
        ]
        payload = {'url': 'https://example.invalid/v1/responses',
                   'request': {}, 'timeout_seconds': 30}
        for error, kind in examples:
            output = io.StringIO()
            opener = MagicMock()
            opener.open.side_effect = error
            with self.subTest(kind=kind), \
                    patch.dict('os.environ', {'OPENAI_API_KEY': 'test-placeholder'}), \
                    patch('sys.stdin', io.StringIO(json.dumps(payload))), \
                    patch('sys.stdout', output), \
                    patch('urllib.request.build_opener', return_value=opener):
                api_worker.main()
            result = json.loads(output.getvalue())
            self.assertEqual(result['diagnostic']['failure_kind'], kind)
            self.assertIn('transport_error', result)
            self.assertNotIn('private-detail', output.getvalue())
            self.assertNotIn('test-placeholder', output.getvalue())

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
