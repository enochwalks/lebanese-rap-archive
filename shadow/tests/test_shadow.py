import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shadow  # noqa: E402


def make_cfg(workspace):
    return {"workspace": str(workspace), "providers": [], "checkouts": {}, "paths": {}}


class RegistryTests(unittest.TestCase):
    def test_real_registry_is_consistent(self):
        projects = shadow.load_registry()
        self.assertIn("rap-archive", projects)
        order, _ = shadow.dependency_order(projects)
        # edit-engine feeds rap-archive, so it must come first
        self.assertLess(order.index("edit-engine"), order.index("rap-archive"))
        for pid, p in projects.items():
            self.assertTrue(p["tasks"], f"{pid} has no tasks")
            for name, t in p["tasks"].items():
                self.assertTrue(t.get("cmd") or t.get("manual"), f"{pid}:{name} has no cmd")
                self.assertTrue(t.get("desc"), f"{pid}:{name} has no desc")

    def test_registry_paths_exist_in_this_repo(self):
        # Projects on master must point at files that really exist here.
        repo = Path(shadow.HERE).parent
        cfg = {"workspace": str(repo.parent), "checkouts": {"lebanese-rap-archive": str(repo)}}
        for pid, p in shadow.load_registry().items():
            if p["branch"] != "master":
                continue
            d = shadow.project_dir(p, cfg)
            for f in p["needs"]["files"]:
                if not shadow.is_secret(f):
                    self.assertTrue((d / f).exists(), f"{pid}: {f} missing")

    def test_cycle_and_unknown_detected(self):
        base = {"tasks": {}, "needs": {}, "logs": [], "feeds": []}
        cyc = {"a": dict(base, depends_on=["b"]), "b": dict(base, depends_on=["a"])}
        with self.assertRaisesRegex(ValueError, "cycle"):
            shadow.dependency_order(cyc)
        with self.assertRaisesRegex(ValueError, "unknown"):
            shadow.dependency_order({"a": dict(base, depends_on=["zzz"])})


class SafetyTests(unittest.TestCase):
    def test_secret_files_detected(self):
        for f in [".env", "signals/.env", "token.pickle", "client_secrets.json",
                  "token_readonly.pickle", "me.session", "config.toml"]:
            self.assertTrue(shadow.is_secret(f), f)
        for f in ["main.py", ".env.example", "agent/config.json", "README.md"]:
            self.assertFalse(shadow.is_secret(f), f)

    def test_safe_path_blocks_escape_and_secrets(self):
        with tempfile.TemporaryDirectory() as ws:
            proj = Path(ws) / "p"
            proj.mkdir()
            (proj / "ok.py").write_text("x")
            p = {"id": "p", "dir": "p"}
            cfg = make_cfg(ws)
            self.assertEqual(shadow.safe_path(p, cfg, "ok.py"), (proj / "ok.py").resolve())
            with self.assertRaises(PermissionError):
                shadow.safe_path(p, cfg, "../outside.txt")
            with self.assertRaises(PermissionError):
                shadow.safe_path(p, cfg, ".env")

    def test_danger_task_refused_without_terminal(self):
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "p").mkdir()
            p = {"id": "p", "dir": "p", "tasks": {"boom": {"cmd": "echo hi", "danger": True}}}
            stdin = sys.stdin
            sys.stdin = io.StringIO("")  # not a tty
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    code, out = shadow.run_task(p, "boom", make_cfg(ws), capture=True)
            finally:
                sys.stdin = stdin
            self.assertEqual((code, out), (1, "Not run."))


