"""Telegram ingestion, in two modes.

watch — long-running client, reacts the instant a message lands (lowest latency).
poll  — one shot over recent history, for a cron/GitHub Actions schedule.

Both read channels through your own account, so private/VIP channels you are
already a member of work without any bot being added to them.
"""

import asyncio
import os
import time

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from . import config, db, deliver, pipeline, singleton


def build_client():
    config.load_env()
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TG_API_ID and TG_API_HASH must be set (see .env.example)")
    session_str = os.environ.get("TG_SESSION")
    if session_str:
        session = StringSession(session_str)          # headless / CI
    else:
        # A file-backed session, so logging in once is actually once. An empty
        # StringSession lives in memory only and would re-prompt for the phone
        # code on every command, which Telegram rate-limits.
        session = str(config.ROOT / "sigfilter.session")
    return TelegramClient(session, int(api_id), api_hash)


async def _handle(client, conn, cfg, channel_id, msg_id, text, ts, channel_override=None):
    result = pipeline.process(conn, cfg, channel_id=channel_id, msg_id=msg_id, text=text,
                              ts=ts, channel_override=channel_override)
    if result["status"] != "scored":
        return result
    if result["verdict"] == "ACCEPT":
        body = deliver.format_signal(result)
        await deliver.send_telegram(client, cfg["destination"], body)
        deliver.push_ntfy(body)
        db.mark_forwarded(conn, result["signal_id"])
        conn.commit()
        print(f"[FORWARD] {result['signal'].symbol} {result['signal'].side} "
              f"score={result['score']} via {result['channel']}")
    else:
        print(f"[reject ] {result['signal'].symbol} {result['signal'].side} "
              f"score={result['score']} — {'; '.join(result['reasons'])}")
    return result


# Held for the life of the process so a second agent can detect the first.
_LOCK_HANDLE = None


async def watch(cfg):
    global _LOCK_HANDLE
    try:
        _LOCK_HANDLE = singleton.acquire(str(config.ROOT / "sigfilter.lock"))
    except singleton.AlreadyRunning:
        raise SystemExit(
            "Another copy of the agent is already running - only one can read "
            "your account at a time.\n"
            "Close the other agent window, or stop the background task with:\n"
            "  Stop-ScheduledTask -TaskName sigfilter-watch\n"
            "then start this one again.")

    client = build_client()
    try:
        await client.start()
    except Exception as exc:
        if "database is locked" in str(exc):
            raise SystemExit(
                "Your Telegram session is in use by another copy of the agent. "
                "Close the other one and try again.")
        raise
    auto = bool(cfg.get("auto_follow", {}).get("enabled"))
    exclude = {int(x) for x in cfg.get("auto_follow", {}).get("exclude", []) or []}
    only_signal = cfg.get("auto_follow", {}).get("only_signal_like", True)
    sources = config.source_map(cfg)
    followed = {}      # chat_id -> name, kept fresh in auto mode

    async def refresh_followed():
        from . import picker

        fresh = {}
        async for dialog in client.iter_dialogs():
            if not (dialog.is_channel or dialog.is_group):
                continue
            if dialog.id in exclude:
                continue
            name = dialog.name or str(dialog.id)
            if only_signal and not picker.looks_like_signals(name):
                continue
            fresh[dialog.id] = name
        followed.clear()
        followed.update(fresh)
        print(f"Auto-follow: watching {len(followed)} signal channel(s) "
              f"(new ones you join are picked up automatically).")

    if auto:
        await refresh_followed()
        chats = None       # listen to everything; filter inside the handler
    else:
        if not sources:
            raise SystemExit("No channels configured. Run: python -m sigfilter.cli pick "
                             "(or turn on auto-follow: python -m sigfilter.cli follow auto)")
        chats = list(sources.keys())
        print(f"Watching {len(sources)} channel(s). Forwarding to {cfg['destination']!r}.")

    @client.on(events.NewMessage(chats=chats))
    async def _on_message(event):
        chat_id = event.chat_id
        override = None
        if auto:
            from . import picker

            if chat_id in exclude:
                return
            if not (event.is_channel or event.is_group):
                return                                    # ignore private chats
            name = followed.get(chat_id)
            if name is None:
                title = getattr(event.chat, "title", None) or str(chat_id)
                if only_signal and not picker.looks_like_signals(title):
                    return
                name = title
                followed[chat_id] = name                  # remember a newly-joined channel
            override = {"name": name, "weight": 1.0}
        text = event.message.message or ""
        ts = int(event.message.date.timestamp())
        with db.connect() as conn:
            try:
                await _handle(client, conn, cfg, chat_id, event.message.id, text, ts,
                              channel_override=override)
            except Exception as exc:                      # one bad message must not kill the listener
                print(f"[error  ] {type(exc).__name__}: {exc}")

    asyncio.create_task(_heartbeat_loop())
    if auto:
        asyncio.create_task(_refresh_loop(cfg, refresh_followed))
    if cfg["outcomes"]["enabled"]:
        asyncio.create_task(_outcome_loop(cfg))
    await client.run_until_disconnected()


