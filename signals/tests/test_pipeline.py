"""End-to-end: many channels in, at most the good ones out."""

import sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigfilter import config, db, pipeline

CFG = dict(config.DEFAULTS)
CFG["sources"] = [
    {"id": -100, "name": "Alpha", "weight": 1.0},
    {"id": -200, "name": "Beta", "weight": 1.0},
    {"id": -300, "name": "Relay", "weight": 1.0},
    {"id": -400, "name": "Casino", "weight": 1.0},
]

CLEAN = "BTC/USDT LONG\nEntry: 62000\nTP1: 64500\nTP2: 66000\nSL: 61000"
RELAYED = "🚀 BTC/USDT LONG ⚡\nEntry: 62000\nTP1: 64500\nTP2: 66000\nSL: 61000\n@relaychannel"
OPPOSITE = "BTC/USDT SHORT\nEntry 62000\nTP 59000\nSL 63000"
JUNK = "ETH LONG x100 entry 3100 tp 3200 sl 2900 GUARANTEED easy money 🚀🚀🚀"


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = self.tmp.name
        self.now = int(time.time())

    def _run(self, conn, channel_id, text, msg_id, offset=0):
        return pipeline.process(conn, CFG, channel_id=channel_id, msg_id=msg_id,
                                text=text, ts=self.now - offset, now=self.now)

    def test_same_message_twice_is_deduped(self):
        with db.connect(self.db) as conn:
            self._run(conn, -100, CLEAN, 1)
            again = self._run(conn, -100, CLEAN, 1)
        self.assertEqual(again["status"], "duplicate_message")

    def test_independent_agreement_raises_score(self):
        with db.connect(self.db) as conn:
            first = self._run(conn, -100, CLEAN, 1, offset=300)
            # Same instrument/direction and a comparable target, so the only
            # meaningful difference from the first is the cross-channel agreement.
            second = self._run(conn, -200, "BTCUSDT buy at 62000, target 66000, stop 61000", 2)
        self.assertGreater(second["score"], first["score"])
        self.assertEqual(second["consensus"]["agreeing_channels"], 1)

    def test_copy_paste_is_not_counted_as_confirmation(self):
        with db.connect(self.db) as conn:
            self._run(conn, -100, CLEAN, 1, offset=60)
            relay = self._run(conn, -300, RELAYED, 2)
        self.assertEqual(relay["consensus"]["duplicate_of"], -100)
        self.assertEqual(relay["consensus"]["agreeing_channels"], 0)

    def test_opposite_calls_block_both_directions(self):
        with db.connect(self.db) as conn:
            self._run(conn, -100, CLEAN, 1, offset=120)
            against = self._run(conn, -200, OPPOSITE, 2)
        self.assertEqual(against["verdict"], "REJECT")
        self.assertTrue(any("opposite direction" in r for r in against["reasons"]))

    def test_casino_signal_rejected(self):
        with db.connect(self.db) as conn:
            result = self._run(conn, -400, JUNK, 1)
        self.assertEqual(result["verdict"], "REJECT")

    def test_chat_never_reaches_scoring(self):
        with db.connect(self.db) as conn:
            result = self._run(conn, -100, "gm team, big moves coming today 🚀", 1)
        self.assertEqual(result["status"], "not_a_signal")

    def test_stale_signal_rejected(self):
        with db.connect(self.db) as conn:
            result = self._run(conn, -100, CLEAN, 1, offset=3600)
        self.assertTrue(any("stale" in r for r in result["reasons"]))

    def test_daily_cap_enforced_across_channels(self):
        capped = dict(CFG)
        capped["gate"] = dict(CFG["gate"], daily_cap=1, min_score=0)
        with db.connect(self.db) as conn:
            first = pipeline.process(conn, capped, channel_id=-100, msg_id=1,
                                     text=CLEAN, ts=self.now, now=self.now)
            self.assertEqual(first["verdict"], "ACCEPT")
            db.mark_forwarded(conn, first["signal_id"])
            second = pipeline.process(conn, capped, channel_id=-200, msg_id=2,
                                      text="SOL LONG entry 140 tp 152 sl 134",
                                      ts=self.now, now=self.now)
        self.assertTrue(any("daily cap" in r for r in second["reasons"]))


if __name__ == "__main__":
    unittest.main()