class AgentTests(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(shadow.extract_json('sure! ```json\n{"action":"answer","text":"a {b}"}\n```'),
                         {"action": "answer", "text": "a {b}"})
        self.assertIsNone(shadow.extract_json("no json here"))
        self.assertEqual(shadow.extract_json('{bad} then {"action":"x"}'), {"action": "x"})

    def test_agent_loop_runs_task_then_answers(self):
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "p").mkdir()
            projects = {"p": {"id": "p", "name": "P", "dir": "p", "depends_on": [], "feeds": [],
                              "logs": [], "needs": {}, "summary": "",
                              "tasks": {"hello": {"cmd": f'"{sys.executable}" -c "print(41+1)"',
                                                  "desc": "say"}}}}
            seen = []

            def fake_brain(messages):
                seen.append(messages[-1]["content"])
                if len(seen) == 1:
                    return json.dumps({"action": "run_task", "project": "p", "task": "hello"})
                return json.dumps({"action": "answer", "text": "done"})

            with contextlib.redirect_stderr(io.StringIO()):
                out = shadow.agent("go", projects, make_cfg(ws), brain=fake_brain, verbose=False)
            self.assertEqual(out, "done")
            self.assertIn("42", seen[1])

    def test_brain_falls_through_to_next_provider(self):
        calls = []
        real = shadow.call_provider

        def fake(prov, messages, timeout=180):
            calls.append(prov["type"])
            if prov["type"] == "ollama":
                raise shadow.BrainError("down")
            return "hi"

        shadow.call_provider = fake
        try:
            b = shadow.Brain([{"type": "ollama"}, {"type": "openai"}])
            self.assertEqual(b([]), "hi")
            self.assertEqual(calls, ["ollama", "openai"])
        finally:
            shadow.call_provider = real

    def test_offline_answer_finds_runbook_fix(self):
        projects = shadow.load_registry()
        out = shadow.offline_answer("run.lock stuck another run in progress", projects,
                                    make_cfg("/nonexistent"))
        self.assertIn("unlock", out)


class ScanTests(unittest.TestCase):
    def test_scan_produces_valid_toml(self):
        import tomllib
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "My Bot"
            (root / "tests").mkdir(parents=True)
            (root / "main.py").write_text("import anthropic\nsubprocess.run(['ffmpeg'])")
            (root / "requirements.txt").write_text("requests")
            (root / ".env").write_text("SECRET=1")
            (root / "tests" / "test_x.py").write_text("")
            data = tomllib.loads(shadow.scan(root))
            p = data["projects"]["my-bot"]
            self.assertIn("main", p["tasks"])
            self.assertIn("install", p["tasks"])
            self.assertIn("ffmpeg", p["needs"]["bins"])
            self.assertIn(".env", p["secrets"])
            self.assertIn("calls Claude", p["notes"])


class WireFormatTests(unittest.TestCase):
    """Real HTTP against a fake local model server, for each provider type."""

    def serve(self, replies, shape):
        import http.server
        import threading
        received = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, dict(self.headers), body))
                text = replies.pop(0)
                out = {"openai": {"choices": [{"message": {"content": text}}]},
                       "ollama": {"message": {"content": text}},
                       "anthropic": {"content": [{"type": "text", "text": text}]}}[shape]
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}", received

    def run_agent(self, prov_type, path_expected):
        with tempfile.TemporaryDirectory() as ws:
            proj = Path(ws) / "p"
            proj.mkdir()
            (proj / "notes.txt").write_text("the answer is 7")
            (proj / ".env").write_text("SECRET=hunter2")
            projects = {"p": {"id": "p", "name": "P", "dir": "p", "depends_on": [], "feeds": [],
                              "logs": [], "needs": {}, "summary": "", "tasks": {}}}
            replies = ['{"action":"read_file","project":"p","path":".env"}',
                       '{"action":"read_file","project":"p","path":"notes.txt"}',
                       '{"action":"answer","text":"7"}']
            url, received = self.serve(replies, prov_type)
            import os
            os.environ["SHADOW_TEST_KEY"] = "k"
            cfg = make_cfg(ws)
            cfg["providers"] = [{"type": prov_type, "model": "m", "base_url": url,
                                 "api_key_env": "SHADOW_TEST_KEY"}]
            with contextlib.redirect_stderr(io.StringIO()):
                out = shadow.agent("what is the answer?", projects, cfg, verbose=False)
            self.assertEqual(out, "7")
            self.assertEqual(received[0][0], path_expected)
            everything_sent = json.dumps([r[2] for r in received])
            self.assertNotIn("hunter2", everything_sent)       # secret never left
            self.assertIn("the answer is 7", everything_sent)  # file content did
            return received

    def test_openai_compatible(self):
        r = self.run_agent("openai", "/chat/completions")
        self.assertEqual(r[0][1]["Authorization"], "Bearer k")

    def test_ollama(self):
        r = self.run_agent("ollama", "/api/chat")
        self.assertFalse(r[0][2]["stream"])

    def test_anthropic(self):
        r = self.run_agent("anthropic", "/v1/messages")
        self.assertTrue(r[0][2]["system"])
        self.assertTrue(all(m["role"] != "system" for m in r[0][2]["messages"]))


if __name__ == "__main__":
    unittest.main()
