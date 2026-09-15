import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sigfilter import singleton


class TestSingleton(unittest.TestCase):
    def setUp(self):
        self.lock = str(Path(tempfile.mkdtemp()) / "test.lock")

    def test_second_acquire_is_refused(self):
        first = singleton.acquire(self.lock)
        with self.assertRaises(singleton.AlreadyRunning):
            singleton.acquire(self.lock)
        first.close()

    def test_lock_is_reusable_after_release(self):
        first = singleton.acquire(self.lock)
        first.close()                      # closing releases the OS lock
        second = singleton.acquire(self.lock)   # must succeed now
        second.close()

    def test_survives_a_crash_simulated_by_subprocess(self):
        import subprocess
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from sigfilter import singleton\n"
            "singleton.acquire(%r)\n"
            "raise SystemExit(1)\n"      # dies while holding the lock
        ) % (str(Path(__file__).resolve().parent.parent), self.lock)
        subprocess.run([sys.executable, "-c", code], capture_output=True)
        # OS released the lock when that process died; we can take it now.
        handle = singleton.acquire(self.lock)
        handle.close()


if __name__ == "__main__":
    unittest.main()
