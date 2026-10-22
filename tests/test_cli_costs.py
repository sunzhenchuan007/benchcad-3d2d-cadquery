import json
from pathlib import Path
import tempfile
import unittest

from benchcad_runtime.cli_costs import estimate_stage, update_report


class CliCostsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trial = {'model': 'gpt-6.1-sol', 'usage': [{'input_tokens': 1_000_000,
                      'cached_input_tokens': 800_000, 'cache_write_input_tokens': 0,
                      'output_tokens': 10_000, 'reasoning_output_tokens': 8_000}]}
        (self.root / 'trial.json').write_text(json.dumps(self.trial))
        (self.root / 'bridge-summary.json').write_text('{"submitted": true}')

    def test_cache_and_reasoning_are_not_double_charged(self):
        self.assertAlmostEqual(estimate_stage(self.root)['estimated_usd'], .58)

    def test_missing_usage_is_unknown_not_zero(self):
        self.trial['usage'] = []
        (self.root / 'trial.json').write_text(json.dumps(self.trial))
        self.assertIsNone(estimate_stage(self.root)['estimated_usd'])

    def test_update_preserves_existing_content_and_has_no_total(self):
        (self.root / 'index.html').write_text('<main><p>Existing recovery note</p></main>')
        update_report(self.root)
        data = update_report(self.root)
        page = (self.root / 'index.html').read_text(encoding='utf-8')
        self.assertEqual(page.count('id="api-stage-cost"'), 1)
        self.assertIn('Existing recovery note', page)
        self.assertNotIn('total', data)
        self.assertIsNone(data['stages']['reconstruction']['estimated_usd'])


if __name__ == '__main__':
    unittest.main()
