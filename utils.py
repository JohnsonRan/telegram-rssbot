import asyncio
import calendar
import hashlib
import logging
import os
import time
from functools import wraps
from typing import Iterable, Optional

import httpx
from telegram import ChatMember, Update
from telegram.ext import ContextTypes, filters

logger = logging.getLogger(__name__)

# Shared feed fetch configuration
FEED_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
FEED_REQUEST_TIMEOUT = 10.0
FEED_FETCH_RETRIES = 2
FEED_RETRY_BACKOFF_SECONDS = 0.6
FEED_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
ENTRY_TIME_FIELDS = ("published_parsed", "updated_parsed", "created_parsed")


def build_feed_request_headers(last_etag: Optional[str] = None, last_modified: Optional[str] = None) -> dict:
    """Build common HTTP headers used by feed fetching workflows."""
    headers = {'User-Agent': FEED_USER_AGENT}
    if last_etag:
        headers['If-None-Match'] = last_etag
    if last_modified:
        headers['If-Modified-Since'] = last_modified
    return headers


async def fetch_feed_with_retry(http_client: httpx.AsyncClient, url: str, headers: Optional[dict] = None) -> httpx.Response:
    """Fetch feed with a small retry budget for transient network/server failures."""
    last_error = None
    total_attempts = FEED_FETCH_RETRIES + 1

    for attempt in range(total_attempts):
        attempt_no = attempt + 1
        try:
            response = await http_client.get(url, headers=headers, timeout=FEED_REQUEST_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt >= FEED_FETCH_RETRIES:
                logger.warning(
                    "Feed fetch failed after %s/%s attempts for %s: %s",
                    attempt_no,
                    total_attempts,
                    url,
                    exc,
                )
                raise

            wait_seconds = FEED_RETRY_BACKOFF_SECONDS * (2 ** attempt)
            logger.info(
                "Feed fetch attempt %s/%s failed for %s (%s). Retrying in %.2fs",
                attempt_no,
                total_attempts,
                url,
                type(exc).__name__,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            continue

        if response.status_code in FEED_RETRYABLE_STATUS_CODES and attempt < FEED_FETCH_RETRIES:
            wait_seconds = FEED_RETRY_BACKOFF_SECONDS * (2 ** attempt)
            logger.info(
                "Feed fetch attempt %s/%s got retryable HTTP %s for %s. Retrying in %.2fs",
                attempt_no,
                total_attempts,
                response.status_code,
                url,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            continue

        if attempt_no > 1:
            logger.info(
                "Feed fetch succeeded on attempt %s/%s for %s",
                attempt_no,
                total_attempts,
                url,
            )

        return response

    if last_error:
        raise last_error

    raise RuntimeError(f"Failed to fetch feed: {url}")


def normalize_multiline_text(text: str, joiner: str = " ") -> str:
    """Collapse multi-line text into a single normalized string."""
    parts = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return joiner.join(parts)


def normalize_entry_title(entry) -> str:
    """Return a safe, normalized title for a feed entry."""
    title = normalize_multiline_text(entry.get("title") or "No Title", joiner=" ").strip()
    return title or "No Title"


def extract_entry_time(entry) -> Optional[int]:
    """Extract a UTC timestamp from a parsed feed entry if one exists."""
    for time_field in ENTRY_TIME_FIELDS:
        time_struct = entry.get(time_field) or getattr(entry, time_field, None)
        if not time_struct:
            continue

        try:
            return calendar.timegm(time_struct)
        except (ValueError, OverflowError, TypeError):
            continue

    return None


def get_latest_entry_time(entries: Iterable) -> Optional[int]:
    """Return the newest parsed entry timestamp from a feed payload."""
    latest_entry_time = None

    for entry in entries:
        entry_time = extract_entry_time(entry)
        if entry_time is None:
            continue

        if latest_entry_time is None or entry_time > latest_entry_time:
            latest_entry_time = entry_time

    return latest_entry_time


def init_feed_check_time(store, url: str) -> None:
    """Set last_check_time for a feed to now, preventing history from being pushed."""
    store.feeds.update(url, last_check_time=time.time())


def _extract_entry_content(entry) -> str:
    content_items = entry.get("content") or []
    if isinstance(content_items, list):
        for item in content_items:
            if isinstance(item, dict) and item.get("value"):
                return item["value"]

    return entry.get("summary") or entry.get("description") or ""


def build_entry_key(entry) -> str:
    """Build a stable key for a feed entry across fetches."""
    for field in ("id", "guid", "link"):
        value = normalize_multiline_text(entry.get(field) or "", joiner="").strip()
        if value:
            return f"{field}:{value}"

    title = normalize_entry_title(entry)
    published_text = normalize_multiline_text(entry.get("published") or entry.get("updated") or "", joiner=" ").strip()
    content = normalize_multiline_text(_extract_entry_content(entry), joiner=" ").strip()

    fingerprint = "\x1f".join((title[:512], published_text[:256], content[:2048]))
    digest = hashlib.sha256(fingerprint.encode("utf-8", "ignore")).hexdigest()
    return f"hash:{digest}"


def get_whitelist():
    """Get whitelist from environment variable."""
    wl = os.getenv("WHITE_LIST", "")
    if not wl:
        return []
    return [int(x.strip()) for x in wl.split(",") if x.strip().isdigit()]


def require_permission(func):
    """Decorator to check permissions before executing handler."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        chat = update.effective_chat

        if not user:
            return

        # Private Chat: Whitelist check
        if chat.type == 'private':
            whitelist = get_whitelist()
            if user.id not in whitelist:
                await update.message.reply_text(Messages.UNAUTHORIZED)
                logger.warning(f"Unauthorized access by {user.id} ({user.username}) in private chat.")
                return

        # Group/Channel: Admin check
        elif chat.type in ['group', 'supergroup', 'channel']:
            try:
                member = await chat.get_member(user.id)
                if member.status not in [ChatMember.OWNER, ChatMember.ADMINISTRATOR]:
                    await update.message.reply_text(Messages.ADMIN_ONLY)
                    return
            except Exception as e:
                logger.error(f"Error checking permissions: {e}")
                return

        # Execute the handler
        return await func(update, context, *args, **kwargs)

    return wrapper


class AdminFilter(filters.MessageFilter):
    """Custom filter to check if user has admin permissions."""

    def __init__(self):
        super().__init__()
        self.name = "AdminFilter"

    async def filter(self, message):
        """Check if user has permission to use the command."""
        user = message.from_user
        chat = message.chat

        if not user:
            return False

        # Private Chat: Whitelist check
        if chat.type == 'private':
            whitelist = get_whitelist()
            return user.id in whitelist

        # Group/Channel: Admin check
        if chat.type in ['group', 'supergroup', 'channel']:
            try:
                member = await chat.get_member(user.id)
                return member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR]
            except Exception as e:
                logger.error(f"Error checking permissions: {e}")
                return False

        return False


# Create filter instance
admin_filter = AdminFilter()


class Messages:
    """Centralized message text constants."""

    # Permission messages
    UNAUTHORIZED = "You are not authorized to use this bot."
    ADMIN_ONLY = "Only admins can configure the bot."

    # Start/Help messages
    WELCOME_NEW = "Hello! I'm your RSS Bot. Use /add <url> to subscribe to a feed."
    WELCOME_BACK = "Welcome back! Use /list to see your feeds."

    # Add feed messages
    USAGE_ADD = "Usage: /add <url>"
    CHECKING_FEED = "Checking {}..."
    INVALID_FEED = "Invalid RSS feed or could not fetch."
    ALREADY_SUBSCRIBED = "You are already subscribed to this feed."
    SUBSCRIBED = "Subscribed to <b>{}</b>!"

    # List feeds messages
    NO_SUBSCRIPTIONS = "No subscriptions found."
    SUBSCRIBED_FEEDS_HEADER = "<b>Subscribed Feeds:</b>\n\n"

    # Remove feed messages
    USAGE_REMOVE = "Usage: /remove <url>"
    NOT_SUBSCRIBED = "You are not subscribed to this URL."
    UNSUBSCRIBED = "Unsubscribed from {}"

    # Export/Import messages
    NOTHING_TO_EXPORT = "Nothing to export."
    EXPORT_CAPTION = "Here is your OPML export."
    IMPORT_USAGE = "Please reply to an OPML file with /import command."
    IMPORT_INVALID_FILE = "Please reply to a .opml or .xml file."
    IMPORT_NO_FEEDS = "No feeds found in file."
    IMPORTING = "Importing {} feeds..."
    IMPORT_SUCCESS = "Imported {} new feeds."
    IMPORT_FAILED = "Failed to process OPML file."

    # Error messages
    ERROR_OCCURRED = "An error occurred."
    ERROR_PROCESSING = "Error processing request."
