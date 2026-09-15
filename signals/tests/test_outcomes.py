import sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigfilter import outcomes


def row(symbol, asset_class, side, entry, tp1, sl, ts):
    return {"symbol": symbol, "asset_class": asset_class, "side": side,
            "entry": entry, "tp1": tp1, "sl": sl, "ts": ts, "id": 1}


class TestYahooTicker(unittest.TestCase):
    def test_metal_and_index_mapping(self):
        self.assertEqual(outcomes._yahoo_ticker("XAUUSD", "metal"), "XAUUSD=X")
        self.assertEqual(outcomes._yahoo_ticker("NAS100", "index"), "^NDX")

    def test_forex_pairs_get_x_suffix(self):
        self.assertEqual(outcomes._yahoo_ticker("EURUSD", "forex"), "EURUSD=X")
        self.assertEqual(outcomes._yahoo_ticker("GBPJPY", "forex"), "GBPJPY=X")

    def test_unknown_symbol_has_no_ticker(self):
        self.assertIsNone(outcomes._yahoo_ticker("WEIRDCOIN", "crypto"))


def fake_yahoo(highs, lows):
    """A recorded-shape Yahoo chart response."""
    return {"chart": {"result": [{
        "timestamp": list(range(len(highs))),
        "indicators": {"quote": [{"high": highs, "low": lows}]},
    }], "error": None}}


class TestGrading(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.old = self.now - 2 * 86400        # safely past the 24h window

    def _grade_with(self, r, highs, lows):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = fake_yahoo(highs, lows)
        with mock.patch.object(outcomes.requests, "get", return_value=resp):
            return outcomes.grade_signal(r, horizon_hours=24, now=self.now)

    def test_gold_buy_hits_target_first(self):
        r = row("XAUUSD", "metal", "BUY", 2340, 2360, 2330, self.old)
        # price rises through TP before ever touching SL
        verdict = self._grade_with(r, highs=[2345, 2362], lows=[2338, 2355])
        self.assertEqual(verdict, "WIN")

    def test_gold_buy_hits_stop_first(self):
        r = row("XAUUSD", "metal", "BUY", 2340, 2360, 2330, self.old)
        verdict = self._grade_with(r, highs=[2344, 2361], lows=[2328, 2335])
        self.assertEqual(verdict, "LOSS")     # SL touched in candle 1

    def test_forex_sell_target(self):
        r = row("EURUSD", "forex", "SELL", 1.0840, 1.0790, 1.0870, self.old)
        verdict = self._grade_with(r, highs=[1.0845, 1.0850], lows=[1.0820, 1.0785])
        self.assertEqual(verdict, "WIN")

    def test_same_candle_touching_both_is_a_loss(self):
        r = row("XAUUSD", "metal", "BUY", 2340, 2360, 2330, self.old)
        verdict = self._grade_with(r, highs=[2365], lows=[2325])   # both in one candle
        self.assertEqual(verdict, "LOSS")

    def test_neither_hit_past_window_expires(self):
        r = row("XAUUSD", "metal", "BUY", 2340, 2400, 2300, self.old)
        verdict = self._grade_with(r, highs=[2350, 2355], lows=[2335, 2338])
        self.assertEqual(verdict, "EXPIRED")

    def test_no_prices_past_window_is_nodata_not_pending(self):
        r = row("XAUUSD", "metal", "BUY", 2340, 2360, 2330, self.old)
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"chart": {"result": []}}
        with mock.patch.object(outcomes.requests, "get", return_value=resp):
            self.assertEqual(outcomes.grade_signal(r, 24, now=self.now), "NODATA")

    def test_no_prices_inside_window_stays_pending(self):
        recent = row("XAUUSD", "metal", "BUY", 2340, 2360, 2330, self.now - 600)
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"chart": {"result": []}}
        with mock.patch.object(outcomes.requests, "get", return_value=resp):
            self.assertIsNone(outcomes.grade_signal(recent, 24, now=self.now))

    def test_crypto_still_uses_binance_shape(self):
        r = row("BTCUSDT", "crypto", "BUY", 62000, 64000, 61000, self.old)
        # Binance klines: [openT, o, high, low, c, ...]
        klines = [[0, "0", "63000", "61500", "0"], [0, "0", "64100", "63000", "0"]]
        resp = mock.Mock(status_code=200)
        resp.json.return_value = klines
        with mock.patch.object(outcomes.requests, "get", return_value=resp):
            self.assertEqual(outcomes.grade_signal(r, 24, now=self.now), "WIN")


if __name__ == "__main__":
    unittest.main()


