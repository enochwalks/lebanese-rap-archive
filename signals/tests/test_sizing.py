import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sigfilter import sizing


class TestSizing(unittest.TestCase):
    def test_gold_min_lot_risk_equals_dollar_distance(self):
        # On gold, 0.01 lot risks ~= the stop distance in dollars.
        s = sizing.suggest("metal", 4315, 4305, 200, 2.0)
        self.assertEqual(s["risk_at_min_lot"], 10.0)
        self.assertTrue(s["too_big"])            # 10 of 200 = 5% > 2% target

    def test_tight_stop_is_affordable(self):
        s = sizing.suggest("metal", 4315, 4312, 200, 2.0)
        self.assertFalse(s["too_big"])
        self.assertEqual(s["lot"], 0.01)
        self.assertAlmostEqual(s["pct_at_lot"], 1.5)

    def test_bigger_account_allows_more_lots(self):
        s = sizing.suggest("metal", 4315, 4305, 2000, 2.0)   # $40 target, $10 stop
        self.assertFalse(s["too_big"])
        self.assertEqual(s["lot"], 0.04)         # 40 / (10*100) = 0.04

    def test_forex_sizing(self):
        s = sizing.suggest("forex", 1.0840, 1.0815, 2000, 2.0)  # 25 pip stop
        self.assertIsNotNone(s)
        self.assertGreater(s["lot"], 0)

    def test_no_balance_returns_none(self):
        self.assertIsNone(sizing.suggest("metal", 4315, 4305, 0, 2.0))

    def test_unknown_class_returns_none(self):
        self.assertIsNone(sizing.suggest("index", 18000, 17950, 200, 2.0))
