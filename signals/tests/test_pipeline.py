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
            second = self._run(conn, -200, "BTCUSDT buy at 62050, target 64000, stop 61100", 2)
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