class TestGradePendingBreakdown(unittest.TestCase):
    """grade_pending reports how each signal resolved, and reset re-opens the
    soft ones so a longer window can be tried."""

    def setUp(self):
        import tempfile
        from sigfilter import db, config, pipeline
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.cfg = dict(config.DEFAULTS)
        self.cfg["gate"] = dict(self.cfg["gate"], min_score=0)
        self.now = int(time.time())
        self._orig = db.db_path
        db.db_path = lambda: self.db
        self.db_mod = db
        old = self.now - 5 * 86400
        with db.connect(self.db) as conn:
            pipeline.process(conn, self.cfg, channel_id=-1, msg_id=1,
                             text="XAUUSD BUY entry 2340 TP 2360 SL 2330",
                             ts=old, now=self.now, historical=True)
            conn.commit()

    def tearDown(self):
        self.db_mod.db_path = self._orig

    def test_expired_can_be_reset_and_retried(self):
        from sigfilter import outcomes
        # First pass: no candles -> EXPIRED (past window).
        empty = mock.Mock(status_code=200)
        empty.json.return_value = {"chart": {"result": []}}
        with mock.patch.object(outcomes.requests, "get", return_value=empty):
            counts = outcomes.grade_pending(self.cfg, now=self.now, polite_delay=0)
        self.assertEqual(counts.get("NODATA", 0), 1)

        with self.db_mod.connect(self.db) as conn:
            reopened = self.db_mod.reset_soft_outcomes(conn)
            conn.commit()
        self.assertEqual(reopened, 1)

        # Second pass with data -> WIN.
        resp = mock.Mock(status_code=200)
        resp.json.return_value = fake_yahoo([2345, 2362], [2338, 2355])
        with mock.patch.object(outcomes.requests, "get", return_value=resp):
            counts = outcomes.grade_pending(self.cfg, now=self.now, polite_delay=0)
        self.assertEqual(counts.get("WIN", 0), 1)

    def test_win_is_never_reset(self):
        from sigfilter import outcomes
        resp = mock.Mock(status_code=200)
        resp.json.return_value = fake_yahoo([2345, 2362], [2338, 2355])
        with mock.patch.object(outcomes.requests, "get", return_value=resp):
            outcomes.grade_pending(self.cfg, now=self.now, polite_delay=0)
        with self.db_mod.connect(self.db) as conn:
            reopened = self.db_mod.reset_soft_outcomes(conn)
            conn.commit()
        self.assertEqual(reopened, 0)      # WIN stays put


class TestSourceSelection(unittest.TestCase):
    """MT5 is preferred for non-crypto; Yahoo is the fallback; Binance stays for
    crypto. Verified with both sources mocked."""

    def setUp(self):
        self.now = int(time.time())
        self.start = self.now - 3 * 86400

    def test_mt5_used_for_gold_when_available(self):
        with mock.patch.object(outcomes.mt5source, "candles",
                               return_value=[(2350.0, 2345.0), (2360.0, 2352.0)]):
            candles, source = outcomes.candles_with_source(
                "XAUUSD", "metal", self.start, self.now)
        self.assertEqual(source, "MT5")
        self.assertEqual(len(candles), 2)

    def test_falls_back_to_yahoo_when_mt5_absent(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = fake_yahoo([2350, 2360], [2345, 2352])
        with mock.patch.object(outcomes.mt5source, "candles", return_value=None), \
             mock.patch.object(outcomes.requests, "get", return_value=resp):
            candles, source = outcomes.candles_with_source(
                "XAUUSD", "metal", self.start, self.now)
        self.assertEqual(source, "Yahoo")
        self.assertEqual(len(candles), 2)

    def test_crypto_prefers_binance(self):
        klines = [[0, "0", "63000", "61500", "0"]]
        resp = mock.Mock(status_code=200)
        resp.json.return_value = klines
        with mock.patch.object(outcomes.mt5source, "candles", return_value=None), \
             mock.patch.object(outcomes.requests, "get", return_value=resp):
            candles, source = outcomes.candles_with_source(
                "BTCUSDT", "crypto", self.start, self.now)
        self.assertEqual(source, "Binance")

    def test_nothing_anywhere_reports_cleanly(self):
        with mock.patch.object(outcomes.mt5source, "candles", return_value=None):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {"chart": {"result": []}}
            with mock.patch.object(outcomes.requests, "get", return_value=resp):
                candles, source = outcomes.candles_with_source(
                    "XAUUSD", "metal", self.start, self.now)
        self.assertEqual(candles, [])


class TestMt5Resolver(unittest.TestCase):
    def test_alias_resolution(self):
        from sigfilter import mt5source

        class FakeSym:
            def __init__(self, name): self.name = name

        fake = mock.Mock()
        fake.symbol_info.return_value = None
        fake.symbols_get.return_value = [FakeSym("GOLD"), FakeSym("EURUSD.pro")]
        self.assertEqual(mt5source._resolve(fake, "XAUUSD"), "GOLD")
        self.assertEqual(mt5source._resolve(fake, "EURUSD"), "EURUSD.pro")

    def test_unavailable_when_package_missing(self):
        from sigfilter import mt5source
        with mock.patch.object(mt5source, "_mt5", return_value=None):
            self.assertFalse(mt5source.available())
            self.assertIsNone(mt5source.candles("XAUUSD", 0, 1))
