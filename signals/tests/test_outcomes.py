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
