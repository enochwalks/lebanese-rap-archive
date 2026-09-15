"""Command line: login, channels, watch, poll, stats, test, tune."""

import argparse
import asyncio
import json
import sys
import time

from . import config, db, deliver, pipeline


def cmd_login(args):
    """One-time interactive login.

    The session string is a bearer credential for the whole Telegram account, so
    it is never printed unless explicitly asked for: terminals get screenshotted,
    pasted into chats and scrolled past by people standing behind you.
    """
    from telethon.sessions import StringSession

    from . import listener

    client = listener.build_client()
    with client:
        me = client.loop.run_until_complete(client.get_me())
        name = getattr(me, "first_name", None) or getattr(me, "username", "?")
        print(f"\nLogged in as {name}. The session is saved locally, so you will")
        print("not be asked for the code again on this machine.")
        if not getattr(args, "show_session", False):
            print("\nRunning this on GitHub Actions instead? Re-run with --show-session")
            print("to print the session string. Do not do that on a shared screen.")
            return
        print("\nSECRET — anyone holding this line can act as you on Telegram.")
        print("Put it in .env as TG_SESSION and never paste it anywhere else:\n")
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


def cmd_pick(args):
    """List your chats, let you choose, write config.yaml for you."""
    from . import listener, picker

    client = listener.build_client()
    cfg = config.load(args.config)
    existing = {int(src["id"]): float(src.get("weight", 1.0))
                for src in (cfg.get("sources") or [])}

    async def gather():
        await client.start()
        found = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel or dialog.is_group:
                found.append({"id": dialog.id, "name": dialog.name or str(dialog.id)})
        await client.disconnect()
        return found

    chats = asyncio.run(gather())
    if not chats:
        print("No channels or groups found on this account.")
        return

    # Suggested ones first, so the list opens on what you are probably after.
    chats.sort(key=lambda c: (not picker.looks_like_signals(c["name"]), c["name"].lower()))

    print("\nChannels and groups you are in:\n")
    for i, chat in enumerate(chats, 1):
        marks = []
        if chat["id"] in existing:
            marks.append("already added")
        if picker.looks_like_signals(chat["name"]):
            marks.append("looks like signals")
        note = ("   <- " + ", ".join(marks)) if marks else ""
        print(f"  {i:>3}. {chat['name'][:52]:<54}{note}")

    print("\nType the numbers of the channels you want filtered.")
    print("Examples:  1,4,9    or   1-6,12   or   all")
    answer = input("Your choice: ")

    picked = picker.parse_selection(answer, len(chats))
    if not picked:
        print("Nothing selected, config.yaml left unchanged.")
        return

    entries = [{"id": chats[i]["id"], "name": chats[i]["name"],
                "weight": existing.get(chats[i]["id"], 1.0)} for i in picked]
    path, backup = picker.write_sources(entries)

    print(f"\nWrote {len(entries)} channel(s) to {path}")
    print(f"(previous version kept as {backup.name})\n")
    for entry in entries:
        muted = "  [muted, weight 0.0]" if entry["weight"] == 0.0 else ""
        print(f"  {entry['name'][:52]}{muted}")
    print("\nRestart the agent for this to take effect:")
    print("  - using the Desktop shortcut: close the agent window and open it again")
    print("  - using the background task:  Restart-ScheduledTask -TaskName sigfilter-watch")


def cmd_watch(args):
    from . import listener

    cfg = config.load(args.config)
    asyncio.run(listener.watch(cfg))


def cmd_poll(args):
    from . import listener

    cfg = config.load(args.config)
    asyncio.run(listener.poll(cfg, limit=args.limit))


