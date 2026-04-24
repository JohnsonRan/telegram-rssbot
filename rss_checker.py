import asyncio
import calendar
import logging
import random
import time
from html import escape

import feedparser
import httpx

from data_store import store
from github_enricher import get_changelog_link, is_github_release_feed
from utils import build_feed_request_headers, fetch_feed_with_retry

logger = logging.getLogger(__name__)

# Configuration
MAX_ENTRIES_PER_UPDATE = 10  # Maximum entries to push per feed per check
MAX_CONCURRENT_FEEDS = 10  # Maximum number of feeds to fetch concurrently
MAX_TELEGRAM_MESSAGE_LENGTH = 3800  # Keep some margin below Telegram's hard limit
MAX_ENTRY_TITLE_LENGTH = 512
MAX_ENTRY_LINK_LENGTH = 1024


def _truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return text[: max_length - 3] + "..."


def _remove_blank_lines(text: str, joiner: str = " ") -> str:
    parts = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return joiner.join(parts)


def _build_feed_messages(feed_title: str, entries_with_changelog: list) -> list[str]:
    """Build one or more Telegram-safe HTML messages for a feed update.

    Args:
        feed_title: Display title of the feed.
        entries_with_changelog: List of (entry_dict, changelog_line_or_None) tuples.
    """
    normalized_feed_title = _remove_blank_lines(feed_title or "Untitled Feed", joiner=" ").strip() or "Untitled Feed"
    safe_feed_title = escape(_truncate_text(normalized_feed_title, 256), quote=False)
    header = f"<b>{safe_feed_title}</b>"

    messages = []
    current_message = f"{header}\n"

    for entry, changelog_line in entries_with_changelog:
        raw_title = _remove_blank_lines(entry.get("title") or "No Title", joiner=" ").strip() or "No Title"
        safe_title = escape(_truncate_text(raw_title, MAX_ENTRY_TITLE_LENGTH), quote=False)

        raw_link = _remove_blank_lines(entry.get("link") or "", joiner="").strip()
        safe_link = escape(_truncate_text(raw_link, MAX_ENTRY_LINK_LENGTH), quote=True)

        if safe_link:
            entry_line = f'• <b><a href="{safe_link}">{safe_title}</a></b>'
        else:
            entry_line = f"• <b>{safe_title}</b>"

        block = f"{entry_line}\n"
        if changelog_line:
            block += f"{changelog_line}\n"

        if len(current_message) + len(block) > MAX_TELEGRAM_MESSAGE_LENGTH:
            messages.append(current_message.rstrip())
            current_message = f"{header}\n{block}"
        else:
            current_message += block

    if current_message.strip():
        messages.append(current_message.rstrip())

    return messages


