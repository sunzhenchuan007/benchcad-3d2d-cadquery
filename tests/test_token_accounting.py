"""Network-free accounting and transport tests; no real credentials or worker."""
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from benchcad_runtime import local_accounting as local
from benchcad_runtime.accounting import summarize_usage
from benchcad_runtime.episode import Budget, Execution, ReplayModel, Reply, TransportError, run_episode
from benchcad_runtime.openai_api import ModelConfigurationError, OpenAIResponses


RATES = {"model": "example-model", "date": "2026-01-01", "source_url": "https://example.invalid/prices",
         "input_per_million": 2, "output_per_million": 8}
PROVIDER = {"input_tokens": 900, "output_tokens": 500,
            "input_tokens_details": {"cached_tokens": 800}}


class FakeEncoding:
    # Deliberately non-additive across boundaries: catches accidental concatenation.
    def encode_ordinary(self, text):
        return [0] * (len(text.encode('utf-8')) + (1 if text else 0))


def count(text):
    return len(FakeEncoding().encode_ordinary(text))


def payload():
    return {"model": "example-model", "instructions": "Instructions <|endoftext|>",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "task"}]}]}


def response(text="```submit\npass\n```", status="completed"):
    return {"status": status, "model": "example-model", "usage": PROVIDER,
            "output": [{"type": "reasoning", "summary": [{"text": "not visible output"}]},
                       {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def worker_result(body, returncode=0):
    return SimpleNamespace(stdout=json.dumps(body), returncode=returncode)


class OfflineTest(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket'):
            guard = patch(target, side_effect=AssertionError('Network forbidden in unit tests'))
            guard.start()
            self.addCleanup(guard.stop)


class AccountingTests(OfflineTest):
    def setUp(self):
        super().setUp()
        p = patch.object(local, '_load_encoding', return_value=FakeEncoding())
        p.start()
        self.addCleanup(p.stop)

    def test_history_is_recounted_on_every_request_and_parts_stay_separate(self):
        first = payload()
        a = local.estimate_request(first)
        first['input'].extend([
            {'role': 'assistant', 'content': 'previous answer'},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'feedback'},
                                         {'type': 'input_text', 'text': 'next part'}]}])
        b = local.estimate_request(first)
        self.assertEqual(a['input_part_tokens'], [count(first['instructions']), count('task')])
        self.assertEqual(b['input_tokens'], a['input_tokens'] + count('previous answer')
                         + count('feedback') + count('next part'))
        summary = local.summarize_local_usage([
            local.account_response(a, ['ab', 'cd']), local.account_response(b, ['done'])])
        self.assertEqual(summary['known_usage_subtotal']['input_tokens'], a['input_tokens'] + b['input_tokens'])
        self.assertEqual(summary['known_usage_subtotal']['output_tokens'], count('ab') + count('cd') + count('done'))
        self.assertTrue(summary['usage_complete'])

    def test_image_urls_and_base64_are_not_tokenized(self):
        p = payload()
        before = local.estimate_request(p)
        p['input'][0]['content'].extend([
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + 'A' * 10000},
            {'type': 'input_image', 'image_url': 'https://example.invalid/image.png'}])
        after = local.estimate_request(p)
        self.assertEqual(before['input_tokens'], after['input_tokens'])
        self.assertEqual(after['image_count'], 2)
        self.assertIsNone(after['image_tokens'])

    def test_special_token_looking_text_uses_ordinary_encoding(self):
        encoder = Mock()
        encoder.encode_ordinary.return_value = [1, 2, 3]
        with patch.object(local, '_load_encoding', return_value=encoder):
            self.assertEqual(local._count('<|endoftext|>'), 3)
        encoder.encode_ordinary.assert_called_once_with('<|endoftext|>')
        encoder.encode.assert_not_called()

    def test_missing_records_and_failed_response_never_become_zero_usage(self):
        a = local.estimate_request(payload())
        for records in ([], [None], [a], [local.account_response(a, ['ok']), None]):
            summary = local.summarize_local_usage(records, RATES)
            self.assertFalse(summary['usage_complete'])
            self.assertIsNone(summary['visible_text_reference_cost_usd'])
        summary = local.summarize_local_usage([a])
        self.assertIsNone(summary['known_usage_subtotal']['output_tokens'])
        self.assertEqual(summary['known_usage_subtotal']['input_tokens'], a['input_tokens'])

    def test_visible_reference_cost_has_no_cache_discount(self):
        record = local.account_response(local.estimate_request(payload()), ['answer'])
        summary = local.summarize_local_usage([record], {**RATES, 'cached_input_per_million': 0})
        expected = (record['input_tokens'] * 2 + record['output_tokens'] * 8) / 1e6
        self.assertAlmostEqual(summary['visible_text_reference_cost_usd'], expected)
        self.assertIsNone(summary['actual_fee'])
        self.assertIsNone(summary['cached_input_tokens'])
        self.assertIsNone(local.summarize_local_usage([record])['visible_text_reference_cost_usd'])
        diagnostic = summarize_usage([PROVIDER], {**RATES, 'cached_input_per_million': 0})
        self.assertAlmostEqual(diagnostic['estimated_api_fee_usd'], (100 * 2 + 500 * 8) / 1e6)

    def test_rates_and_known_models_are_validated(self):
        record = local.account_response(local.estimate_request(payload()), ['ok'], 'example-model')
        for key in ('input_per_million', 'output_per_million', 'cached_input_per_million'):
            for value in (-1, float('nan'), float('inf'), True, '1'):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    local.summarize_local_usage([record], {**RATES, key: value})
        for key in ('model', 'date', 'source_url', 'input_per_million', 'output_per_million'):
            rates = dict(RATES)
            rates.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                local.summarize_local_usage([record], rates)
        for key in ('model', 'response_model'):
            with self.subTest(model=key), self.assertRaises(ValueError):
                local.summarize_local_usage([{**record, key: 'other-model'}], RATES)


