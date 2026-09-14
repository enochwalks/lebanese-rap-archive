"""A local web dashboard, served from this machine.

Deliberately local: the database sits on your PC, so a hosted page could not read
it without shipping your trading history off the machine first. Standard library
only - no web framework, no build step, nothing extra to install.
"""

import json
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import config, db, promo, score

HERE = Path(__file__).resolve().parent.parent
PAGE = HERE / "dashboard.html"


def _day_key(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")


def collect(cfg, now=None):
    """Everything the page needs, in one read."""
    now = now or int(time.time())
    day_start = now - (now % 86400)
    sources = config.source_map(cfg)

    with db.connect() as conn:
        heartbeat = int(db.get_state(conn, "heartbeat", 0) or 0)
        seen_today = db.get_counter(conn, f"seen:{_day_key(now)}")

        totals = conn.execute(
            """SELECT COUNT(*) AS parsed,
                      SUM(verdict = 'ACCEPT') AS accepted,
                      SUM(forwarded = 1)      AS forwarded
                 FROM signals WHERE ts >= ?""", (day_start,)).fetchone()

        latest = conn.execute(
            "SELECT * FROM signals WHERE verdict = 'ACCEPT' ORDER BY ts DESC LIMIT 1"
        ).fetchone()

        recent = conn.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT 25").fetchall()

        channels = conn.execute(
            """SELECT channel_id, channel_name,
                      COUNT(*) AS seen,
                      SUM(verdict = 'ACCEPT') AS accepted,
                      SUM(outcome = 'WIN')    AS wins,
                      SUM(outcome = 'LOSS')   AS losses,
                      AVG(score)              AS avg_score
                 FROM signals GROUP BY channel_id""").fetchall()

        channel_rows = []
        for row in channels:
            cid = row["channel_id"]
            wins, losses = int(row["wins"] or 0), int(row["losses"] or 0)
            promo_n = db.get_counter(conn, f"promo:{cid}")
            msgs_n = db.get_counter(conn, f"msgs:{cid}")
            multiplier = promo.penalty(promo_n, msgs_n)
            weight = sources.get(cid, {}).get("weight", 1.0)
            channel_rows.append({
                "name": row["channel_name"] or str(cid),
                "seen": row["seen"],
                "sent": int(row["accepted"] or 0),
                "wins": wins,
                "losses": losses,
                "avg_score": round(row["avg_score"] or 0, 1),
                "promo_pct": round(100.0 * promo_n / msgs_n, 1) if msgs_n else None,
                "messages": msgs_n,
                "trust": round(score.channel_trust(
                    wins, losses, cfg["scoring"]["trust_prior_trades"],
                    weight * multiplier), 2),
                "muted": weight == 0.0,
            })
        channel_rows.sort(key=lambda c: (-c["trust"], -c["seen"]))

    def signal_row(row):
        return {
            "ts": row["ts"],
            "symbol": row["symbol"],
            "side": row["side"],
            "score": row["score"],
            "verdict": row["verdict"],
            "channel": row["channel_name"],
            "reasons": json.loads(row["reasons"] or "[]"),
            "outcome": row["outcome"],
            "entry": row["entry"], "sl": row["sl"], "tp1": row["tp1"],
            "rr": round(row["rr"], 2) if row["rr"] else None,
        }

    latest_signal = None
    if latest:
        latest_signal = signal_row(latest)
        latest_signal["breakdown"] = _breakdown_rows(latest)

    return {
        "now": now,
        "heartbeat": heartbeat,
        "alive": bool(heartbeat and now - heartbeat < 180),
        "channels_watched": len(sources),
        "today": {
            "messages": seen_today,
            "parsed": totals["parsed"] or 0,
            "accepted": int(totals["accepted"] or 0),
            "forwarded": int(totals["forwarded"] or 0),
        },
        "gate": cfg["gate"],
        "latest": latest_signal,
        "recent": [signal_row(r) for r in recent],
        "channels": channel_rows,
    }


def _breakdown_rows(row):
    """The component breakdown exactly as it was scored, not re-derived."""
    try:
        stored = json.loads(row["breakdown"] or "{}")
    except (TypeError, ValueError):
        return []
    total_weight = sum(c.get("weight", 0) for c in stored.values()) or 1
    return [
        {
            "name": name.replace("_", " "),
            "points": component.get("points", 0),
            "max_points": round(component.get("weight", 0) / total_weight * 100, 1),
            "value": component.get("value"),
        }
        for name, component in stored.items()
    ]


class Handler(BaseHTTPRequestHandler):
    cfg = None

    def do_GET(self):
        if self.path.startswith("/api/state"):
            payload = json.dumps(collect(self.cfg)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path in ("/", "/index.html", "/dashboard.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *_args):
        pass          # the console belongs to the listener, not to page requests


def serve(cfg, port=8765, open_browser=True):
    Handler.cfg = cfg
    # Loopback only: this page exposes your trading history and channel list.
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard running at {url}")
    print("It reads the same database the listener writes. Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
