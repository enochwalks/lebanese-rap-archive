#!/usr/bin/env python3
"""
shadow.py - keeps every project running when Claude is not around.

It knows every project from projects.toml: where the code lives, how to set
it up, how to run each task, how the projects connect, and what can go
wrong. It works on three levels, so losing any one of them loses nothing:

  1. No AI at all      list / show / doctor / run / status / logs / order /
                       fetch / scan / brief  - plain deterministic commands.
  2. Any AI you have   ask / chat - an agent loop that reads the code and runs
                       registered tasks, using whatever model is reachable:
                       a local Ollama model (offline, free), any
                       OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter,
                       DeepSeek, Mistral, LM Studio...), or Claude if it's back.
  3. No AI reachable   ask still answers, by searching the registry, runbooks
                       and memory and printing the matching instructions.

Standard library only (Python 3.11+). Runs on Windows, macOS, Linux, Termux.

    python shadow/shadow.py list
    python shadow/shadow.py doctor
    python shadow/shadow.py run rap-archive status
    python shadow/shadow.py ask "the daily video didn't upload, what do I do?"
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("shadow needs Python 3.11 or newer (for tomllib).")

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "projects.toml"
CONFIG = HERE / "config.toml"
RUNBOOKS = HERE / "runbooks"
MEMORY = HERE / "memory.md"
JOURNAL = HERE / "journal.log"
IS_WINDOWS = os.name == "nt"

# Files the shadow never reads or sends to a model, whatever it is asked.
SECRET_PATTERNS = [
    r"(^|/)\.env(\..*)?$", r"\.pickle$", r"client_secret", r"(^|/)token",
    r"\.session$", r"\.pem$", r"\.key$", r"id_rsa", r"credentials",
    r"(^|/)config\.toml$",
]
SECRET_OK = [r"\.env\.example$"]

MAX_READ = 20_000
MAX_STEPS = 15


# --------------------------------------------------------------------------
# Registry and config
# --------------------------------------------------------------------------

def load_registry(path=REGISTRY):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    projects = data.get("projects", {})
    for pid, p in projects.items():
        p.setdefault("tasks", {})
        p.setdefault("needs", {})
        p.setdefault("depends_on", [])
        p.setdefault("feeds", [])
        p.setdefault("logs", [])
        p["id"] = pid
    return projects


def load_config(path=CONFIG):
    cfg = {}
    if Path(path).exists():
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    cfg.setdefault("workspace", os.environ.get("SHADOW_WORKSPACE", "~/shadow-projects"))
    cfg.setdefault("providers", [])
    if os.environ.get("SHADOW_PROVIDER"):
        # One-off override: SHADOW_PROVIDER=ollama SHADOW_MODEL=llama3.1
        cfg["providers"] = [{
            "type": os.environ["SHADOW_PROVIDER"],
            "model": os.environ.get("SHADOW_MODEL", ""),
            "base_url": os.environ.get("SHADOW_BASE_URL", ""),
            "api_key_env": os.environ.get("SHADOW_API_KEY_ENV", ""),
        }]
    if not cfg["providers"]:
        cfg["providers"] = [{"type": "ollama", "model": "llama3.1"}]
    return cfg


def project_dir(p, cfg):
    """Folder the project's commands run in."""
    override = cfg.get("paths", {}).get(p["id"])
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    root = checkout_dir(p, cfg)
    return root / p["subdir"] if p.get("subdir") else root


_HOME_REPO = None


