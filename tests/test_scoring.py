import unittest
from evaluation.scoring import reconstruction_score


class ScoringTests(unittest.TestCase):
    def test_thresholds_use_corrected_not_raw_iou(self):
        result = reconstruction_score(.95, .5)
        self.assertAlmostEqual(result['corrected_iou'], .9)
        self.assertEqual(result['reconstruction_score_100'], 0)
        result = reconstruction_score(.97375, .5)
        self.assertAlmostEqual(result['corrected_iou'], .9475)
        self.assertAlmostEqual(result['reconstruction_score_100'], 50)
        self.assertEqual(reconstruction_score(.9975, .5)['reconstruction_score_100'], 100)

    def test_clamping_and_identity(self):
        self.assertEqual(reconstruction_score(.2, .6)['corrected_iou'], 0)
        self.assertEqual(reconstruction_score(1, .8)['reconstruction_score_100'], 100)
        self.assertEqual(reconstruction_score(.9, 0)['reconstruction_score_100'], 0)
        self.assertEqual(reconstruction_score(.995, 0)['reconstruction_score_100'], 100)

    def test_primitive_reference_uses_upstream_degenerate_rule(self):
        self.assertEqual(reconstruction_score(1, 1)['reconstruction_score_100'], 100)
        self.assertEqual(reconstruction_score(.999, 1)['reconstruction_score_100'], 0)

    def test_rejects_invalid_numbers(self):
        for value in [None, True, -1, 1.01, float('nan'), float('inf')]:
            for args in [(value, .5), (.5, value)]:
                with self.assertRaises(ValueError):
                    reconstruction_score(*args)

    def test_old_full_threshold_is_no_longer_full_score(self):
        self.assertAlmostEqual(reconstruction_score(.99, 0)['score'], .09/.095)
        self.assertEqual(reconstruction_score(.995, 0)['score'], 1)
        self.assertAlmostEqual(reconstruction_score(.9475, 0)['score'], .5)

    def test_v2_cached_result_regraded_without_mutating_history(self):
        from evaluation.interfaces import upgrade_iou_result
        old={'status':'computed','metric_version':'normalized-iou48-primitive-threshold/2',
             'iou':.99,'raw_iou':.99,'primitive_baseline':0,'corrected_iou':.99,'reconstruction_score_100':100}
        result=upgrade_iou_result(old)
        self.assertAlmostEqual(result['score'],.09/.095)
        self.assertEqual(old['reconstruction_score_100'],100)
        self.assertNotEqual(old['metric_version'],result['metric_version'])
        old['reconstruction_score_100']=12
        with self.assertRaises(ValueError):upgrade_iou_result(old)
