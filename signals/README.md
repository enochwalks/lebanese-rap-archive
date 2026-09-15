# sigfilter — one gold signal instead of forty noisy ones

Reads every Telegram signal channel you're already a member of, parses each post
into a structured trade, scores it, and forwards **only** what clears the gate to
one place (your Saved Messages, a private channel, and optionally your phone).

Everything here runs on free infrastructure: the Telegram API is free, Binance's
public price endpoint needs no key, and `ntfy.sh` push needs no account.

## What it actually does

```
channels ──► parse ──► dedupe ──► consensus ──► score ──► gate ──► you
             │         │          │             │         │
             │         │          │             │         └─ hard rules: no SL,
             │         │          │             │            bad R:R, stale,
             │         │          │             │            daily cap
             │         │          │             └─ 6 weighted components,
             │         │          │                every one shown to you
             │         │          └─ do other channels agree, disagree,
             │         │             or are they just copy-pasting?
             │         └─ same post, reposted or relayed, counts once
             └─ strict: a post it can't read confidently is dropped,
                never guessed at
```

**The six scoring components** (weights in `config.yaml`):

| Component | What it measures |
|---|---|
| `channel_trust` (30) | That channel's own graded win/loss record, Wilson lower bound so a 3-for-3 newcomer can't outrank a 60-for-100 veteran |
| `completeness` (15) | Entry, target *and* stop present; parse warnings subtract |
| `risk_reward` (15) | `(TP1 − entry) / (entry − SL)`. Zero below your minimum, saturates at 3R because bigger printed targets are usually fiction |
| `consensus` (20) | Independent channels calling the same direction in the same window |
| `discipline` (10) | Penalties for missing stops, 50x leverage, "GUARANTEED", all-caps, 8-target ladders |
| `freshness` (10) | An entry price 40 minutes old is usually already gone |

**The two things that make this more than a regex:**

1. **Copy-paste detection.** Most signal channels relay each other. Three channels
   posting the same text is *one* source wearing three hats, and counting it as
   agreement is the fastest way to build a filter that is confidently wrong. Posts
   above `near_duplicate_ratio` text similarity collapse to a single vote.
2. **Outcomes feed back into trust.** Every crypto signal is graded against Binance
   candles — did TP1 trade before SL? Channels that are right earn weight; channels
   that are wrong lose it, automatically. Without this loop "trust" is just a number
   you made up.

## Setup on Windows (about 10 minutes)

Open PowerShell and run these **one block at a time**, not all pasted together.

```powershell
# 1. Install Python and Git (skip either if you already have it),
#    then CLOSE PowerShell and open it again so PATH updates.
winget install -e --id Python.Python.3.11
winget install -e --id Git.Git
```

```powershell
# 2. Get the code into your Documents folder (not system32).
cd $HOME\Documents
git clone -b claude/nifty-pasteur-rcppo9 https://github.com/enochwalks/lebanese-rap-archive.git
cd lebanese-rap-archive\signals
```

```powershell
# 3. One script does the rest: virtual environment, dependencies, config files.
.\setup.ps1
```

If PowerShell refuses to run the script ("running scripts is disabled"), allow it
for this window only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Then, one at a time:

```powershell
.\run.ps1 login       # one-time Telegram login
.\run.ps1 pick        # numbered list of your chats -> choose -> writes config.yaml
.\run.ps1 watch       # starts filtering
```

`pick` is the safe way to choose channels: it rewrites only the `sources:` block
and keeps a `.bak`, so a mistyped space cannot break the rest of the file. Run it
again any time you join or leave a channel. `channels` still prints the raw ids if
you would rather edit the file yourself.

`run.ps1` works for every command: `stats`, `recent`, `poll`, `test`.

## Setup on macOS / Linux

```bash
cd signals
pip install -r requirements.txt
cp .env.example .env          # fill in TG_API_ID / TG_API_HASH from my.telegram.org
cp config.example.yaml config.yaml

python -m sigfilter.cli login       # one-time; prints a TG_SESSION string for .env
python -m sigfilter.cli channels    # lists your chats + their ids
#   paste the ids of the signal channels into config.yaml under `sources:`
python -m sigfilter.cli watch       # go
```

## Running it 24/7, free

