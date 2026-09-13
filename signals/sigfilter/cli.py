"""Command line: login, channels, watch, poll, stats, test, tune."""

import argparse
import asyncio
import json
import sys
import time

from . import config, db, deliver, pipeline


def cmd_login(_args):
    """One-time interactive login; prints a session string for headless runs."""
    from telethon.sessions import StringSession

    from . import listener

    client = listener.build_client()
    with client:
        client.loop.run_until_complete(client.get_me())
        print("\nLogged in. Put this in your .env as TG_SESSION (keep it secret —")
        print("it is full access to your Telegram account):\n")
        print(StringSession.save(client.session))


def cmd_channels(_args):
    """List the chats you're in, with the ids to paste into config.yaml."""
    from . import listener

    client = listener.build_client()

    async def run():
        await client.start()
        print(f"{'id':>16}  {'type':<10} title")
        async for dialog in client.iter_dialogs():
            kind = "channel" if dialog.is_channel else ("group" if dialog.is_group else "user")
            if kind == "user":
                continue
            print(f"{dialog.id:>16}  {kind:<10} {dialog.name}")
        await client.disconnect()

    asyncio.run(run())


def cmd_watch(args):
    from . import listener

    cfg = config.load(args.config)
    asyncio.run(listener.watch(cfg))


def cmd_poll(args):
    from . import listener

    cfg = config.load(args.config)
    asyncio.run(listener.poll(cfg, limit=args.limit))


def cmd_stats(args):
    cfg = config.load(args.config)
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT channel_name, channel_id,
                      COUNT(*) AS seen,
                      SUM(verdict = 'ACCEPT') AS accepted,
                      SUM(outcome = 'WIN')    AS wins,
                      SUM(outcome = 'LOSS')   AS losses,
                      ROUND(AVG(score), 1)    AS avg_score
                 FROM signals GROUP BY channel_id ORDER BY wins DESC""",
        ).fetchall()
        if not rows:
            print("No signals recorded yet.")
            return
        print(f"{'channel':<28}{'seen':>6}{'sent':>6}{'W':>5}{'L':>5}{'avg':>7}{'trust':>8}")
        for row in rows:
            from .score import channel_trust
            wins, losses = int(row["wins"] or 0), int(row["losses"] or 0)
            weight = config.source_map(cfg).get(row["channel_id"], {}).get("weight", 1.0)
            trust = channel_trust(wins, losses, cfg["scoring"]["trust_prior_trades"], weight)
            print(f"{(row['channel_name'] or '?')[:27]:<28}{row['seen']:>6}"
                  f"{int(row['accepted'] or 0):>6}{wins:>5}{losses:>5}"
                  f"{row['avg_score'] or 0:>7}{trust:>8.2f}")

        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM signals WHERE outcome IS NULL AND asset_class = 'crypto'"
        ).fetchone()["n"]
        print(f"\n{pending} crypto signal(s) still awaiting an outcome.")


def cmd_recent(args):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (args.limit,)
        ).fetchall()
    for row in rows:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(row["ts"]))
        flag = "✅" if row["verdict"] == "ACCEPT" else "  "
        reasons = json.loads(row["reasons"] or "[]")
        detail = "" if row["verdict"] == "ACCEPT" else " — " + "; ".join(reasons[:2])
        print(f"{flag} {stamp} {row['symbol'] or '?':<10}{row['side'] or '?':<5}"
              f"score {row['score']:<6}{(row['channel_name'] or '?')[:20]:<22}"
              f"{row['outcome'] or ''}{detail}")


def cmd_test(args):
    """Score a message from stdin or --text without touching Telegram.

    The honest way to tune min_score: paste real posts from your channels and
    watch what the gate does with them.
    """
    cfg = config.load(args.config)
    text = args.text or sys.stdin.read()
    with db.connect(args.db) as conn:
        result = pipeline.process(
            conn, cfg, channel_id=args.channel or -1, msg_id=int(time.time() * 1000) % 10**9,
            text=text, ts=int(time.time()),
        )
    if result["status"] != "scored":
        print(f"Not forwarded: {result['status']}")
        return
    print(json.dumps({
        "verdict": result["verdict"], "score": result["score"],
        "signal": result["signal"].as_dict(), "reasons": result["reasons"],
        "breakdown": result["breakdown"],
    }, indent=2, ensure_ascii=False))
    if result["verdict"] == "ACCEPT":
        print("\n--- would send ---\n" + deliver.format_signal(result))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sigfilter", description=__doc__)
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="one-time Telegram login, prints a session string")
    sub.add_parser("channels", help="list your chats and their ids")
    sub.add_parser("watch", help="run forever, forward signals as they arrive")

    poll_parser = sub.add_parser("poll", help="process new messages once, then exit (for cron)")
    poll_parser.add_argument("--limit", type=int, default=40, help="max messages per channel")

    sub.add_parser("stats", help="per-channel hit rate and trust")

    recent_parser = sub.add_parser("recent", help="last scored signals and why they passed or failed")
    recent_parser.add_argument("--limit", type=int, default=20)

    test_parser = sub.add_parser("test", help="score a pasted message without Telegram")
    test_parser.add_argument("--text")
    test_parser.add_argument("--channel", type=int)
    test_parser.add_argument("--db", help="use a scratch database")

    args = parser.parse_args(argv)
    handlers = {
        "login": cmd_login, "channels": cmd_channels, "watch": cmd_watch,
        "poll": cmd_poll, "stats": cmd_stats, "recent": cmd_recent, "test": cmd_test,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