def cmd_grade(args):
    """Grade every past signal we have a price source for, and show the breakdown."""
    from . import outcomes

    cfg = config.load(args.config)
    if args.horizon:
        cfg["outcomes"]["horizon_hours"] = args.horizon
    horizon = cfg["outcomes"]["horizon_hours"]

    if args.regrade:
        with db.connect() as conn:
            n = db.reset_soft_outcomes(conn)
            conn.commit()
        print(f"Reset {n} previously expired/no-data signal(s) to try again.")

    print(f"Grading against real prices, {horizon}h window (crypto, gold, forex)...\n")
    counts = outcomes.grade_pending(cfg)
    total = sum(counts.values())
    print(f"Checked {total} signal(s):")
    print(f"  WIN                             {counts.get('WIN', 0)}")
    print(f"  LOSS                            {counts.get('LOSS', 0)}")
    print(f"  no hit within {horizon}h (expired)     {counts.get('EXPIRED', 0)}")
    print(f"  no price data (symbol/source)   {counts.get('NODATA', 0)}")
    print(f"  still open                      {counts.get('PENDING', 0)}")
    if counts.get("EXPIRED", 0) > counts.get("WIN", 0) + counts.get("LOSS", 0):
        print("\nLots expired unresolved. Gold/forex signals often take days -")
        print(f"try a longer window:  .\\run.ps1 grade --regrade --horizon 120")
    if counts.get("NODATA", 0) > 0:
        print("\nSome had no price data. Check one:  .\\run.ps1 probe XAUUSD")
    print("\nThen:  .\\run.ps1 stats")


def cmd_clean(args):
    """Remove stored signals whose symbol no longer parses as a real instrument -
    junk left by an earlier, looser parser (e.g. FUTURESUSDT from the word
    'futures')."""
    from . import symbols

    def looks_real(sym):
        if not sym:
            return False
        if symbols.asset_class(sym) in ("metal", "index", "forex"):
            return True
        base = sym
        for quote in ("USDT", "USDC", "BUSD", "USD"):
            if sym.endswith(quote):
                base = sym[: -len(quote)]
                break
        return base in symbols.KNOWN_CRYPTO or base in symbols._ALIASES

    junk = []
    with db.connect() as conn:
        rows = conn.execute("SELECT id, symbol FROM signals").fetchall()
        for row in rows:
            if not looks_real(row["symbol"]):
                junk.append((row["id"], row["symbol"]))
        if junk:
            db.delete_signals(conn, [i for i, _ in junk])
            conn.commit()
    if not junk:
        print("No junk signals found - database is clean.")
        return
    from collections import Counter
    counts = Counter(sym for _, sym in junk)
    print(f"Removed {len(junk)} mis-parsed signal(s):")
    for sym, n in counts.most_common(15):
        print(f"  {sym or '(empty)'}: {n}")


def cmd_set(args):
    """Change one gate setting without hand-editing YAML, e.g. set min_score 55."""
    from . import picker

    try:
        path, number = picker.set_gate_value(args.key, args.value, args.config)
    except KeyError:
        print(f"Unknown setting '{args.key}'. You can set: "
              + ", ".join(picker.GATE_KEYS))
        return
    except ValueError:
        print(f"'{args.value}' is not a number.")
        return
    print(f"Set {args.key} = {number} in {path.name}")
    print("Restart the agent for it to take effect.")


def cmd_probe(args):
    """Fetch prices for one symbol so you can see the source is reachable."""
    import time as _time

    from . import outcomes, symbols

    from . import mt5source

    sym = symbols.canonical(args.symbol) or args.symbol.upper()
    asset_class = symbols.asset_class(sym)
    now = int(_time.time())
    candles, source = outcomes.candles_with_source(sym, asset_class, now - 3 * 86400, now)
    print(f"{sym}  ({asset_class})")
    print(f"  MetaTrader 5 available: {'yes' if mt5source.available() else 'no'}")
    if candles:
        print(f"  OK via {source} - {len(candles)} five-minute candles")
        print(f"  first {candles[0]}  last {candles[-1]}")
    else:
        print("  NOTHING came back from any source.")
        if not mt5source.available():
            print("  MT5 is not connected. Open the MetaTrader 5 terminal and log in,")
            print("  then:  .\\run.ps1 grade --regrade")


def cmd_backfill(args):
    from . import listener

    cfg = config.load(args.config)
    print(f"Reading up to {args.limit} past messages per channel. This is a "
          "one-off; nothing is forwarded.\n")
    asyncio.run(listener.backfill(cfg, limit=args.limit))


