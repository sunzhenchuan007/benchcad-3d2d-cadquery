import unittest
from evaluation.rescore import regrade_iou


class RegradeTests(unittest.TestCase):
    def test_sw_raw_evidence_uses_primitive_not_expert_baseline(self):
        r=regrade_iou({'status':'computed','iou':.97375,'upstream_diagnostics':{'baseline':.5}})
        self.assertAlmostEqual(r['score'],.5)
        self.assertEqual(r['raw_iou'],.97375)
        self.assertFalse(r['geometry_recomputed'])

    def test_missing_baseline_not_silently_zero(self):
        self.assertIsNone(regrade_iou({'status':'computed','iou':1})['score'])
        self.assertIsNone(regrade_iou({'status':'failed'})['score'])
