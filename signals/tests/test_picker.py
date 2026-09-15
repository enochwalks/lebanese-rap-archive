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


class TestNonAsciiRoundTrip(unittest.TestCase):
    """A config naming channels in Arabic or with emoji must survive a write and
    a read. Windows opens files in the locale codepage unless told otherwise, so
    this is the exact path that crashed the listener on a real machine."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "config.yaml"
        self.path.write_text(CONFIG, encoding="utf-8")

    def test_arabic_and_emoji_channel_names_round_trip(self):
        import os
        from sigfilter import config

        entries = [
            {"id": -100111, "name": "قناة الميادين | عاجل", "weight": 1.0},
            {"id": -100222, "name": "United Kings™ Signals! 👑", "weight": 1.0},
            {"id": -100333, "name": "GOLD SNIPERS FOREX", "weight": 0.0},
        ]
        picker.write_sources(entries, path=self.path)

        os.environ["SIGFILTER_CONFIG"] = str(self.path)
        try:
            loaded = config.load(self.path)
        finally:
            os.environ.pop("SIGFILTER_CONFIG", None)

        names = [src["name"] for src in loaded["sources"]]
        self.assertIn("قناة الميادين | عاجل", names)
        self.assertIn("United Kings™ Signals! 👑", names)
        self.assertEqual(loaded["gate"]["min_score"], 70)

    def test_loads_under_a_legacy_locale(self):
        """The real failure: Windows opens files in the locale codepage, so a
        config naming a channel in Arabic raised UnicodeDecodeError and took the
        listener down at startup. Simulated here by disabling UTF-8 mode in a
        subprocess - on a UTF-8 host the bug is invisible without it."""
        import os
        import subprocess
        import sys

        picker.write_sources(
            [{"id": -100111, "name": "قناة الميادين | عاجل", "weight": 1.0}],
            path=self.path)

        snippet = (
            "import sys; sys.path.insert(0, %r)\n"
            "from sigfilter import config\n"
            "cfg = config.load(%r)\n"
            "assert cfg['sources'][0]['id'] == -100111\n"
            "print('ok')\n"
        ) % (str(Path(__file__).resolve().parent.parent), str(self.path))

        env = dict(os.environ, PYTHONUTF8="0", LC_ALL="C", LANG="C")
        result = subprocess.run([sys.executable, "-c", snippet], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_file_is_written_as_utf8(self):
        picker.write_sources([{"id": -1, "name": "ذهب", "weight": 1.0}], path=self.path)
        self.assertIn("ذهب", self.path.read_bytes().decode("utf-8"))


class TestGateSetter(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "config.yaml"
        self.path.write_text(
            "sources:\n  - id: -1\n    name: \"x\"\n    weight: 1.0\n\n"
            "gate:\n  min_score: 70          # raise for fewer, better signals\n"
            "  min_risk_reward: 1.2\n  daily_cap: 8\n", encoding="utf-8")

    def test_sets_value_and_keeps_comment(self):
        _, number = picker.set_gate_value("min_score", "55", path=self.path)
        self.assertEqual(number, 55)
        text = self.path.read_text()
        self.assertIn("min_score: 55          # raise for fewer, better signals", text)

    def test_float_value(self):
        _, number = picker.set_gate_value("min_risk_reward", "1.5", path=self.path)
        self.assertEqual(number, 1.5)
        import yaml
        self.assertEqual(yaml.safe_load(self.path.read_text())["gate"]["min_risk_reward"], 1.5)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            picker.set_gate_value("nonsense", "1", path=self.path)

    def test_non_number_raises(self):
        with self.assertRaises(ValueError):
            picker.set_gate_value("min_score", "high", path=self.path)

    def test_other_settings_untouched(self):
        picker.set_gate_value("min_score", "55", path=self.path)
        import yaml
        cfg = yaml.safe_load(self.path.read_text())
        self.assertEqual(cfg["gate"]["daily_cap"], 8)
        self.assertEqual(cfg["sources"][0]["id"], -1)


class TestAutoFollowToggle(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "config.yaml"

    def _write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def test_adds_block_when_absent(self):
        self._write("sources:\n  - id: -1\n    name: \"x\"\n\ndestination: \"me\"\n")
        picker.set_auto_follow(True, path=self.path)
        import yaml
        cfg = yaml.safe_load(self.path.read_text())
        self.assertTrue(cfg["auto_follow"]["enabled"])
        self.assertEqual(cfg["destination"], "me")      # rest preserved

    def test_flips_existing_flag(self):
        self._write("auto_follow:\n  enabled: false\n  refresh_hours: 24\n\ndestination: \"me\"\n")
        picker.set_auto_follow(True, path=self.path)
        import yaml
        cfg = yaml.safe_load(self.path.read_text())
        self.assertTrue(cfg["auto_follow"]["enabled"])
        self.assertEqual(cfg["auto_follow"]["refresh_hours"], 24)

    def test_turns_off(self):
        self._write("auto_follow:\n  enabled: true\n\ndestination: \"me\"\n")
        picker.set_auto_follow(False, path=self.path)
        import yaml
        self.assertFalse(yaml.safe_load(self.path.read_text())["auto_follow"]["enabled"])
