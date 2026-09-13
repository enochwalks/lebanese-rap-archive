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

from . import config, db, deliver, pipeline


def build_client():
    config.load_env()
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TG_API_ID and TG_API_HASH must be set (see .env.example)")
    session_str = os.environ.get("TG_SESSION")
    session = StringSession(session_str) if session_str else StringSession()
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


async def watch(cfg):
    client = build_client()
    await client.start()
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

    if cfg["outcomes"]["enabled"]:
        asyncio.create_task(_outcome_loop(cfg))
    await client.run_until_disconnected()


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