def home_repo():
    """(remote url, branch) of the checkout the shadow itself lives in."""
    global _HOME_REPO
    if _HOME_REPO is None:
        def g(*a):
            try:
                return subprocess.run(["git", *a], cwd=HERE, capture_output=True,
                                      text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return ""
        url = re.sub(r"(\.git)?/?$", "", g("remote", "get-url", "origin")).lower()
        _HOME_REPO = (url, g("rev-parse", "--abbrev-ref", "HEAD"))
    return _HOME_REPO


def checkout_dir(p, cfg):
    override = cfg.get("checkouts", {}).get(p.get("dir", ""))
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    # Running from a clone of this very project/branch? Use it, no fetch needed.
    url, branch = home_repo()
    if url and url == re.sub(r"(\.git)?/?$", "", p.get("repo", "")).lower() \
            and branch == p.get("branch"):
        return HERE.parent
    ws = Path(os.path.expandvars(cfg["workspace"])).expanduser()
    return ws / p.get("dir", p["id"])


def get_project(projects, pid):
    if pid in projects:
        return projects[pid]
    matches = [k for k in projects if k.startswith(pid)]
    if len(matches) == 1:
        return projects[matches[0]]
    sys.exit(f"Unknown project '{pid}'. Known: {', '.join(projects)}")


def journal(line):
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Dependency order
# --------------------------------------------------------------------------

def dependency_order(projects):
    """Topological order: a project comes after everything it depends on.
    `feeds = [x]` is the same as x having `depends_on = [this]`."""
    deps = {pid: set(p["depends_on"]) for pid, p in projects.items()}
    for pid, p in projects.items():
        for target in p["feeds"]:
            if target in deps:
                deps[target].add(pid)
    unknown = sorted({d for ds in deps.values() for d in ds if d not in deps})
    if unknown:
        raise ValueError(f"unknown project(s) referenced: {', '.join(unknown)}")
    order, state = [], {}

    def visit(n, trail):
        if state.get(n) == "done":
            return
        if state.get(n) == "busy":
            raise ValueError("dependency cycle: " + " -> ".join(trail + [n]))
        state[n] = "busy"
        for d in sorted(deps[n]):
            visit(d, trail + [n])
        state[n] = "done"
        order.append(n)

    for n in sorted(deps):
        visit(n, [])
    return order, deps


# --------------------------------------------------------------------------
# Running tasks
# --------------------------------------------------------------------------

def task_command(task):
    if IS_WINDOWS and task.get("cmd_windows"):
        return task["cmd_windows"]
    cmd = task.get("cmd")
    # Registry says "python"; on machines where that's "python3" or "py", use
    # the interpreter running the shadow instead.
    if cmd and cmd.startswith("python ") and not shutil.which("python"):
        cmd = f'"{sys.executable}" ' + cmd[len("python "):]
    return cmd


def confirm(prompt, assume_yes=False):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{prompt} -> refused (no terminal to confirm in).")
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def run_task(p, task_name, cfg, assume_yes=False, capture=False, extra_args=""):
    tasks = p["tasks"]
    if task_name not in tasks:
        return 2, f"'{p['id']}' has no task '{task_name}'. Tasks: {', '.join(tasks)}"
    task = tasks[task_name]
    if task.get("manual"):
        return 0, f"Manual step (do this yourself): {task['manual']}"
    cmd = task_command(task)
    if extra_args:
        cmd = f"{cmd} {extra_args}"
    cwd = project_dir(p, cfg)
    if not cwd.exists():
        return 2, f"{cwd} does not exist. Run: python shadow/shadow.py fetch {p['id']}"
    if task.get("danger") and not confirm(
            f"'{p['id']}:{task_name}' touches the outside world ({task.get('desc', cmd)}). Run it?",
            assume_yes):
        return 1, "Not run."
    env = dict(os.environ, **{k: str(v) for k, v in task.get("env", {}).items()})
    journal(f"run {p['id']}:{task_name} -> {cmd} (in {cwd})")
    print(f"$ {cmd}   (in {cwd})", file=sys.stderr)
    if capture:
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, env=env, text=True,
                               capture_output=True, timeout=cfg.get("task_timeout", 900))
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        out = (r.stdout + ("\n" + r.stderr if r.stderr else ""))[-6000:]
        journal(f"  exit {r.returncode}")
        return r.returncode, out
    r = subprocess.run(cmd, shell=True, cwd=cwd, env=env)
    journal(f"  exit {r.returncode}")
    return r.returncode, ""


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

def doctor_project(p, cfg):
    """Returns a list of (ok, message)."""
    results = []
    d = project_dir(p, cfg)
    results.append((d.exists(), f"folder {d}"))
    for b in p["needs"].get("bins", []):
        found = shutil.which(b) or (b == "python" and sys.executable)
        results.append((bool(found), f"program '{b}' on PATH"))
    for f in p["needs"].get("files", []):
        results.append(((d / f).exists(), f"file {f}"))
    for e in p["needs"].get("env", []):
        results.append((bool(os.environ.get(e)), f"env var {e}"))
    return results


