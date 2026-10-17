"""Offline request-size/deadline/diagnostic regressions; never call the API."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from benchcad_runtime import openai_api as api
from benchcad_runtime.episode import Budget, TransportError, run_episode


class RequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'OPENAI_API_KEY': 'test-only-placeholder'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = [{'role': 'user', 'text': 'Draw the supplied part', 'images': []}]
        self.accounting = patch.object(api, 'estimate_request', return_value=None)
        self.accounting.start()
        self.addCleanup(self.accounting.stop)
        self.response_accounting = patch.object(api, 'account_response', return_value=None)
        self.response_accounting.start()
        self.addCleanup(self.response_accounting.stop)

    def result(self, extra=None):
        body = {'status': 'completed', 'model': 'test-model', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': '```submit\npass\n```'}]}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(extra or body))

    def test_64mib_default_rejects_aggregate_images_before_reading(self):
        model = api.OpenAIResponses('test-model')
        self.messages[0]['images'] = ['first.png'] * 5
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=12*1024*1024)), \
                patch.object(Path, 'read_bytes') as read, patch.object(api.subprocess, 'run') as run:
            with self.assertRaises(api.RequestBudgetError) as error:
                model('Explicit instructions', self.messages, 60)
        self.assertEqual(error.exception.diagnostic['attribution'], 'framework_request_budget')
        self.assertFalse(error.exception.diagnostic['request_sent'])
        read.assert_not_called()
        run.assert_not_called()

    def test_preflight_exactly_matches_unicode_escaped_json_and_base64_sizes(self):
        metadata = {'model': 'test-model', 'instructions': 'Unicode 中文 "quote"', 'store': False}
        messages = [{'role': 'user', 'text': '尺寸\n测试', 'images': ['one.png']},
                    {'role': 'assistant', 'text': 'print("完成")', 'images': []}]
        data = b'\x89PNG\r\n\x1a\n12345678'
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=len(data))), \
                patch.object(Path, 'read_bytes', return_value=data):
            estimated = api.preflight_request_bytes(metadata, messages, 10000)
            actual = len(json.dumps(dict(metadata, input=api.encode_messages(messages))).encode('utf-8'))
        self.assertEqual(estimated, actual)

    def test_text_alone_is_subject_to_budget_without_model_request(self):
        model = api.OpenAIResponses('test-model', max_request_bytes=100)
        with patch.object(api.subprocess, 'run') as run:
            with self.assertRaises(api.RequestBudgetError):
                model('Explicit instructions', self.messages, 60)
        run.assert_not_called()

    def test_final_serialization_is_checked_again(self):
        model = api.OpenAIResponses('test-model', max_request_bytes=1024)
        with patch.object(api, 'encode_messages', return_value=[{'role': 'user', 'content': 'x'*2048}]), \
                patch.object(api.subprocess, 'run') as run:
            with self.assertRaises(api.RequestBudgetError) as error:
                model('Explicit instructions', self.messages, 60)
        self.assertEqual(error.exception.diagnostic['phase'], 'serialized')
        run.assert_not_called()

    def test_encoding_counting_and_serialization_are_deducted_from_deadline(self):
        model = api.OpenAIResponses('test-model')
        now = [0.0]
        encode, dumps = api.encode_messages, json.dumps
        def slow_encode(messages):
            now[0] += 2
            return encode(messages)
        def slow_count(payload):
            now[0] += 3
            return None
        def slow_dumps(value, *args, **kwargs):
            if isinstance(value, dict) and 'input' in value:
                now[0] += 4
            return dumps(value, *args, **kwargs)
        result = self.result()
        with patch.object(api.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(api, 'encode_messages', side_effect=slow_encode), \
                patch.object(api, 'estimate_request', side_effect=slow_count), \
                patch.object(api.json, 'dumps', side_effect=slow_dumps), \
                patch.object(api.subprocess, 'run', return_value=result) as run:
            model('Explicit instructions', self.messages, 10)
        self.assertEqual(run.call_args.kwargs['timeout'], 1)
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['timeout_seconds'], 1)

    def test_expired_during_encoding_never_sends_request(self):
        model = api.OpenAIResponses('test-model')
        now = [0.0]
        def slow_encode(messages):
            now[0] = 11
            return []
        with patch.object(api.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(api, 'encode_messages', side_effect=slow_encode), \
                patch.object(api.subprocess, 'run') as run:
            with self.assertRaises(TransportError) as error:
                model('Explicit instructions', self.messages, 10)
        self.assertFalse(error.exception.diagnostic['request_sent'])
        run.assert_not_called()

    def test_fatal_http_diagnostic_survives_episode_and_journal_without_raw_fields(self):
        model = api.OpenAIResponses('test-model')
        diagnostic = {'failure_kind': 'http_error', 'http_status': 401,
                      'attribution': 'provider-controlled text', 'raw_response': 'do not retain'}
        response = self.result({'fatal_error': 'API HTTP 401', 'diagnostic': diagnostic})
        with tempfile.TemporaryDirectory() as tmp, patch.object(api.subprocess, 'run', return_value=response):
            executor, finalizer = Mock(), Mock()
            path = Path(tmp) / 'episode'
            result = run_episode(model, executor, finalizer, 'task', path, Budget(),
                                 system_prompt='Explicit instructions')
            journal = [json.loads(line) for line in (path / 'journal.jsonl').read_text().splitlines()]
        expected = {'failure_kind': 'http_error', 'http_status': 401, 'attribution': 'transport_or_service'}
        self.assertEqual(result['events'][0]['diagnostic'], expected)
        self.assertEqual(next(x for x in journal if x['event'] == 'request_failed')['diagnostic'], expected)
        self.assertEqual(result['ended_by'], 'infrastructure_failed')
        self.assertEqual(result['decision_rounds'], 0)
        executor.execute.assert_not_called()
        finalizer.assert_not_called()

    def test_request_budget_failure_is_recorded_without_retry_or_decision(self):
        model = api.OpenAIResponses('test-model', max_request_bytes=100)
        with tempfile.TemporaryDirectory() as tmp, patch.object(api.subprocess, 'run') as run:
            path = Path(tmp) / 'episode'
            result = run_episode(model, Mock(), Mock(), 'task', path, Budget(),
                                 system_prompt='Explicit instructions')
        self.assertEqual(result['model_call_attempts'], 1)
        self.assertEqual(result['decision_rounds'], 0)
        self.assertEqual(result['events'][0]['diagnostic']['attribution'], 'framework_request_budget')
        self.assertEqual(result['ended_by'], 'infrastructure_failed')
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
