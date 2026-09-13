"""Formatting and delivery of accepted signals."""

import os


def format_signal(result):
    sig = result["signal"]
    info = result["consensus"]
    wins, losses = result["record"]
    rr = sig.risk_reward()

    lines = [
        f"🏆 GOLD SIGNAL — score {result['score']}/100",
        "",
        f"{sig.symbol}  {sig.side}",
        f"Entry: {' – '.join(f'{e:g}' for e in sig.entries) if sig.entries else 'n/a'}",
        f"TP:    {', '.join(f'{t:g}' for t in sig.tps) if sig.tps else 'n/a'}",
        f"SL:    {sig.sl:g}" if sig.sl else "SL:    n/a",
        f"R:R:   {rr:.2f}" if rr else "R:R:   n/a",
    ]
    if sig.leverage:
        lines.append(f"Lev:   {sig.leverage}x")
    lines += [
        "",
        f"Source: {result['channel']} (record {wins}W/{losses}L, trust {result['trust']})",
    ]
    if info["agreeing_channels"]:
        lines.append(f"Confirmed by {info['agreeing_channels']} other channel(s) within {info['window_minutes']}m")
    if info.get("duplicate_of") is not None:
        lines.append("Note: near-identical to another channel's post — counted as one source, not confirmation")
    top = sorted(result["breakdown"].items(), key=lambda kv: -kv[1]["points"])[:3]
    lines.append("Why: " + ", ".join(f"{k} {v['points']}pts" for k, v in top))
    return "\n".join(lines)


async def send_telegram(client, destination, text):
    target = destination
    if isinstance(destination, str) and destination not in ("me",):
        try:
            target = int(destination)
        except ValueError:
            target = destination
    await client.send_message(target, text)


def push_ntfy(text, topic=None, title="Gold signal"):
    """Optional phone push. Free, no account, best-effort."""
    topic = topic or os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    import requests

    url = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
    try:
        requests.post(url, data=text.encode("utf-8"),
                      headers={"Title": title, "Priority": "high"}, timeout=10)
        return True
    except requests.RequestException:
        return False