def git_state(path):
    if not (path / ".git").exists():
        return "not a git checkout"
    def g(*a):
        r = subprocess.run(["git", *a], cwd=path, capture_output=True, text=True)
        return r.stdout.strip()
    branch = g("rev-parse", "--abbrev-ref", "HEAD")
    dirty = "dirty" if g("status", "--porcelain") else "clean"
    last = g("log", "-1", "--format=%cr: %s")
    return f"{branch}, {dirty}, last commit {last}"


def tail(path, n=20):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError as e:
        return f"(cannot read {path}: {e.strerror})"


# --------------------------------------------------------------------------
# Scan: discover a project you haven't registered yet
# --------------------------------------------------------------------------

def scan(path):
    path = Path(path).resolve()
    pid = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "project"
    files = {f.relative_to(path).as_posix() for f in path.rglob("*")
             if f.is_file() and ".git" not in f.parts and "node_modules" not in f.parts
             and ".venv" not in f.parts and "venv" not in f.parts}
    bins, tasks, found = set(), {}, []

    if "requirements.txt" in files:
        bins.add("python")
        tasks["install"] = ("python -m pip install -r requirements.txt", "Install dependencies")
    if "pyproject.toml" in files:
        bins.add("python")
        tasks.setdefault("install", ("python -m pip install -e .", "Install the package"))
    if "package.json" in files:
        bins.add("node")
        try:
            pkg = json.loads((path / "package.json").read_text(encoding="utf-8"))
            tasks["install"] = ("npm install", "Install dependencies")
            for s in pkg.get("scripts", {}):
                tasks[s] = (f"npm run {s}", f"npm script '{s}'")
        except (ValueError, OSError):
            pass
    for entry in ("main.py", "app.py", "run.py", "bot.py", "manage.py", "server.py"):
        if entry in files:
            tasks[entry[:-3]] = (f"python {entry}", f"Entry point {entry}")
    if any(f.startswith("tests/") for f in files):
        tasks["tests"] = ("python -m pytest -q", "Run tests")
    if "Dockerfile" in files:
        bins.add("docker")
        found.append("Dockerfile present")
    if "Makefile" in files:
        found.append("Makefile present - check its targets")
    secrets = sorted(f for f in files if is_secret(f))
    if ".env.example" in files:
        found.append(".env.example present - copy to .env and fill it in")
    workflows = sorted(f for f in files if f.startswith(".github/workflows/"))
    for w in workflows:
        text = (path / w).read_text(encoding="utf-8", errors="replace")
        crons = re.findall(r"cron:\s*['\"]([^'\"]+)['\"]", text)
        found.append(f"workflow {w}" + (f" (cron {', '.join(crons)})" if crons else ""))
    if any("ffmpeg" in (path / f).read_text(encoding="utf-8", errors="ignore")
           for f in files if f.endswith(".py") and (path / f).stat().st_size < 400_000):
        bins.update(["ffmpeg", "ffprobe"])
    for f in files:
        if f.endswith(".py") and (path / f).stat().st_size < 400_000:
            if re.search(r"anthropic|claude", (path / f).read_text(encoding="utf-8", errors="ignore"), re.I):
                found.append(f"calls Claude: {f} - add a claude_touchpoints entry with its fallback")

    lines = [f"[projects.{pid}]",
             f'name = "{path.name}"',
             'summary = """\nTODO: what it does, in two or three plain sentences.\n"""',
             'repo = ""', 'branch = "main"', f'dir = "{path.name}"', 'subdir = ""',
             'runs_on = "TODO"', 'schedule = "TODO"',
             "depends_on = []", "feeds = []", "logs = []",
             "secrets = [" + ", ".join(json.dumps(s) for s in secrets) + "]",
             'claude_touchpoints = []', 'notes = """', *[f"- {x}" for x in found], '"""', "",
             f"[projects.{pid}.needs]",
             "bins = [" + ", ".join(json.dumps(b) for b in sorted(bins)) + "]",
             "files = []", "env = []", ""]
    for name, (cmd, desc) in tasks.items():
        lines += [f"[projects.{pid}.tasks.{name}]", f"cmd = {json.dumps(cmd)}",
                  f"desc = {json.dumps(desc)}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Knowledge pack (what the brain reads, and what you paste into any chatbot)
# --------------------------------------------------------------------------

def is_secret(relpath):
    rp = relpath.replace("\\", "/").lower()
    if any(re.search(p, rp) for p in SECRET_OK):
        return False
    return any(re.search(p, rp) for p in SECRET_PATTERNS)


def pretty(path):
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home) else str(path)