def cmd_dashboard(args):
    from . import dashboard

    cfg = config.load(args.config)
    dashboard.serve(cfg, port=args.port, open_browser=not args.no_browser)


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
        from . import promo
        from .score import channel_trust
        print(f"{'channel':<26}{'seen':>6}{'sent':>6}{'W':>5}{'L':>5}{'avg':>7}{'promo':>7}{'trust':>7}")
        for row in rows:
            wins, losses = int(row["wins"] or 0), int(row["losses"] or 0)
            weight = config.source_map(cfg).get(row["channel_id"], {}).get("weight", 1.0)
            promo_n = db.get_counter(conn, f"promo:{row['channel_id']}")
            msgs_n = db.get_counter(conn, f"msgs:{row['channel_id']}")
            multiplier = promo.penalty(promo_n, msgs_n)
            trust = channel_trust(wins, losses, cfg["scoring"]["trust_prior_trades"],
                                  weight * multiplier)
            share = f"{(100.0 * promo_n / msgs_n):.0f}%" if msgs_n else "-"
            print(f"{(row['channel_name'] or '?')[:25]:<26}{row['seen']:>6}"
                  f"{int(row['accepted'] or 0):>6}{wins:>5}{losses:>5}"
                  f"{row['avg_score'] or 0:>7}{share:>7}{trust:>7.2f}")
        print("\npromo = share of that channel's posts that are deposit/recruitment pitches.")
        print("Anything above ~30% is a funnel with signals attached, not a signal channel.")

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


def _use_utf8_console():
    """Windows consoles default to a legacy codepage that cannot print a channel
    named in Arabic or carrying an emoji. Printing one would otherwise abort the
    listener mid-run."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _use_utf8_console()
    parser = argparse.ArgumentParser(prog="sigfilter", description=__doc__)
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    login_parser = sub.add_parser("login", help="one-time Telegram login")
    login_parser.add_argument(
        "--show-session", action="store_true",
        help="also print the session string, needed only for headless/CI runs",
    )
    sub.add_parser("channels", help="list your chats and their ids")
    sub.add_parser("pick", help="choose which channels to filter and write config.yaml")
    sub.add_parser("watch", help="run forever, forward signals as they arrive")

    poll_parser = sub.add_parser("poll", help="process new messages once, then exit (for cron)")
    poll_parser.add_argument("--limit", type=int, default=40, help="max messages per channel")

    backfill_parser = sub.add_parser(
        "backfill", help="score past history to bootstrap channel trust (nothing forwarded)")
    backfill_parser.add_argument("--limit", type=int, default=300,
                                 help="max past messages per channel")

    grade_parser = sub.add_parser("grade", help="grade stored signals against real prices")
    grade_parser.add_argument("--horizon", type=int,
                              help="hours to allow a signal to hit target/stop")
    grade_parser.add_argument("--regrade", action="store_true",
                              help="retry signals that expired or had no data")

    probe_parser = sub.add_parser("probe", help="test the price feed for one symbol")
    probe_parser.add_argument("symbol", help="e.g. XAUUSD, BTCUSDT, EURUSD")

    sub.add_parser("clean", help="remove mis-parsed junk symbols from the database")

    set_parser = sub.add_parser("set", help="change a gate setting, e.g. set min_score 55")
    set_parser.add_argument("key", help="min_score, min_risk_reward, max_leverage, "
                            "max_age_minutes or daily_cap")
    set_parser.add_argument("value")

    sub.add_parser("stats", help="per-channel hit rate and trust")

    dash_parser = sub.add_parser("dashboard", help="open the live dashboard in your browser")
    dash_parser.add_argument("--port", type=int, default=8765)
    dash_parser.add_argument("--no-browser", action="store_true")

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
        "dashboard": cmd_dashboard, "pick": cmd_pick, "backfill": cmd_backfill,
        "grade": cmd_grade, "probe": cmd_probe, "set": cmd_set, "clean": cmd_clean,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
