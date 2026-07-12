# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Telegram RSS subscription bot built on `python-telegram-bot[job-queue]`. Polls subscribed
feeds every 5 minutes and pushes new entries to subscribed chats. Also bundles two extra
commands: `/no` (no-as-a-service replies) and `/sticker` (Telegram sticker → PNG/GIF).

## Commands

```bash
pip install -r requirements.txt   # deps
cp .env.example .env              # then fill TELEGRAM_BOT_TOKEN + WHITE_LIST
python bot.py                     # run (long-running poller; Ctrl-C for graceful shutdown)
```

No test suite, linter, or build step exists. `ffmpeg` must be on PATH for video-sticker
(`.webm`) → GIF conversion; without it `/sticker` errors only on video stickers.

## Environment (.env)

- `TELEGRAM_BOT_TOKEN` — required; bot exits early if missing.
- `WHITE_LIST` — comma-separated user IDs allowed to use admin commands in **private** chats.
- `GITHUB_TOKEN` — optional; raises GitHub API rate limit for changelog fallback (60→5000/hr).
- `TG2GIF_*` — `/sticker` output tuning (max size, fps, max bytes, background white|transparent).

## Architecture

Single SQLite-backed process. Module map:

- **bot.py** — entrypoint. Builds the `Application`, registers `CommandHandler`s, and starts
  a `JobQueue` running `check_updates` every 300s. Creates two shared `httpx.AsyncClient`s in
  `post_init` stored in `application.bot_data` (`rss_http_client`, `no_http_client`) and closes
  them in `post_shutdown`. Handlers reach these clients via `context.application.bot_data`.
- **handlers.py** — command handlers: `/add /list /remove /export /import /start /help` (feed
  management + OPML) and `/no`.
- **rss_checker.py** — the polling job. `check_updates` → `fetch_and_process_feeds` fans out
  over all feeds (semaphore-limited to 10 concurrent, each with 0–5s random jitter), then sends
  collected `(chat_id, message)` tuples. Messages are HTML, chunked under
  `MAX_TELEGRAM_MESSAGE_LENGTH`.
- **data_store.py** — SQLite layer. A single module-global `store = SQLiteStore()` is imported
  everywhere. Connections are **thread-local** (`threading.local`) with WAL mode, which matters
  because `feedparser.parse` runs via `asyncio.to_thread`. Domain-grouped operation classes:
  `store.chats`, `store.feeds`, `store.subscriptions`, `store.messages`, `store.feed_entries`.
  Schema is created on first connection in `_init_database`.
- **utils.py** — shared feed-fetch helpers (`build_feed_request_headers`,
  `fetch_feed_with_retry` with backoff on retryable statuses, the SSRF guard, and a streaming
  response-size cap), `parse_feed_bytes` (feedparser on a dedicated single-thread executor),
  `trim_memory` (glibc `malloc_trim`, no-op elsewhere), entry-key extraction
  (`build_entry_key`), the `Messages` text constants, and the **permission layer**.
- **github_enricher.py** — for `*/releases.atom` feeds, appends a "Full Changelog" compare
  link, parsed from entry HTML first, falling back to the GitHub Releases API.
- **sticker_converter.py** — `/sticker`: static→PNG (Pillow), animated `.tgs`→GIF
  (rlottie-python), video `.webm`→GIF (ffmpeg subprocess).

## Key invariants

- **Permissions:** `/start /help /add /list /remove /export /import` are gated by `admin_filter`
  (registered in bot.py). The filter requires whitelist membership in private chats and
  owner/admin status in groups/channels. `/no` and `/sticker` are intentionally **public** (no
  filter).
- **No history spam:** new entries are detected via persistent per-feed entry keys
  (`feed_entries` table, `build_entry_key`). On a feed's first poll,
  `feed_entries.register_and_get_new` seeds all current keys and reports nothing new, so
  existing entries are never sent on subscription. `last_check_time` is diagnostic metadata
  only.
- **SSRF guard:** all feed fetches go through `fetch_feed_with_retry`, which validates every
  URL (and every redirect hop) with `assert_public_url`; rejections raise
  `UnsafeFeedURLError`.
- **Dedup:** `message_history` table deduplicates by `(title, feed_url, chat_id)` within a
  1.5h window (`DEDUP_WINDOW = 5400`); old rows are cleaned at the top of each `check_updates`.
- **Feed GC:** unsubscribing the last subscriber of a feed deletes the feed row
  (`subscriptions.unsubscribe`).
- **Conditional GET:** ETag / Last-Modified are stored per feed and replayed as
  `If-None-Match` / `If-Modified-Since`; a 304 short-circuits processing.
- **Memory bounds:** feed responses are streamed with a hard cap
  (`MAX_FEED_RESPONSE_BYTES`, oversize raises `FeedTooLargeError`); feed parsing and sticker
  conversion each run on their own single-worker executor so peak memory is one parse tree /
  one conversion at a time; `trim_memory()` runs after each poll cycle and sticker conversion
  to return freed pages to the OS (glibc only).

## Conventions

Code is async throughout (handlers + job). Blocking work is offloaded to threads: feed
parsing via `utils.parse_feed_bytes` and sticker conversion via a module-level single-worker
executor (both deliberately serialized for memory reasons — don't switch them back to
`asyncio.to_thread`). User-facing text lives in `utils.Messages`;
inline user-facing strings are Chinese in places (sticker errors, http.cat fallbacks). Telegram
messages use `parse_mode='HTML'` and all dynamic text is passed through `html.escape`.