def project_brief(p, cfg):
    out = [f"## {p['id']} - {p.get('name', '')}", "", p.get("summary", "").strip(), "",
           f"- Code: {p.get('repo', '?')} (branch {p.get('branch', '?')})"
           + (f", folder {p['subdir']}/" if p.get("subdir") else ""),
           f"- Local folder: {pretty(project_dir(p, cfg))}",
           f"- Runs on: {p.get('runs_on', '?')}",
           f"- Schedule: {p.get('schedule', '?')}"]
    if p["depends_on"]:
        out.append(f"- Depends on: {', '.join(p['depends_on'])}")
    if p["feeds"]:
        out.append(f"- Feeds: {', '.join(p['feeds'])}")
    for label, key in (("Secrets (names only)", "secrets"), ("Where it uses Claude", "claude_touchpoints")):
        if p.get(key):
            out.append(f"- {label}:")
            out += [f"    - {s}" for s in p[key]]
    n = p["needs"]
    out.append(f"- Needs: programs {n.get('bins', [])}, files {n.get('files', [])}, env {n.get('env', [])}")
    out += ["", "Tasks (python shadow/shadow.py run " + p["id"] + " <task>):"]
    for t, spec in p["tasks"].items():
        how = spec.get("manual") and f"MANUAL: {spec['manual']}" or task_command(spec)
        if spec.get("env"):
            how = " ".join(f"{k}={v}" for k, v in spec["env"].items()) + " " + how
        out.append(f"- {t}{' [DANGER: asks first]' if spec.get('danger') else ''}: {spec.get('desc', '')}  ->  `{how}`")
    if p.get("notes"):
        out += ["", "Notes:", p["notes"].strip()]
    rb = RUNBOOKS / f"{p['id']}.md"
    if rb.exists():
        out += ["", "Runbook:", rb.read_text(encoding="utf-8").strip()]
    return "\n".join(out)


def knowledge_pack(projects, cfg):
    order, _ = dependency_order(projects)
    parts = ["# Shadow knowledge pack",
             f"Generated {datetime.now():%Y-%m-%d %H:%M}. Projects in dependency order: "
             + ", ".join(order), ""]
    parts += [project_brief(projects[pid], cfg) + "\n" for pid in order]
    if MEMORY.exists():
        parts += ["# Memory (things learned while running these projects)",
                  MEMORY.read_text(encoding="utf-8").strip()]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Brain: any model, through a plain JSON protocol (no vendor tool-calling)
# --------------------------------------------------------------------------

AGENT_RULES = """You are the Shadow: the operator who keeps the owner's projects running
when their usual assistant (Claude) is unavailable. You know the projects only
from the knowledge pack below and from files you read. Never guess a command:
use the registered tasks, or read the code first.

Reply with EXACTLY ONE JSON object and nothing else. Actions:
  {"action":"read_file","project":"<id>","path":"<relative path>"}
  {"action":"list_dir","project":"<id>","path":"<relative path or .>"}
  {"action":"run_task","project":"<id>","task":"<task name>"}
  {"action":"shell","project":"<id>","cmd":"<command>","why":"<reason>"}
  {"action":"remember","note":"<a durable fact worth knowing next time>"}
  {"action":"answer","text":"<final answer to the owner, plain words>"}

Rules:
- Prefer run_task over shell. shell always needs the owner's approval.
- Tasks marked DANGER (uploads, real money, messages) need the owner's approval;
  say what will happen before you ask.
- Secret files (.env, tokens, client_secrets) are blocked. Ask the owner instead.
- Never weaken a safety rule in the notes (permission gate, one host only, private uploads).
- When you fix or learn something that will matter again, use remember.
- Finish with answer: what you found, what you did, what the owner must do next.
"""


BRIEF_INTRO = """# How to use this file
Paste everything below into any AI chat (ChatGPT, Gemini, Claude, a local
model...) and then ask your question. To the AI reading this: you are helping
the owner run the projects described here while their usual assistant is away.
Answer only from this document; give the exact command from the Tasks lists
(run them from the repository root as `python shadow/shadow.py run <id> <task>`);
never suggest weakening a rule in the Notes; say plainly when the answer isn't here.
"""


