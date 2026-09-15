#!/data/data/com.termux/files/usr/bin/bash
# Starts the agent on the phone and keeps Android from sleeping the process.
# Leave the Termux session running. Stop it with Ctrl+C.
cd "$(dirname "$0")"

# Stops Android suspending the CPU while the agent waits for messages.
termux-wake-lock 2>/dev/null || true

echo "sigfilter is running on this phone. Keep Termux open."
echo "Signals go to Telegram Saved Messages and your ntfy push."
echo
python -m sigfilter.cli watch

# Release the wake lock if watch ever exits.
termux-wake-unlock 2>/dev/null || true