class LoaderTests(OfflineTest):
    def setUp(self):
        super().setUp()
        local._load_encoding.cache_clear()
        self.addCleanup(local._load_encoding.cache_clear)

    def test_missing_library_fails_clearly(self):
        with patch.dict(sys.modules, {'tiktoken': None}):
            with self.assertRaisesRegex(local.LocalAccountingError, 'tiktoken==0.12.0'):
                local._load_encoding()

    def test_wrong_version_fails_clearly(self):
        with patch.dict(sys.modules, {'tiktoken': Mock()}), patch.object(importlib.metadata, 'version', return_value='0.11.0'):
            with self.assertRaisesRegex(local.LocalAccountingError, 'exactly'):
                local._load_encoding()

    def test_missing_disabled_and_corrupt_cache_fail_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            for cache in ('', tmp):
                with patch.dict(os.environ, {'TIKTOKEN_CACHE_DIR': cache}), patch.dict(sys.modules, {'tiktoken': Mock()}), patch.object(importlib.metadata, 'version', return_value='0.12.0'):
                    with self.assertRaisesRegex(local.LocalAccountingError, 'cache'):
                        local._load_encoding()
            cache_file = Path(tmp) / local.hashlib.sha1(local._VOCAB_URL.encode()).hexdigest()
            cache_file.write_bytes(b'corrupt')
            with patch.dict(os.environ, {'TIKTOKEN_CACHE_DIR': tmp}), patch.dict(sys.modules, {'tiktoken': Mock()}), patch.object(importlib.metadata, 'version', return_value='0.12.0'):
                with self.assertRaisesRegex(local.LocalAccountingError, 'checksum'):
                    local._load_encoding()
            self.assertEqual(cache_file.read_bytes(), b'corrupt')

    def test_pinned_definition_matches_installed_tiktoken_without_loading_vocab(self):
        try:
            from tiktoken_ext import openai_public
        except ImportError:
            self.skipTest('optional tiktoken not installed')
        with patch.object(openai_public, 'load_tiktoken_bpe', return_value={}) as loader:
            definition = openai_public.o200k_base()
        self.assertEqual(definition['pat_str'], local._PATTERN)
        self.assertEqual(definition['name'], local.ENCODING)
        loader.assert_called_once_with(local._VOCAB_URL, expected_hash=local._VOCAB_SHA256)

    def test_real_encoding_if_offline_cache_is_available(self):
        try:
            encoding = local._load_encoding()
        except local.LocalAccountingError as exc:
            self.skipTest(str(exc))
        text = '工程 drawing <|endoftext|>'
        self.assertEqual(encoding.encode_ordinary(text), encoding.encode(text, disallowed_special=()))
        self.assertGreater(len(encoding.encode_ordinary('<|endoftext|>')), 1)


class TransportIntegrationTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for p in (patch.object(local, '_load_encoding', return_value=FakeEncoding()),
                  patch.dict(os.environ, {'OPENAI_API_KEY': 'unit-test-placeholder',
                                          'OPENAI_BASE_URL': 'https://example.invalid/v1'})):
            p.start()
            self.addCleanup(p.stop)
        p = patch('benchcad_runtime.openai_api.subprocess.run')
        self.worker = p.start()
        self.addCleanup(p.stop)
        self.worker.return_value = worker_result(response())
        self.model = OpenAIResponses('example-model')
        self.executor = Mock()
        self.executor.execute.return_value = Execution(0, stdout='feedback')

    def episode(self, **kwargs):
        return run_episode(kwargs.pop('model', self.model), self.executor, lambda **kw: 'frozen',
                           'task', self.root / 'logs', system_prompt=kwargs.pop('system_prompt', 'instructions'),
                           **kwargs)

    def test_success_uses_exact_payload_and_preserves_provider_usage(self):
        result = self.episode(rates=RATES)
        sent = json.loads(self.worker.call_args.kwargs['input'])['request']
        expected = local.account_response(local.estimate_request(sent), ['```submit\npass\n```'], 'example-model')
        self.assertEqual(result['local_usage_records'], [expected])
        self.assertEqual(result['usage_records'], [PROVIDER])
        self.assertEqual(result['provider_usage'], summarize_usage([PROVIDER]))
        self.assertEqual(result['usage_metric'], 'local_visible_text')
        self.assertTrue(result['local_visible_text']['usage_complete'])
        self.assertIsNotNone(result['local_visible_text']['visible_text_reference_cost_usd'])
        saved = json.loads((self.root / 'logs/episode.json').read_text())
        self.assertEqual(saved['local_visible_text'], result['local_visible_text'])

    def test_request_worker_receives_same_deadline_as_supervisor(self):
        self.model('instructions', [{'role': 'user', 'text': 'task'}], 480.5)
        call = self.worker.call_args.kwargs
        self.assertEqual(call['timeout'], 480.5)
        self.assertEqual(json.loads(call['input'])['timeout_seconds'], 480.5)

    def test_multiple_rounds_count_actual_repeated_history_and_image(self):
        image = self.root / 'observation.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        self.executor.execute.return_value = Execution(0, stdout='feedback', images=[str(image)])
        self.worker.side_effect = [worker_result(response('```python\npass\n```')), worker_result(response())]
        result = self.episode()
        sent = [json.loads(call.kwargs['input'])['request'] for call in self.worker.call_args_list]
        self.assertEqual(len(sent[1]['input']), 3)
        expected = sum(local.estimate_request(p)['input_tokens'] for p in sent)
        self.assertEqual(result['local_visible_text']['known_usage_subtotal']['input_tokens'], expected)
        self.assertEqual(result['local_usage_records'][1]['image_count'], 1)
        self.assertIn('feedback', sent[1]['input'][2]['content'][0]['text'])

    def test_incomplete_output_counted_saved_and_not_executed(self):
        self.worker.return_value = worker_result(response(status='incomplete'))
        result = self.episode(budget=Budget(rounds=1))
        self.executor.execute.assert_not_called()
        self.assertEqual(result['local_usage_records'][0]['output_tokens'], count('```submit\npass\n```'))
        self.assertTrue(result['local_visible_text']['usage_complete'])
        self.assertEqual((self.root / 'logs/reply_01.txt').read_text(), '```submit\npass\n```')
        self.assertEqual(result['events'][0]['event'], 'unexecutable_response')

    def test_parts_and_refusal_are_visible_but_refusal_never_executes(self):
        body = response()
        body['output'][1]['content'].append({'type': 'refusal', 'refusal': 'declined'})
        self.worker.return_value = worker_result(body)
        reply = self.model('instructions', [{'role': 'user', 'text': 'task'}], 10)
        self.assertFalse(reply.executable)
        self.assertEqual(reply.local_usage['output_tokens'], count('```submit\npass\n```') + count('declined'))

    def test_transport_failure_retains_outgoing_usage_and_unknown_output(self):
        self.worker.side_effect = [subprocess.TimeoutExpired('mock-worker', 1), worker_result(response())]
        result = self.episode(rates=RATES)
        self.assertEqual(result['model_call_attempts'], 2)
        self.assertEqual(result['usage_records'], [None, PROVIDER])
        first, second = result['local_usage_records']
        self.assertEqual(first['input_tokens'], second['input_tokens'])
        self.assertIsNone(first['output_tokens'])
        self.assertFalse(result['local_visible_text']['usage_complete'])
        self.assertFalse(result['provider_usage']['usage_complete'])
        self.assertIsNone(result['local_visible_text']['visible_text_reference_cost_usd'])

    def test_worker_failure_modes_keep_input_estimate(self):
        cases = [OSError('mock launch failure'), worker_result({}, 1),
                 SimpleNamespace(stdout='invalid json', returncode=0), worker_result({}),
                 worker_result({'output': None}), worker_result({'transport_error': 'mock error'}),
                 worker_result({'fatal_error': 'mock config error'})]
        for case in cases:
            with self.subTest(case=type(case).__name__):
                self.worker.side_effect = case if isinstance(case, Exception) else None
                self.worker.return_value = case
                with self.assertRaises((TransportError, ModelConfigurationError)) as caught:
                    self.model('instructions', [{'role': 'user', 'text': 'task'}], 10)
                self.assertGreater(caught.exception.local_usage['input_tokens'], 0)
                self.assertIsNone(caught.exception.local_usage['output_tokens'])

    def test_missing_provider_does_not_replace_local_usage(self):
        body = response()
        body.pop('usage')
        self.worker.return_value = worker_result(body)
        result = self.episode()
        self.assertTrue(result['local_visible_text']['usage_complete'])
        self.assertFalse(result['provider_usage']['usage_complete'])

    def test_replay_usage_is_unknown(self):
        result = self.episode(model=ReplayModel(['```submit\npass\n```']))
        self.assertEqual(result['local_usage_records'], [None])
        self.assertIsNone(result['local_visible_text']['known_usage_subtotal'])
        self.worker.assert_not_called()

    def test_uninstrumented_failures_have_missing_records(self):
        for exception in (TransportError('unknown'), ValueError('bad client')):
            model = Mock(side_effect=exception)
            with tempfile.TemporaryDirectory() as tmp:
                result = run_episode(model, self.executor, Mock(), 'task', Path(tmp) / 'logs',
                                     budget=Budget(max_transport_failures=1))
            self.assertEqual(result['local_usage_records'], [None])
            self.assertFalse(result['local_visible_text']['usage_complete'])

    def test_empty_instructions_rejected_before_worker_and_episode(self):
        for system in ('', '  \n\t'):
            with self.subTest(system=repr(system)), self.assertRaises(ValueError):
                self.model(system, [], 10)
            with self.assertRaises(ValueError):
                self.episode(system_prompt=system)
        self.worker.assert_not_called()
        self.assertFalse((self.root / 'logs').exists())

    def test_missing_tokenizer_blocks_api_and_is_explicit(self):
        with patch.object(local, '_load_encoding', side_effect=local.LocalAccountingError('Offline cache missing')):
            with self.assertRaisesRegex(local.LocalAccountingError, 'cache missing'):
                self.model('instructions', [], 10)
        self.worker.assert_not_called()
        with patch.object(local, '_load_encoding', side_effect=local.LocalAccountingError('Offline cache missing')):
            result = self.episode()
        self.assertEqual(result['events'][0]['error'], 'Offline cache missing')
        self.assertFalse(result['local_visible_text']['usage_complete'])
        self.worker.assert_not_called()

    def test_incomplete_text_is_recounted_as_history_on_next_call(self):
        body = response('first part', status='incomplete')
        body['output'][1]['content'].append({'type': 'output_text', 'text': 'second part'})
        self.worker.side_effect = [worker_result(body), worker_result(response())]
        result = self.episode()
        second_payload = json.loads(self.worker.call_args.kwargs['input'])['request']
        self.assertEqual(second_payload['input'][1]['content'], 'first part\nsecond part')
        first, second = result['local_usage_records']
        self.assertEqual(first['output_tokens'], count('first part') + count('second part'))
        self.assertEqual(second['input_part_tokens'][2], count('first part\nsecond part'))
        self.assertEqual(result['code_executions'], 1)

    def test_fatal_response_keeps_accounting_in_episode_report(self):
        self.worker.return_value = worker_result({'fatal_error': 'mock model error'})
        result = self.episode()
        self.assertEqual(result['ended_by'], 'infrastructure_failed')
        self.assertGreater(result['local_usage_records'][0]['input_tokens'], 0)
        self.assertIsNone(result['local_usage_records'][0]['output_tokens'])
        self.assertFalse(result['local_visible_text']['usage_complete'])

    def test_price_model_mismatch_rejected_before_call(self):
        with self.assertRaisesRegex(ValueError, 'model'):
            self.episode(rates={**RATES, 'model': 'wrong-model'})
        self.worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
