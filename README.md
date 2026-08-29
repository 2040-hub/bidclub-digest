# BidClub Daily Podcast Digest

Fetches every podcast episode published the previous day from the public
[BidClub API](https://bidclub.ai/api-docs) and pushes the ready-made bilingual
summaries to Telegram and Lark (Feishu). Designed to run once a day from cron at
08:00 Beijing time.

Long digests are split into several messages automatically, so neither
platform's size limit can make a send fail.

## Features

- **No account, no API key.** The BidClub episode API is public and keyless.
- **"Yesterday" is resolved in your timezone**, not the server's, so the same
  crontab entry produces the same digest on a UTC host and on a Beijing host.
- **Ready-made summaries.** BidClub publishes a TL;DR, a sectioned digest and a
  one-paragraph blurb per episode, each in English *and* Chinese. Nothing is
  generated locally, so there is no LLM cost and no summarisation latency.
- **Automatic message splitting.** Telegram caps a message at 4096 characters,
  a Lark custom bot caps the whole request body at 20 KB. Chunks are packed to
  fit, numbered `(2/5)`, and paced apart so neither platform throttles the run.
- **Exactly-once delivery, per destination.** A local ledger records which
  episode reached which chat, so a re-run, a cron retry or a wider lookback
  window can never double-post — and a channel that failed is retried tomorrow.
- **Degrades instead of failing.** A withdrawn episode falls back to its list
  metadata, a Telegram markup error resends the chunk as plain text, and a Lark
  card that is rejected falls back to rich text and then to plain text. An
  episode whose summary could not be *reached* is held back instead, so a
  timeout never freezes a "summary unavailable" placeholder into the ledger.
- **Injection-safe.** Episode titles are upstream text: HTML is escaped for
  Telegram, and Lark `<at>` mention tags are stripped so a crafted title cannot
  ping a whole work group.

## Requirements

- Python 3.13+
- `requests` (and `tzdata` on hosts with no system timezone database)

## Installation

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy the configuration template and fill in your credentials:

```bash
cp config.ini.template config.ini
```

3. Verify that every channel is reachable:

```bash
python3 bidclub_digest.py --check
```

## Configuration

`config.ini` has eight sections. Every key is documented inline in
`config.ini.template`; the settings you are most likely to change are below.

Keep comments on their own line. Inline comments use `;` only, on purpose — so
a `#` inside a URL or a template string is never silently truncated.

### Channels

```ini
[telegram]
enabled = true
bot_token = 123456789:AA...
; one or more targets, comma separated; the bot must belong to each chat
chat_id = -1001234567890, @my_channel
parse_mode = html
disable_notification = true

[lark]
enabled = true
webhook_url = https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
; only when the bot has signature verification enabled
secret =
msg_type = interactive
```

At least one channel must be configured or the script exits with an error.
Each Telegram chat is tracked independently, so adding a second chat later does
not replay history into the first one.

### Which day, and which episodes

```ini
[window]
timezone = Asia/Shanghai
date_basis = published_at
lookback_days = 3
notify_on_first_run = false

[content]
summary_field = tldr
language = zh
max_episodes = 12
```

- `timezone` defines what "yesterday" means. The digest always reports on the
  previous calendar day in this timezone.
- `date_basis` picks the field that decides an episode's day. `published_at` is
  the real publication instant and the only field the API orders by. The
  alternative, `date`, is the date the show itself declares; it disagrees with
  the local date of `published_at` for roughly a third of episodes.
- `lookback_days` widens the scan beyond yesterday. Episodes published late in
  the day keep arriving in the API well into the next day, so a single 08:00
  pass over yesterday alone would permanently drop the late arrivals. The
  ledger means a wider window never double-posts; it only catches up. Set it to
  `1` for strictly-yesterday behaviour.
- `language` is `zh`, `en`, `primary` or `both`. Every episode carries one
  primary language plus a translation of the other, so `zh` reads Chinese
  whether the show is Chinese or English.
- `summary_field` is `tldr` (a bullet list, the default), `digest` (the full
  sectioned write-up, several times longer) or `dek` (one paragraph).

### Splitting and pacing

```ini
[output]
telegram_chunk_chars = 3500
telegram_max_chunks = 20
telegram_delay_seconds = 3.2
lark_chunk_bytes = 8000
lark_max_chunks = 20
lark_delay_seconds = 1.0
startup_jitter_seconds = 180
title = BidClub Daily Podcast
```

The two budgets use different units on purpose — Telegram counts characters,
Lark counts bytes of the serialized request body. See
[Message splitting](#message-splitting) below.

If your Lark bot has a required keyword configured, put that keyword in
`title`: only `text` and `title` fields are scanned for it.

## Usage

```bash
# Yesterday's digest (what cron runs)
python3 bidclub_digest.py

# A specific day, in the configured timezone
python3 bidclub_digest.py --date 2026-08-28

# Render and print every chunk without sending or recording anything
python3 bidclub_digest.py --dry-run --date 2026-08-28

# Validate credentials and chat membership, send nothing
python3 bidclub_digest.py --check

# Send a short live test message to every enabled channel
python3 bidclub_digest.py --test

# Re-push one day that was already delivered
python3 bidclub_digest.py --force --date 2026-08-28 --lookback 1
```

| Flag | Meaning |
| --- | --- |
| `-c`, `--config PATH` | path to `config.ini` (default: alongside the script) |
| `--date YYYY-MM-DD` | target day, in the configured timezone (default: yesterday) |
| `--lookback N` | override `[window] lookback_days` for this run |
| `--limit N` | override `[content] max_episodes` for this run |
| `--force` | ignore the ledger; push every episode in the window again |
| `--dry-run` | render and print; no network sends, no ledger write |
| `--check` | preflight only: `getMe`, `getChat`, webhook shape, signature self-test |
| `--test` | send a short live message through the real send path; every enabled destination must accept it |
| `--telegram-only` / `--lark-only` | disable the other channel for this run |
| `--no-jitter` | skip `startup_jitter_seconds` |
| `-v`, `--verbose` | force `DEBUG` logging |
| `--version` | print the version and exit |

Exit codes: `0` success or nothing to do, `1` fatal, `2` `--check`/`--test`
failed, `3` partial delivery (retried on the next run), `130` interrupted.

## Cron Job

The script resolves "yesterday" in `[window] timezone` itself, so the host's own
timezone only decides *when* the job fires, never *which* day it reports on.

```bash
# 08:00 Beijing time, server in Asia/Shanghai
0 8 * * * cd $YOUR_DIR/bidclub-digest/ && bash crontab.sh &

# Server in UTC (08:00 Beijing = 00:00 UTC)
0 0 * * * cd $YOUR_DIR/bidclub-digest/ && bash crontab.sh &
```

If the host timezone is uncertain, pin it in the crontab instead of guessing:

```bash
CRON_TZ=Asia/Shanghai
0 8 * * * cd $YOUR_DIR/bidclub-digest/ && bash crontab.sh &
```

Overlapping runs are safe: a lock on `bidclub_digest.lock` makes the second one
exit immediately without sending. `crontab.sh` redirects to its own
`bidclub_digest_cron.log`, kept separate from the rotating log the service
writes itself so no line is recorded twice in the same file.

## How Deduplication Works

`sent_episodes.json` records, per episode slug, which destinations already
received it — `telegram:<chat_id>` for each chat and `lark` for the webhook.

- An episode is selected for a destination when it is inside the day window
  **and** that destination is absent from its `delivered` map.
- A slug is marked delivered for a destination only if **every** chunk carrying
  any of its content was accepted. A partial failure is retried in full the next
  day; a duplicated half is preferable to a hole.
- On the very first run, with `notify_on_first_run = false`, only the target day
  is pushed and the older episodes in the window are recorded as delivered, so
  the first run does not flood the chat with a week of history.
- An unreadable ledger is renamed to `sent_episodes.json.corrupt-<epoch>` and
  the run is treated as a first run. At worst one day is re-posted, never a week.
- Entries older than `state_retention_days` (90 by default) are pruned on write.

## Message Splitting

The digest is rendered as a list of atomic lines — one logical line each, with
every markup tag opened and closed inside it. Chunks are then cut **between**
lines, so a split can never bisect a tag and produce the classic "unterminated
entity" rejection.

| | Telegram | Lark |
| --- | --- | --- |
| Hard limit | 4096 characters after entity parsing | 20480 bytes of request body |
| Unit measured | `max(code points, UTF-16 units)` | UTF-8 bytes of the serialized body |
| Default budget | 3500 characters | 8000 bytes of content |
| Pacing | 3.2 s between messages (20/min group ceiling) | 1.0 s (100/min per bot) |
| Formatting | HTML | card markdown → rich text → plain text |

A single line that is still too long is stripped of its markup and cut on a
sentence boundary, then on an exact character boundary found by binary search —
a proportional estimate overshoots on mixed text, where an emoji costs one code
point but two Telegram units and a Chinese character costs one character but
three UTF-8 bytes.

If the digest would need more messages than `telegram_max_chunks` /
`lark_max_chunks`, the tail episodes are replaced by a compact list of titles
and links rather than being dropped silently.

`startup_jitter_seconds` delays the first send by a random interval because
Lark's documentation warns that messages landing exactly on the hour draw
throttling errors — and this job fires at exactly 08:00.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Configuration file not found` | `cp config.ini.template config.ini` |
| `No notification channel configured` | Fill in `[telegram] bot_token` + `chat_id`, or `[lark] webhook_url` |
| `--check` fails with `401 Unauthorized` | The Telegram bot token is wrong or was revoked |
| `--check` fails with `chat not found` | The bot is not a member of that chat. Add it to the group first |
| Lark `19001` | The webhook token is invalid or was regenerated; copy the new URL |
| Lark `19021` | Wrong `[lark] secret`, or the host clock is more than an hour off |
| Lark `19022` | This host's IP is not on the bot's IP allowlist |
| Lark `19024` | The bot requires a keyword; put it in `[output] title` |
| `Another run holds ...lock` | A previous run is still going. Benign — it exits `0` |
| `max_pages ... this scan is INCOMPLETE` | The window needs more than `page_size × max_pages` rows. Raise `[bidclub] max_pages` or lower `lookback_days` |
| `Holding back N episode(s)` | Their detail fetch timed out. They ship on the next run |
| Nothing is sent, log says "already delivered" | The ledger has them. Use `--force` to re-push |
| The digest is missing a late episode | BidClub ingests some episodes hours after publication; `lookback_days = 3` picks them up the next morning |

Add `-v` for `DEBUG` logging, and `--dry-run` to see exactly what would be sent
and how large each chunk is.

## File Structure

```
podcast/bidclub/
├── bidclub_digest.py      # the service: fetch → render → split → push → record
├── config.ini.template    # configuration template (copy to config.ini)
├── crontab.sh             # one-shot launcher for cron
├── requirements.txt       # Python dependencies
├── .gitignore             # ignores config.ini, runtime state and logs
├── README.md              # English documentation
└── README_cn.md           # Chinese documentation
```

Runtime files, all git-ignored: `config.ini`, `sent_episodes.json` (the delivery
ledger), `bidclub_digest.lock`, the rotating `bidclub_digest.log`, and
`bidclub_digest_cron.log` — where `crontab.sh` captures anything that fails
before logging is configured, such as a missing dependency.

## Notes

- `config.ini` holds a bot token and a webhook secret. It is git-ignored, and
  the logger redacts both from every message, including exception text.
- The BidClub API asks that a link back to the episode and its original source
  be included; both are on by default via `include_episode_link` and
  `include_source_link`.
- `transcript_md` is discarded as soon as an episode is fetched. It is roughly
  80% of the payload and is never rendered.

## License

MIT
