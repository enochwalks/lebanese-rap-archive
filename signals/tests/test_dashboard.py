"""The dashboard must never invent numbers the database doesn't hold."""

import sys, tempfile, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigfilter import config, db, dashboard, pipeline

CFG = dict(config.DEFAULTS)
CFG["gate"] = dict(CFG["gate"], min_score=60)
CFG["sources"] = [{"id": -100, "name": "Alpha"}, {"id": -400, "name": "Casino"}]


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.now = int(time.time())
        self._orig = dashboard.db.db_path
        dashboard.db.db_path = lambda: self.tmp

    def tearDown(self):
        dashboard.db.db_path = self._orig

    def _seed(self):
        with db.connect(self.tmp) as conn:
            pipeline.process(conn, CFG, channel_id=-100, msg_id=1,
                             text="BTC/USDT LONG entry 62000 TP1 65000 TP2 67000 SL 61000",
                             ts=self.now - 30, now=self.now)
            pipeline.process(conn, CFG, channel_id=-400, msg_id=2,
                             text="ETH LONG entry 3100 tp 3110 sl 2900",
                             ts=self.now - 20, now=self.now)
            db.set_state(conn, "heartbeat", self.now - 10)
            conn.commit()

    def test_empty_database_does_not_crash(self):
        state = dashboard.collect(CFG, now=self.now)
        self.assertEqual(state["today"]["parsed"], 0)
        self.assertIsNone(state["latest"])
        self.assertFalse(state["alive"])

    def test_counts_match_the_database(self):
        self._seed()
        state = dashboard.collect(CFG, now=self.now)
        self.assertEqual(state["today"]["parsed"], 2)
        self.assertEqual(state["today"]["messages"], 2)
        self.assertTrue(state["alive"])

    def test_breakdown_is_the_stored_one(self):
        self._seed()
        state = dashboard.collect(CFG, now=self.now)
        rows = state["latest"]["breakdown"]
        self.assertEqual(len(rows), len(CFG["scoring"]["weights"]))
        total = sum(r["points"] for r in rows)
        self.assertAlmostEqual(total, state["latest"]["score"], places=0)

    def test_stale_heartbeat_reads_as_not_running(self):
        self._seed()
        state = dashboard.collect(CFG, now=self.now + 3600)
        self.assertFalse(state["alive"])

    def test_channel_rows_carry_promo_share(self):
        self._seed()
        state = dashboard.collect(CFG, now=self.now)
        self.assertEqual({c["name"] for c in state["channels"]}, {"Alpha", "Casino"})
        for channel in state["channels"]:
            self.assertGreaterEqual(channel["trust"], 0.0)
            self.assertLessEqual(channel["trust"], 1.0)


if __name__ == "__main__":
    unittest.main()
