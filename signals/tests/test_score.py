import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigfilter import config, consensus, score
from sigfilter.parse import parse

CFG = config.DEFAULTS


class TestTrust(unittest.TestCase):
    def test_unknown_channel_is_neutral(self):
        self.assertAlmostEqual(score.channel_trust(0, 0), 0.5)

    def test_small_sample_cannot_outrank_large_one(self):
        lucky = score.channel_trust(3, 0, prior_trades=10)
        proven = score.channel_trust(60, 40, prior_trades=10)
        self.assertLess(lucky, proven)

    def test_losing_channel_sinks(self):
        self.assertLess(score.channel_trust(2, 30), 0.25)

    def test_weight_zero_mutes_channel(self):
        self.assertEqual(score.channel_trust(50, 5, weight=0.0), 0.0)


class TestComponents(unittest.TestCase):
    def test_rr_below_minimum_scores_zero(self):
        self.assertEqual(score.risk_reward_score(0.8, minimum=1.2), 0.0)
        self.assertGreater(score.risk_reward_score(2.0, minimum=1.2), 0.0)

    def test_rr_saturates(self):
        self.assertEqual(score.risk_reward_score(50.0), 1.0)

    def test_conflict_hurts_more_than_agreement_helps(self):
        agree = score.consensus_score({"agreeing_channels": 1, "conflicting_channels": 0})
        conflict = score.consensus_score({"agreeing_channels": 1, "conflicting_channels": 1})
        self.assertLess(conflict, agree)

    def test_discipline_penalises_hype_and_leverage(self):
        clean = parse("BTC LONG entry 62000 tp 64000 sl 61000")
        messy = parse("BTC LONG entry 62000 tp 64000 sl 61000 x100 GUARANTEED easy money")
        self.assertGreater(score.discipline_score(clean), score.discipline_score(messy))

    def test_missing_stop_is_heavily_penalised(self):
        sig = parse("BTC LONG entry 62000 tp 64000")
        self.assertLessEqual(score.discipline_score(sig), 0.5)

    def test_freshness_decays(self):
        self.assertEqual(score.freshness_score(10), 1.0)
        self.assertEqual(score.freshness_score(3600, max_age_minutes=20), 0.0)


class TestGate(unittest.TestCase):
    def _score(self, sig, trust=0.5, info=None, age=60):
        info = info or {"agreeing_channels": 0, "conflicting_channels": 0, "duplicate_of": None}
        total, _ = score.score_signal(sig, trust=trust, consensus_info=info, age_seconds=age, cfg=CFG)
        return total, score.gate_reasons(sig, total, info, age, CFG)

    def test_bad_rr_is_rejected(self):
        sig = parse("BTC LONG entry 62000 TP 62500 SL 60000")
        _, reasons = self._score(sig)
        self.assertTrue(any("risk/reward" in r for r in reasons))

    def test_no_stop_is_rejected(self):
        sig = parse("BTC LONG entry 62000 TP 65000")
        _, reasons = self._score(sig)
        self.assertIn("no stop-loss", reasons)

    def test_conflicting_channels_block(self):
        sig = parse("BTC LONG entry 62000 TP 66000 SL 61000")
        info = {"agreeing_channels": 1, "conflicting_channels": 1, "duplicate_of": None}
        _, reasons = self._score(sig, info=info)
        self.assertTrue(any("opposite direction" in r for r in reasons))

    def test_clean_confirmed_signal_passes(self):
        sig = parse("BTC LONG entry 62000 TP1 64500 TP2 66000 SL 61000")
        info = {"agreeing_channels": 2, "conflicting_channels": 0, "duplicate_of": None}
        total, reasons = self._score(sig, trust=0.75, info=info)
        self.assertEqual(reasons, [], f"score={total}")
        self.assertGreaterEqual(total, CFG["gate"]["min_score"])

    def test_daily_cap_blocks(self):
        sig = parse("BTC LONG entry 62000 TP 66000 SL 61000")
        info = {"agreeing_channels": 2, "conflicting_channels": 0, "duplicate_of": None}
        total, _ = score.score_signal(sig, trust=0.9, consensus_info=info, age_seconds=30, cfg=CFG)
        reasons = score.gate_reasons(sig, total, info, 30, CFG, forwarded_today=99)
        self.assertTrue(any("daily cap" in r for r in reasons))


class TestCopyPasteDetection(unittest.TestCase):
    def test_reposted_text_detected_as_duplicate(self):
        a = "🚀 BTC/USDT LONG\nEntry 62000\nTP 64000\nSL 61000"
        b = "BTC/USDT LONG ⚡\nEntry 62000\nTP 64000\nSL 61000\n@somevipchannel"
        self.assertGreaterEqual(consensus.similarity(a, b), 0.82)

    def test_genuinely_different_posts_are_not_duplicates(self):
        a = "BTC/USDT LONG Entry 62000 TP 64000 SL 61000"
        b = "Gold sell 2345, stop 2352, target 2330, watch the news at 15:30"
        self.assertLess(consensus.similarity(a, b), 0.82)


if __name__ == "__main__":
    unittest.main()
