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


async def _handle(client, conn, cfg, channel_id, msg_id, text, ts):
    result = pipeline.process(conn, cfg, channel_id=channel_id, msg_id=msg_id, text=text, ts=ts)
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
    sources = config.source_map(cfg)
    if not sources:
        raise SystemExit("No sources configured. Run: python -m sigfilter.cli channels")

    print(f"Watching {len(sources)} channel(s). Forwarding to {cfg['destination']!r}.")

    @client.on(events.NewMessage(chats=list(sources.keys())))
    async def _on_message(event):
        text = event.message.message or ""
        ts = int(event.message.date.timestamp())
        with db.connect() as conn:
            try:
                await _handle(client, conn, cfg, event.chat_id, event.message.id, text, ts)
            except Exception as exc:                      # one bad message must not kill the listener
                print(f"[error  ] {type(exc).__name__}: {exc}")

    asyncio.create_task(_heartbeat_loop())
    if cfg["outcomes"]["enabled"]:
        asyncio.create_task(_outcome_loop(cfg))
    await client.run_until_disconnected()


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
            graded = outcomes.grade_pending(cfg)
            if graded:
                print(f"[grade  ] updated {graded} signal outcome(s)")
        except Exception as exc:
            print(f"[error  ] grading: {type(exc).__name__}: {exc}")


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
