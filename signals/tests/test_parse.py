import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigfilter.parse import parse, is_update
from sigfilter import symbols


class TestSymbols(unittest.TestCase):
    def test_aliases_collapse(self):
        for text in ("GOLD buy", "XAUUSD buy", "xau/usd buy", "الذهب شراء"):
            self.assertEqual(symbols.extract(text), "XAUUSD", text)

    def test_bare_crypto_gets_usdt_quote(self):
        self.assertEqual(symbols.extract("$SOL long"), "SOLUSDT")

    def test_keywords_are_not_tickers(self):
        for text in ("BUY NOW", "TP1: 63000", "SL 60000", "entry zone"):
            self.assertIsNone(symbols.extract(text))

    def test_asset_class(self):
        self.assertEqual(symbols.asset_class("BTCUSDT"), "crypto")
        self.assertEqual(symbols.asset_class("EURUSD"), "forex")
        self.assertEqual(symbols.asset_class("XAUUSD"), "metal")


class TestParse(unittest.TestCase):
    def test_full_crypto_signal(self):
        sig = parse("#BTC/USDT LONG\nEntry: 62000 - 61500\nTP1: 63000\nTP2: 64500\nSL: 60000\nLeverage: 10x")
        self.assertEqual((sig.symbol, sig.side), ("BTCUSDT", "BUY"))
        self.assertEqual(sig.entries, [62000.0, 61500.0])
        self.assertEqual(sig.tps, [63000.0, 64500.0])
        self.assertEqual(sig.sl, 60000.0)
        self.assertEqual(sig.leverage, 10)

    def test_space_separated_targets(self):
        sig = parse("XAUUSD SELL @ 2345.5  TP 2340 2335  SL 2352")
        self.assertEqual(sig.tps, [2335.0, 2340.0])
        self.assertEqual(sig.sl, 2352.0)

    def test_tp_index_is_not_read_as_price(self):
        sig = parse("SOL LONG entry 140 TP1 145 TP2 152 SL 134")
        self.assertEqual(sig.tps, [145.0, 152.0])

    def test_leverage_not_confused_with_price_digits(self):
        sig = parse("ETH SHORT entry 3100 targets 3050 stop loss 3160 x50")
        self.assertEqual(sig.leverage, 50)

    def test_tp1_is_nearest_target_not_first_listed(self):
        sig = parse("BTC LONG entry 62000 TP 64500 63000 SL 60000")
        self.assertEqual(sig.tp1, 63000.0)

    def test_risk_reward(self):
        sig = parse("GOLD BUY 2340 sl 2330 tp 2360")
        self.assertAlmostEqual(sig.risk_reward(), 2.0)

    def test_stop_on_wrong_side_is_rejected(self):
        sig = parse("BTC LONG entry 62000 TP 63000 SL 63500")
        self.assertIsNone(sig.sl)
        self.assertIn("stop-loss on wrong side of entry", sig.parse_notes)

    def test_targets_on_wrong_side_dropped(self):
        sig = parse("BTC LONG entry 62000 TP 61000 63000 SL 60000")
        self.assertEqual(sig.tps, [63000.0])

    def test_management_updates_are_not_signals(self):
        for text in ("TP1 hit ✅ close half", "move sl to be", "SL hit, sorry team", "cancelled"):
            self.assertTrue(is_update(text), text)
            self.assertIsNone(parse(text), text)

    def test_chat_is_not_a_signal(self):
        self.assertIsNone(parse("gm everyone, market looking bullish today"))
        self.assertIsNone(parse("BTC looking strong"))

    def test_arabic_signal(self):
        sig = parse("ذهب بيع 2345 وقف 2352 هدف 2330")
        self.assertEqual((sig.symbol, sig.side), ("XAUUSD", "SELL"))
        self.assertEqual(sig.sl, 2352.0)

    def test_hype_detected(self):
        sig = parse("ETH SHORT entry 3100 tp 3000 sl 3160 GUARANTEED profit")
        self.assertIn("guaranteed", sig.hype_hits)

    def test_thousands_separator(self):
        sig = parse("BTC LONG entry 62,000 tp 64,000 sl 61,000")
        self.assertEqual(sig.entry, 62000.0)


if __name__ == "__main__":
    unittest.main()