async def _refresh_loop(cfg, refresh_followed):
    """Re-scan the channel list periodically so renamed or newly-joined channels
    stay current (new messages are already caught live; this keeps the list tidy
    and prints an updated count)."""
    hours = max(1, int(cfg.get("auto_follow", {}).get("refresh_hours", 24)))
    while True:
        await asyncio.sleep(hours * 3600)
        try:
            await refresh_followed()
        except Exception as exc:
            print(f"[error  ] refresh: {type(exc).__name__}: {exc}")


async def _heartbeat_loop():
    """Proof of life. Without it, a quiet market and a dead process look identical."""
    while True:
        try:
            with db.connect() as conn:
                db.set_state(conn, "heartbeat", int(time.time()))
        except Exception:
            pass
        await asyncio.sleep(30)


async def _outcome_loop(cfg):
    from . import outcomes
    interval = max(300, cfg["outcomes"]["poll_minutes"] * 60)
    while True:
        await asyncio.sleep(interval)
        try:
            counts = outcomes.grade_pending(cfg)
            resolved = counts.get("WIN", 0) + counts.get("LOSS", 0)
            if resolved:
                print(f"[grade  ] {counts.get('WIN',0)} win / {counts.get('LOSS',0)} loss")
        except Exception as exc:
            print(f"[error  ] grading: {type(exc).__name__}: {exc}")


async def backfill(cfg, limit=300):
    """Read recent history from every channel, score it as historical (never
    forwarded), then grade the crypto signals against real prices so channel
    trust reflects the backlog instead of starting from a blank slate."""
    client = build_client()
    await client.start()
    sources = config.source_map(cfg)
    now = int(time.time())
    scored = 0

    with db.connect() as conn:
        for channel_id in sources:
            name = sources[channel_id]["name"]
            batch = []
            async for msg in client.iter_messages(channel_id, limit=limit):
                if msg.message and msg.message.strip():
                    batch.append(msg)
            for msg in reversed(batch):          # oldest first for consensus
                result = pipeline.process(
                    conn, cfg, channel_id=channel_id, msg_id=msg.id,
                    text=msg.message, ts=int(msg.date.timestamp()),
                    now=now, historical=True)
                if result["status"] == "scored":
                    scored += 1
            conn.commit()
            print(f"  {name}: read {len(batch)} message(s)")

    print(f"\nScored {scored} past signal(s). Grading outcomes against real prices...")
    if cfg["outcomes"]["enabled"]:
        from . import outcomes
        counts = outcomes.grade_pending(cfg)
        wins, losses = counts.get("WIN", 0), counts.get("LOSS", 0)
        print(f"Graded outcomes: {wins} win, {losses} loss, "
              f"{counts.get('EXPIRED', 0)} expired, {counts.get('NODATA', 0)} no-data.")
    print("\nRun  python -m sigfilter.cli stats  to see which channels actually win.")
    await client.disconnect()


async def poll(cfg, limit=40):
    """Read what we missed since the last run, then exit. Cron-friendly."""
    client = build_client()
    await client.start()
    sources = config.source_map(cfg)
    now = int(time.time())
    processed = 0

    with db.connect() as conn:
        for channel_id in sources:
            key = f"last_msg:{channel_id}"
            last_seen = int(db.get_state(conn, key, 0) or 0)
            highest = last_seen
            batch = []
            async for msg in client.iter_messages(channel_id, limit=limit):
                if last_seen and msg.id <= last_seen:
                    break
                batch.append(msg)
                highest = max(highest, msg.id)
            for msg in reversed(batch):                  # oldest first: consensus needs order
                text = msg.message or ""
                if not text.strip():
                    continue
                await _handle(client, conn, cfg, channel_id, msg.id,
                              text, int(msg.date.timestamp()))
                processed += 1
            db.set_state(conn, key, highest)
            conn.commit()

    if cfg["outcomes"]["enabled"]:
        from . import outcomes
        outcomes.grade_pending(cfg)

    print(f"Polled {len(sources)} channel(s), processed {processed} new message(s) at {now}.")
    await client.disconnect()
