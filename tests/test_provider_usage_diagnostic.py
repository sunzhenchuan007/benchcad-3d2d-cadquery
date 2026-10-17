"""An optional provider diagnostic must not break local accounting reports."""
import unittest
from benchcad_runtime.accounting import summarize_usage


class ProviderDiagnosticTests(unittest.TestCase):
    def test_malformed_provider_fields_are_incomplete_not_fatal(self):
        for record in (42, 'unknown', [], {'input_tokens': None,
                        'input_tokens_details': {'cached_tokens': 0}},
                       {'input_tokens': 3, 'input_tokens_details': 'unknown'}):
            with self.subTest(record=record):
                result = summarize_usage([record])
                self.assertFalse(result['usage_complete'])
                self.assertIsNone(result['estimated_api_fee_usd'])
                self.assertIsNone(result['actual_fee'])

    def test_valid_provider_usage_preserved(self):
        result = summarize_usage([{'input_tokens': 24, 'output_tokens': 8,
                                   'input_tokens_details': {'cached_tokens': 0}}])
        self.assertTrue(result['usage_complete'])
        self.assertEqual(result['known_usage_subtotal']['input_tokens'], 24)
