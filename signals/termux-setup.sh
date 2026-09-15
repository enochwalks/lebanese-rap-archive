#!/data/data/com.termux/files/usr/bin/bash
# One-time setup for running sigfilter on an Android phone via Termux.
# Run it from inside the signals folder:   bash termux-setup.sh
set -e

echo "== sigfilter phone setup =="
echo

# Termux packages: Python and git.
pkg update -y
pkg install -y python git

python -m pip install --upgrade pip
echo "Installing dependencies (a minute or two)..."
pip install -r requirements.txt

# Config files (never overwrite existing ones).
[ -f .env ] || cp .env.example .env
[ -f config.yaml ] || cp config.example.yaml config.yaml

echo
echo "== Done. Next: =="
echo "1. Put your Telegram keys in .env :   nano .env"
echo "     (same TG_API_ID / TG_API_HASH from my.telegram.org)"
echo "     add your NTFY_TOPIC line too, so the phone still pushes."
echo "2. Log in once:                       python -m sigfilter.cli login"
echo "3. Choose channels (or copy config):  python -m sigfilter.cli pick"
echo "4. Start it:                          bash termux-run.sh"