class TestPromoPenalty(unittest.TestCase):
    """A channel whose feed is mostly deposit pitches loses trust for its signals."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.now = int(time.time())

    def test_real_funnel_text_is_flagged(self):
        from sigfilter import promo
        for text in (
            "If you know you have $1000 and above this is your opportunity to "
            "make it big in our investment plan. #4days plan",
            "Invest $1000 get $11,000",
            "Slot is Limited CLICK ON THE PINNED MESSAGE TO CONTACT ADMIN",
            "BOOK YOUR SLOT NOW FOR ACCOUNT MANAGEMENT",
            "NB: Our commission is 20% and Taken from profits",
        ):
            self.assertTrue(promo.is_promo(text), text)

    def test_real_signals_are_never_flagged(self):
        from sigfilter import promo
        for text in (CLEAN, "XAUUSD BUY 2340 TP 2365 SL 2332",
                     "SOL LONG entry 140 TP1 145 SL 134"):
            self.assertFalse(promo.is_promo(text), text)

    def test_penalty_needs_a_sample_before_biting(self):
        from sigfilter import promo
        self.assertEqual(promo.penalty(3, 5), 1.0)      # day one proves nothing
        self.assertLess(promo.penalty(15, 40), 0.7)     # a third of the feed does

    def test_funnel_channel_scores_lower_than_clean_one(self):
        with db.connect(self.tmp) as conn:
            for i in range(30):                          # Casino spams the funnel
                pipeline.process(conn, CFG, channel_id=-400, msg_id=1000 + i,
                                 text="Invest $1000 get $11,000, contact admin, slot is limited",
                                 ts=self.now - 600, now=self.now)
            for i in range(30):                          # Alpha posts ordinary chat
                pipeline.process(conn, CFG, channel_id=-100, msg_id=2000 + i,
                                 text="good morning team, watching the open",
                                 ts=self.now - 600, now=self.now)
            funnel = pipeline.process(conn, CFG, channel_id=-400, msg_id=9001,
                                      text=CLEAN, ts=self.now, now=self.now)
            clean = pipeline.process(conn, CFG, channel_id=-100, msg_id=9002,
                                     text="SOL LONG entry 140 TP1 152 SL 134",
                                     ts=self.now, now=self.now)
        self.assertLess(funnel["promo"][2], 1.0)
        self.assertEqual(clean["promo"][2], 1.0)
        self.assertLess(funnel["trust"], clean["trust"])


class TestHistoricalBackfill(unittest.TestCase):
    """Backfill scores old posts on quality and never forwards them."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.now = int(time.time())

    def test_stale_signal_rejected_live_but_scored_historically(self):
        old_ts = self.now - 3 * 86400        # three days old
        with db.connect(self.tmp) as conn:
            live = pipeline.process(conn, CFG, channel_id=-100, msg_id=1,
                                    text=CLEAN, ts=old_ts, now=self.now)
            hist = pipeline.process(conn, CFG, channel_id=-100, msg_id=2,
                                    text=CLEAN, ts=old_ts, now=self.now, historical=True)
        # Live: rejected as stale. Historical: age is not a reason.
        self.assertTrue(any("stale" in r for r in live["reasons"]))
        self.assertFalse(any("stale" in r for r in hist["reasons"]))

    def test_backfill_does_not_touch_daily_cap(self):
        capped = dict(CFG)
        capped["gate"] = dict(CFG["gate"], daily_cap=1, min_score=0)
        with db.connect(self.tmp) as conn:
            for i in range(5):
                r = pipeline.process(conn, capped, channel_id=-100, msg_id=100 + i,
                                     text=CLEAN, ts=self.now - 86400, now=self.now,
                                     historical=True)
                self.assertFalse(any("daily cap" in reason for reason in r["reasons"]))

    def test_historical_flag_is_reported(self):
        with db.connect(self.tmp) as conn:
            r = pipeline.process(conn, CFG, channel_id=-100, msg_id=1,
                                 text=CLEAN, ts=self.now - 86400, now=self.now,
                                 historical=True)
        self.assertTrue(r["historical"])

    def test_backfilled_messages_count_on_their_own_day_not_today(self):
        from sigfilter.dashboard import collect
        with db.connect(self.tmp) as conn:
            pipeline.process(conn, CFG, channel_id=-100, msg_id=1, text=CLEAN,
                             ts=self.now - 5 * 86400, now=self.now, historical=True)
        dashboard_db = __import__("sigfilter.dashboard", fromlist=["db"]).db
        orig = dashboard_db.db_path
        dashboard_db.db_path = lambda: self.tmp
        try:
            state = collect(CFG, now=self.now)
        finally:
            dashboard_db.db_path = orig
        self.assertEqual(state["today"]["messages"], 0)   # it was 5 days ago