async def fetch_single_feed(feed_url: str, feed_data: dict, current_time: float, http_client):
    """
    Fetch and process a single feed.
    Returns a list of (chat_id, message) tuples to send.

    Args:
        feed_url: URL of the feed to fetch
        feed_data: Feed metadata from database
        current_time: Current timestamp
        http_client: Shared HTTP client instance
    """
    messages_to_send = []

    try:
        headers = build_feed_request_headers(feed_data.last_etag, feed_data.last_modified)

        try:
            response = await fetch_feed_with_retry(http_client, feed_url, headers=headers)
        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching {feed_url}")
            return messages_to_send
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching {feed_url}: {e}")
            return messages_to_send

        # Check if feed was modified (304 Not Modified)
        if response.status_code == 304:
            # Not modified, skip
            return messages_to_send

        # Check for errors
        if response.status_code >= 400:
            logger.warning(f"HTTP {response.status_code} for {feed_url}")
            return messages_to_send

        # Parse feed content
        d = await asyncio.to_thread(feedparser.parse, response.content)

        # Update feed metadata
        etag = response.headers.get('ETag')
        modified = response.headers.get('Last-Modified')
        latest_title = d.feed.get('title', None)

        store.feeds.update(feed_url, last_etag=etag, last_modified=modified, title=latest_title)

        # Get last check time
        last_check_time = feed_data.last_check_time

        # Check for new entries based on publish time
        new_entries = []

        for entry in d.entries:
            # Get entry publish time
            entry_time = None

            # Try different time fields
            for time_field in ['published_parsed', 'updated_parsed', 'created_parsed']:
                if hasattr(entry, time_field) and getattr(entry, time_field):
                    time_struct = getattr(entry, time_field)
                    try:
                        # Use calendar.timegm for UTC time (feedparser returns UTC)
                        entry_time = calendar.timegm(time_struct)
                        break
                    except (ValueError, OverflowError):
                        continue

            if last_check_time is None:
                # First check - skip all existing entries (don't push history)
                continue

            # If entry has no publish time, skip it (can't determine if it's new)
            if entry_time is None:
                continue

            # Check if entry is newer than last check
            if entry_time > last_check_time:
                new_entries.append(entry)

        # Update last check time
        store.feeds.update(feed_url, last_check_time=current_time)

        # Prepare messages for subscribers
        if new_entries:
            subscribers = store.subscriptions.get_feed_subscribers(feed_url)

            # Limit the number of entries to push to avoid spam
            if len(new_entries) > MAX_ENTRIES_PER_UPDATE:
                entries_to_push = new_entries[:MAX_ENTRIES_PER_UPDATE]
            else:
                entries_to_push = new_entries

            # Get feed title
            feed_title = latest_title or feed_data.title or feed_url

            # For GitHub release feeds, resolve changelog links once (shared across subscribers)
            github_feed = is_github_release_feed(feed_url)
            changelog_map: dict[str, str | None] = {}
            if github_feed:
                for entry in entries_to_push:
                    key = (entry.get("link") or entry.get("id") or "").strip()
                    if key and key not in changelog_map:
                        changelog_map[key] = await get_changelog_link(http_client, feed_url, entry)

            # Process each subscriber separately for deduplication
            total_filtered = 0
            for chat_id in subscribers:
                ordered_entries = list(reversed(entries_to_push))  # Oldest first
                candidate_titles = [(entry.get('title') or 'No Title') for entry in ordered_entries]
                allowed_titles = store.messages.filter_and_record_batch(feed_url, chat_id, candidate_titles)

                filtered_entries = []
                sent_titles = set()
                for entry in ordered_entries:
                    title = (entry.get('title') or 'No Title')
                    if title in allowed_titles and title not in sent_titles:
                        filtered_entries.append(entry)
                        sent_titles.add(title)
                    else:
                        total_filtered += 1
                        logger.debug(f"Skipping duplicate message '{title}' for chat {chat_id}")

                if filtered_entries:
                    entries_with_changelog = [
                        (entry, changelog_map.get((entry.get("link") or entry.get("id") or "").strip()) if github_feed else None)
                        for entry in filtered_entries
                    ]
                    for combined_message in _build_feed_messages(feed_title, entries_with_changelog):
                        messages_to_send.append((chat_id, combined_message))

            if total_filtered > 0:
                logger.info(f"Filtered {total_filtered} duplicate messages from {feed_url}")

            if len(new_entries) > MAX_ENTRIES_PER_UPDATE:
                logger.info(
                    f"Found {len(new_entries)} new entries in {feed_url}, "
                    f"pushing {MAX_ENTRIES_PER_UPDATE}, skipping {len(new_entries) - MAX_ENTRIES_PER_UPDATE}"
                )
            else:
                logger.info(f"Found {len(new_entries)} new entries in {feed_url} for {len(subscribers)} subscribers")

    except Exception as e:
        logger.error(f"Error processing feed {feed_url}: {e}")

    return messages_to_send


async def fetch_and_process_feeds(http_client):
    """
    Fetch all feeds concurrently and check for new entries based on publish time.
    Returns a list of (chat_id, message) tuples to send.

    Args:
        http_client: Shared HTTP client instance for fetching feeds
    """
    messages_to_send = []

    try:
        feeds = store.feeds.get_all()
        current_time = time.time()

        if not feeds:
            return messages_to_send

        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)

        async def limited_fetch(feed_url: str, feed_data: dict):
            """Fetch a single feed with concurrency limit and random delay."""
            # 增加 0 到 5 秒的随机延迟，避免瞬间并发过高导致超时
            await asyncio.sleep(random.uniform(0, 5.0))
            async with semaphore:
                return await fetch_single_feed(feed_url, feed_data, current_time, http_client)

        # Create tasks for all feeds
        tasks = [
            limited_fetch(feed_url, feed_data)
            for feed_url, feed_data in feeds.items()
        ]

        logger.info(f"Starting concurrent fetch of {len(tasks)} feeds (max {MAX_CONCURRENT_FEEDS} concurrent)")

        # Execute all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all messages
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Feed fetch error: {result}")
            elif result:
                messages_to_send.extend(result)

    except Exception as e:
        logger.error(f"Global feed check error: {e}")

    return messages_to_send


async def check_updates(context):
    """
    Async job callback. Fetches feeds and sends messages.
    """
    # Clean up old message history records first
    try:
        store.messages.cleanup_old()
    except Exception as e:
        logger.error(f"Error cleaning up old messages: {e}")

    # Get RSS HTTP client from bot_data
    http_client = context.bot_data.get('rss_http_client')
    if not http_client:
        logger.error("RSS HTTP client not initialized in bot_data")
        return

    # Fetch feeds asynchronously
    messages = await fetch_and_process_feeds(http_client)

    # Send messages asynchronously
    for chat_id, text in messages:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            # Add small delay to avoid hitting rate limits
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")

    if messages:
        logger.info(f"Sent {len(messages)} messages to subscribers")