class BrainError(Exception):
    pass


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise BrainError(f"HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BrainError(str(getattr(e, "reason", e))) from e


def call_provider(prov, messages, timeout=180):
    kind = prov.get("type", "")
    model = prov.get("model", "")
    key = os.environ.get(prov.get("api_key_env") or "", "")
    if kind == "ollama":
        base = prov.get("base_url") or "http://localhost:11434"
        r = _post(f"{base.rstrip('/')}/api/chat",
                  {"model": model, "messages": messages, "stream": False,
                   "options": {"temperature": 0.2}}, {}, timeout)
        return r["message"]["content"]
    if kind == "openai":
        base = prov.get("base_url") or "https://api.openai.com/v1"
        if not key and "localhost" not in base and "127.0.0.1" not in base:
            raise BrainError(f"env var {prov.get('api_key_env')!r} is not set")
        r = _post(f"{base.rstrip('/')}/chat/completions",
                  {"model": model, "messages": messages, "temperature": 0.2},
                  {"Authorization": f"Bearer {key}"} if key else {}, timeout)
        return r["choices"][0]["message"]["content"]
    if kind == "anthropic":
        if not key:
            raise BrainError(f"env var {prov.get('api_key_env')!r} is not set")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        r = _post((prov.get("base_url") or "https://api.anthropic.com") + "/v1/messages",
                  {"model": model, "max_tokens": 4096, "system": system, "messages": rest},
                  {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        return "".join(b.get("text", "") for b in r.get("content", []))
    raise BrainError(f"unknown provider type '{kind}' (use ollama, openai or anthropic)")


class Brain:
    """Tries each configured provider in order and sticks with the first that answers."""

    def __init__(self, providers):
        self.providers = providers
        self.active = None
        self.errors = []

    def __call__(self, messages):
        order = [self.active] if self.active else self.providers
        for prov in order:
            try:
                text = call_provider(prov, messages)
                self.active = prov
                return text
            except (BrainError, KeyError, IndexError, TypeError) as e:
                self.errors.append(f"{prov.get('type')}:{prov.get('model')} -> {e}")
                if self.active:
                    self.active = None
                    return self(messages)
        raise BrainError("no model reachable:\n  " + "\n  ".join(self.errors))


def extract_json(text):
    """First balanced {...} object in the text that parses as JSON."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def safe_path(p, cfg, rel):
    base = project_dir(p, cfg).resolve()
    target = (base / (rel or ".")).resolve()
    if target != base and base not in target.parents:
        raise PermissionError("path is outside the project folder")
    if is_secret(target.relative_to(base).as_posix()):
        raise PermissionError("that is a secret file; ask the owner instead")
    return target


def do_action(act, projects, cfg, assume_yes=False):
    kind = act.get("action")
    try:
        if kind == "remember":
            note = str(act.get("note", "")).strip()
            if note:
                with open(MEMORY, "a", encoding="utf-8") as f:
                    f.write(f"- ({datetime.now():%Y-%m-%d}) {note}\n")
            return "saved to memory"
        p = projects.get(act.get("project", ""))
        if p is None:
            return f"unknown project; known: {', '.join(projects)}"
        if kind == "read_file":
            t = safe_path(p, cfg, act.get("path", ""))
            if not t.is_file():
                return "no such file"
            return t.read_text(encoding="utf-8", errors="replace")[:MAX_READ]
        if kind == "list_dir":
            t = safe_path(p, cfg, act.get("path", "."))
            if not t.is_dir():
                return "no such folder"
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in t.iterdir())[:300])
        if kind == "run_task":
            code, out = run_task(p, act.get("task", ""), cfg, assume_yes=assume_yes, capture=True)
            return f"exit {code}\n{out}"
        if kind == "shell":
            cmd = str(act.get("cmd", ""))
            print(f"\nThe shadow wants to run in {p['id']}:\n    {cmd}\n  because: {act.get('why', '?')}")
            if not confirm("Allow?"):
                return "owner refused"
            journal(f"shell {p['id']} -> {cmd}")
            r = subprocess.run(cmd, shell=True, cwd=project_dir(p, cfg), text=True,
                               capture_output=True, timeout=cfg.get("task_timeout", 900))
            return f"exit {r.returncode}\n{(r.stdout + r.stderr)[-6000:]}"
    except (PermissionError, OSError, subprocess.TimeoutExpired) as e:
        return f"error: {e}"
    return f"unknown action '{kind}'"


def agent(question, projects, cfg, brain=None, history=None, assume_yes=False, verbose=True):
    brain = brain or Brain(cfg["providers"])
    messages = history if history is not None else [
        {"role": "system", "content": AGENT_RULES + "\n\n" + knowledge_pack(projects, cfg)}]
    messages.append({"role": "user", "content": question})
    for _ in range(MAX_STEPS):
        reply = brain(messages)
        messages.append({"role": "assistant", "content": reply})
        act = extract_json(reply)
        if act is None:
            return reply.strip()  # model answered in prose; take it
        if act.get("action") == "answer":
            return str(act.get("text", "")).strip()
        if verbose:
            shown = {k: v for k, v in act.items() if k != "action"}
            print(f"  · {act.get('action')} {json.dumps(shown, ensure_ascii=False)[:160]}", file=sys.stderr)
        result = do_action(act, projects, cfg, assume_yes=assume_yes)
        messages.append({"role": "user", "content": f"RESULT of {act.get('action')}:\n{result}"})
    return "Stopped after too many steps. Narrow the question, or run the tasks by hand."


def offline_answer(question, projects, cfg, limit=6):
    """No model reachable: rank paragraphs of the knowledge pack by word overlap."""
    words = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) > 2}
    chunks = []
    for pid, p in projects.items():
        for para in re.split(r"\n\s*\n", project_brief(p, cfg)):
            lines = para.strip().splitlines()
            # Tables and lists: each row/item is its own answer.
            if sum(l.lstrip().startswith(("|", "- ")) for l in lines) >= 3:
                chunks += [(pid, l.strip()) for l in lines
                           if l.strip() and not re.fullmatch(r"[|\-\s]+", l)]
            else:
                chunks.append((pid, para.strip()))
    if MEMORY.exists():
        chunks += [("memory", line) for line in MEMORY.read_text(encoding="utf-8").splitlines() if line.strip()]
    scored = []
    for pid, para in chunks:
        text = (pid + " " + para).lower()
        score = sum(text.count(w) for w in words)
        if score:
            scored.append((score, pid, para))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return "Nothing in the registry matches. Try: python shadow/shadow.py list"
    return "\n\n".join(f"[{pid}]\n{para}" for _, pid, para in scored[:limit])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="shadow", description="Run every project without Claude.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every project and its tasks")
    s = sub.add_parser("show", help="everything known about one project"); s.add_argument("project")
    s = sub.add_parser("doctor", help="check programs, files, env vars"); s.add_argument("project", nargs="?")
    s = sub.add_parser("run", help="run a registered task")
    s.add_argument("project"); s.add_argument("task"); s.add_argument("args", nargs=argparse.REMAINDER)
    s.add_argument("--yes", action="store_true", help="don't ask before DANGER tasks")
    sub.add_parser("status", help="git state, health and last log line of every project")
    s = sub.add_parser("logs", help="tail a project's logs"); s.add_argument("project"); s.add_argument("-n", type=int, default=30)
    sub.add_parser("order", help="dependency order and links between projects")
    s = sub.add_parser("fetch", help="clone/update a project's code"); s.add_argument("project", nargs="?")
    s = sub.add_parser("scan", help="print a registry block for a new project folder"); s.add_argument("path")
    s = sub.add_parser("brief", help="write the knowledge pack (paste it into any AI)")
    s.add_argument("-o", "--out", default=str(HERE / "SHADOW_BRIEF.md"))
    s = sub.add_parser("ask", help="ask the shadow a question; it may read code and run tasks")
    s.add_argument("question", nargs="+"); s.add_argument("--offline", action="store_true")
    s.add_argument("--yes", action="store_true", help="don't ask before DANGER tasks")
    sub.add_parser("chat", help="conversation with the shadow")
    sub.add_parser("brains", help="which models are reachable right now")
    a = ap.parse_args(argv)

    projects = load_registry()
    cfg = load_config()

    if a.cmd == "list":
        for pid in dependency_order(projects)[0]:
            p = projects[pid]
            print(f"{pid:14} {p.get('name', '')}\n{'':14} runs on: {p.get('runs_on', '?')}")
            print(f"{'':14} tasks: {', '.join(p['tasks'])}\n")
    elif a.cmd == "show":
        print(project_brief(get_project(projects, a.project), cfg))
    elif a.cmd == "doctor":
        targets = [get_project(projects, a.project)] if a.project else list(projects.values())
        bad = 0
        for p in targets:
            print(f"\n{p['id']}")
            for ok, msg in doctor_project(p, cfg):
                bad += not ok
                print(f"  {'ok ' if ok else 'MISSING'}  {msg}")
        print(f"\n{bad} problem(s)." if bad else "\nAll good.")
        return 1 if bad else 0
    elif a.cmd == "run":
        args = [x for x in a.args if x != "--yes"]
        quote = subprocess.list2cmdline if IS_WINDOWS else shlex.join
        code, out = run_task(get_project(projects, a.project), a.task, cfg,
                             assume_yes=a.yes or "--yes" in a.args,
                             extra_args=quote(args) if args else "")
        if out:
            print(out)
        return code
    elif a.cmd == "status":
        for pid in dependency_order(projects)[0]:
            p = projects[pid]
            d = project_dir(p, cfg)
            problems = [m for ok, m in doctor_project(p, cfg) if not ok]
            print(f"\n{pid}  [{'OK' if not problems else str(len(problems)) + ' missing'}]")
            print(f"  code:  {git_state(checkout_dir(p, cfg)) if d.exists() else 'not fetched'}")
            print(f"  runs:  {p.get('runs_on', '?')} | {p.get('schedule', '?')}")
            for lg in p["logs"]:
                last = tail(d / lg, 1).strip()
                print(f"  log:   {last[:150]}")
            for m in problems[:4]:
                print(f"  fix:   {m}")
    elif a.cmd == "logs":
        p = get_project(projects, a.project)
        if not p["logs"]:
            print(f"{p['id']} has no log files registered. Runs on: {p.get('runs_on')}")
        for lg in p["logs"]:
            print(f"== {lg}\n{tail(project_dir(p, cfg) / lg, a.n)}")
    elif a.cmd == "order":
        order, deps = dependency_order(projects)
        for i, pid in enumerate(order, 1):
            need = ", ".join(sorted(deps[pid])) or "nothing"
            print(f"{i}. {pid:14} needs: {need}")
    elif a.cmd == "fetch":
        targets = [get_project(projects, a.project)] if a.project else list(projects.values())
        done = set()
        for p in targets:
            dest = checkout_dir(p, cfg)
            if str(dest) in done:
                continue
            done.add(str(dest))
            if (dest / ".git").exists():
                print(f"updating {dest}")
                subprocess.run(["git", "pull", "--ff-only"], cwd=dest)
            elif p.get("repo"):
                print(f"cloning {p['repo']} ({p.get('branch')}) -> {dest}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "clone", "-b", p.get("branch", "main"), p["repo"], str(dest)])
            else:
                print(f"{p['id']}: no repo set; put the code in {dest}")
    elif a.cmd == "scan":
        print(scan(a.path))
    elif a.cmd == "brief":
        Path(a.out).write_text(BRIEF_INTRO + "\n" + knowledge_pack(projects, cfg),
                               encoding="utf-8")
        print(f"Wrote {a.out}. Paste it into any AI chat, then ask your question.")
    elif a.cmd == "brains":
        for prov in cfg["providers"]:
            try:
                call_provider(prov, [{"role": "user", "content": "Reply with the word ok."}], timeout=60)
                print(f"  ok       {prov.get('type')}:{prov.get('model')}")
            except BrainError as e:
                print(f"  DOWN     {prov.get('type')}:{prov.get('model')}  ({str(e)[:120]})")
    elif a.cmd == "ask":
        q = " ".join(a.question)
        if a.offline:
            print(offline_answer(q, projects, cfg))
            return 0
        try:
            print(agent(q, projects, cfg, assume_yes=a.yes))
        except BrainError as e:
            print(f"(No AI reachable - answering from the registry instead.)\n{e}\n", file=sys.stderr)
            print(offline_answer(q, projects, cfg))
    elif a.cmd == "chat":
        brain = Brain(cfg["providers"])
        history = [{"role": "system", "content": AGENT_RULES + "\n\n" + knowledge_pack(projects, cfg)}]
        print("Shadow ready. Ask anything about your projects. Empty line or 'exit' to quit.")
        while True:
            try:
                q = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q in ("", "exit", "quit"):
                break
            try:
                print("\nshadow> " + agent(q, projects, cfg, brain=brain, history=history))
            except BrainError as e:
                print(f"\n(No AI reachable: {str(e).splitlines()[0]})\n" + offline_answer(q, projects, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
