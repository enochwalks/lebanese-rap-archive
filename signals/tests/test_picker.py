import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigfilter import picker

CONFIG = '''# Copy to config.yaml and edit.

sources:
  - id: -1001111111111
    name: "Example"
    weight: 1.0

destination: "me"

gate:
  min_score: 70          # raise for fewer, better signals
  daily_cap: 8
'''


class TestSelection(unittest.TestCase):
    def test_numbers_ranges_and_all(self):
        self.assertEqual(picker.parse_selection("1,3", 5), [0, 2])
        self.assertEqual(picker.parse_selection("2-4", 5), [1, 2, 3])
        self.assertEqual(picker.parse_selection("all", 3), [0, 1, 2])

    def test_junk_and_out_of_range_ignored(self):
        self.assertEqual(picker.parse_selection("1, banana, 99", 3), [0])
        self.assertEqual(picker.parse_selection("", 3), [])

    def test_duplicates_collapse(self):
        self.assertEqual(picker.parse_selection("2 2 2", 4), [1])


class TestWrite(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "config.yaml"
        self.path.write_text(CONFIG)

    def test_rewrites_only_the_sources_block(self):
        picker.write_sources([
            {"id": -100123, "name": "GOLD SNIPERS", "weight": 1.0},
            {"id": -100456, "name": "Crypto Pro", "weight": 0.0},
        ], path=self.path)
        text = self.path.read_text()
        self.assertIn("-100123", text)
        self.assertIn("-100456", text)
        self.assertNotIn("-1001111111111", text)
        # everything below sources must survive, comments included
        self.assertIn("min_score: 70          # raise for fewer, better signals", text)
        self.assertIn('destination: "me"', text)

    def test_result_still_parses(self):
        import yaml
        picker.write_sources([{"id": -1, "name": 'Odd "quoted" name', "weight": 1.0}],
                             path=self.path)
        parsed = yaml.safe_load(self.path.read_text())
        self.assertEqual(parsed["sources"][0]["id"], -1)
        self.assertEqual(parsed["gate"]["min_score"], 70)
        self.assertEqual(parsed["destination"], "me")

    def test_backup_is_kept(self):
        _, backup = picker.write_sources([{"id": -1, "name": "x"}], path=self.path)
        self.assertTrue(backup.exists())
        self.assertIn("-1001111111111", backup.read_text())


class TestHints(unittest.TestCase):
    def test_signal_channels_are_suggested(self):
        self.assertTrue(picker.looks_like_signals("GOLD SNIPERS FOREX SIGNALS"))
        self.assertTrue(picker.looks_like_signals("Crypto Pumps Pro"))
        self.assertFalse(picker.looks_like_signals("Al Mayadeen News"))
        self.assertFalse(picker.looks_like_signals("Family group"))


if __name__ == "__main__":
    unittest.main()