| Option | Latency | Notes |
|---|---|---|
| Any always-on machine (old laptop, Raspberry Pi, free-tier VPS) running `watch` | seconds | Best. Add a `systemd` unit or `tmux` so it restarts |
| GitHub Actions (`.github/workflows/signal-filter.yml`, `poll` mode) | 10–25 min | Truly free, zero hardware. Scheduled runs are delayed under load, and GitHub disables schedules after 60 days of repo inactivity |

For scalping, minutes of latency destroys the edge — use `watch` on real hardware.
For swing entries with a wide entry zone, `poll` is fine.

**Security:** `TG_SESSION` is full access to your Telegram account. If you put it in
GitHub Secrets, use a **separate Telegram account** that is only a member of the
signal channels — never your main one. Telegram may also challenge logins coming
from datacenter IPs.

## Using the backlog

The live agent only sees messages that arrive after it starts - it does not read
old history, and it shouldn't forward stale entries to trade. But the backlog is
free evidence about channel quality:

```powershell
.\run.ps1 backfill        # reads recent history, grades it, builds trust
```

It scores every past post as if it were fresh (never forwarding any), grades them
against what the price actually did - crypto on Binance, gold/silver/forex/indices
on Yahoo Finance, both free and keyless - and updates each channel's record.
Afterwards `stats` and the dashboard show real win/loss numbers immediately
instead of after weeks of waiting. Run it once, after `pick`.

## Watching it work

```powershell
.\shortcuts.ps1          # puts two shortcuts on your Desktop (setup.ps1 does this too)
```

**Signal Dashboard** starts the local server if it isn't already up and opens the
page; double-clicking it twice is harmless. **Start Signal Agent** runs the
listener in a visible window, and is only needed if you skipped
`install-task.ps1`. Remove both with `.\shortcuts.ps1 -Remove`.

From a terminal instead:

```powershell
.\run.ps1 dashboard      # opens http://127.0.0.1:8765 in your browser
```

A live page showing whether the listener is actually alive (it writes a heartbeat
every 30 seconds, so a quiet market and a dead process don't look the same), what
it has read today, the last signal that passed with its score broken into the six
components, every message it rejected and why, and the quality table for every
channel.

It binds to 127.0.0.1 only and reads `signals.db` directly. The page cannot be
hosted anywhere else, by design: your trading history and channel list would have
to leave the machine first.

Run it alongside `watch` in a second PowerShell window, or leave the scheduled
task running and just open the dashboard when you want to look.

## Running unattended

```powershell
.\install-task.ps1           # start automatically at every Windows login
.\install-task.ps1 -Remove   # undo that
```

Runs the listener in the background under `pythonw` with no console window and
automatic restart. It only runs while you are logged in.

## Tuning it

```bash
# Score a real post from one of your channels, without touching Telegram:
python -m sigfilter.cli test --text "XAUUSD BUY 2340 TP 2365 SL 2332"

python -m sigfilter.cli recent    # what passed, what failed, and why
python -m sigfilter.cli stats     # per-channel record and current trust
```

**Expect near-silence in week one, by design.** With no graded history every channel
sits at the neutral 0.5 prior, so a clean but unconfirmed signal scores around 65 and
a perfect one caps near 75. Start at `min_score: 60` while the record builds, run
`stats` after a week or two, then raise it to 70–75 and set `weight: 0.0` on the
channels that have proven themselves useless. That tightening is the whole product —
the code just gives you the evidence to do it.

## Honest limits

- **Only crypto outcomes are auto-graded.** Forex, gold and indices need a priced
  data feed with an API key; those signals stay ungraded, so those channels keep
  the neutral prior unless you set `weight` by hand.
- **Grading is conservative.** If one 5-minute candle touches both TP and SL we
  can't tell which came first, so it's scored a loss. Assuming the good fill is
  how backtests lie to you.
- **The filter cannot make a bad channel profitable.** It measures completeness,
  discipline and consistency — not whether the analysis is any good. A channel
  that posts beautifully-formatted losing trades will score well until enough
  outcomes accumulate to sink it. Paper-trade the output before risking money.
- **Parsing is strict on purpose.** Some real signals in unusual formats will be
  dropped. A missed signal costs nothing; a mis-parsed stop-loss costs money.
