import asyncio
import logging
from html import escape
from io import BytesIO

import feedparser
import httpx
import listparser
from telegram import Document, Update
from telegram.ext import ContextTypes

from data_store import Chat, Feed, store
from utils import (
    FEED_REQUEST_TIMEOUT,
    Messages,
    build_feed_request_headers,
    fetch_feed_with_retry,
    init_feed_check_time,
)

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start and /help commands."""
    chat_id = update.effective_chat.id
    title = update.effective_chat.title or update.effective_chat.username or str(chat_id)

    try:
        chat = store.chats.get(chat_id)
        if not chat:
            store.chats.create(Chat(id=chat_id, title=title))
            await update.message.reply_text(Messages.WELCOME_NEW)
        else:
            await update.message.reply_text(Messages.WELCOME_BACK)
    except Exception as e:
        logger.error(f"Error in start: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


async def add_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add command to subscribe to a feed."""
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(Messages.USAGE_ADD)
        return

    url = context.args[0]
    msg = await update.message.reply_text(Messages.CHECKING_FEED.format(url))

    http_client = context.application.bot_data.get('rss_http_client')
    owns_http_client = False
    if not http_client:
        http_client = httpx.AsyncClient(timeout=FEED_REQUEST_TIMEOUT, follow_redirects=True)
        owns_http_client = True

    try:
        headers = build_feed_request_headers()
        response = await fetch_feed_with_retry(http_client, url, headers=headers)

        # Check HTTP status first
        status = response.status_code
        if status >= 400:
            # Delete the "Checking..." message and send http.cat image only
            await msg.delete()
            try:
                await update.message.reply_photo(photo=f"https://http.cat/{status}")
            except Exception:
                # Fallback to simple text if image fails
                await update.message.reply_text(f"HTTP {status} 错误")
            return

        # Validate feed content by parsing response body
        d = await asyncio.to_thread(feedparser.parse, response.content)

        # Check if feed is valid
        if not d.entries and not d.feed.get('title') and not d.version:
            error_msg = Messages.INVALID_FEED
            # Check if there's a bozo exception (parse error)
            if hasattr(d, 'bozo') and d.bozo and hasattr(d, 'bozo_exception'):
                error_msg += f"\n错误详情: {str(d.bozo_exception)[:100]}"
            await msg.edit_text(error_msg)
            return

        feed_title = d.feed.get('title', url)

        # Ensure chat exists
        chat_title = update.effective_chat.title or update.effective_chat.username or str(chat_id)
        store.chats.create(Chat(id=chat_id, title=chat_title))

        # Check if already subscribed
        if store.subscriptions.is_subscribed(chat_id, url):
            await msg.edit_text(Messages.ALREADY_SUBSCRIBED)
            return

        # Add feed to database
        store.feeds.create(Feed(url=url, title=feed_title))

        # Subscribe chat to feed
        store.subscriptions.subscribe(chat_id, url)

        # Initialize check time to avoid pushing history
        init_feed_check_time(store, url)

        safe_feed_title = escape(feed_title)
        await msg.edit_text(
            Messages.SUBSCRIBED.format(safe_feed_title),
            parse_mode='HTML'
        )

    except httpx.TimeoutException:
        logger.warning(f"Timeout validating feed {url}")
        await msg.edit_text(f"{Messages.INVALID_FEED} (请求超时)")
    except httpx.HTTPError as e:
        logger.error(f"HTTP error validating feed {url}: {e}")
        await msg.edit_text(Messages.ERROR_PROCESSING)
    except Exception as e:
        logger.error(f"Error adding feed: {e}")
        await msg.edit_text(Messages.ERROR_PROCESSING)
    finally:
        if owns_http_client:
            await http_client.aclose()


async def list_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command to show all subscribed feeds."""
    chat_id = update.effective_chat.id

    try:
        feeds = store.subscriptions.get_chat_feeds(chat_id)

        if not feeds:
            await update.message.reply_text(Messages.NO_SUBSCRIPTIONS)
            return

        text = Messages.SUBSCRIBED_FEEDS_HEADER
        for i, feed in enumerate(feeds, 1):
            safe_url = escape(feed.url, quote=True)
            safe_title = escape(feed.title or feed.url)
            text += f"{i}. <a href=\"{safe_url}\">{safe_title}</a>\n"

        await update.message.reply_text(text, parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error listing feeds: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


async def remove_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove command to unsubscribe from a feed."""
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(Messages.USAGE_REMOVE)
        return

    url = context.args[0]

    try:
        # Check if subscribed
        if not store.subscriptions.is_subscribed(chat_id, url):
            await update.message.reply_text(Messages.NOT_SUBSCRIBED)
            return

        # Get feed info before unsubscribing
        feed = store.feeds.get(url)
        feed_title = feed.title if feed else url

        # Unsubscribe
        store.subscriptions.unsubscribe(chat_id, url)
        await update.message.reply_text(Messages.UNSUBSCRIBED.format(feed_title))

    except Exception as e:
        logger.error(f"Error removing feed: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


async def export_opml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /export command to export feeds as OPML."""
    chat_id = update.effective_chat.id

    try:
        feeds = store.subscriptions.get_chat_feeds(chat_id)

        if not feeds:
            await update.message.reply_text(Messages.NOTHING_TO_EXPORT)
            return

        # Generate OPML
        opml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<opml version="1.0">\n<head><title>RSS Export</title></head>\n<body>\n'

        for feed in feeds:
            title = feed.title.replace('&', '&amp;').replace('"', '&quot;')
            url = feed.url.replace('&', '&amp;').replace('"', '&quot;')
            opml_content += f'  <outline text="{title}" title="{title}" type="rss" xmlUrl="{url}"/>\n'

        opml_content += '</body>\n</opml>'

        # Send as file
        file = BytesIO(opml_content.encode('utf-8'))
        file.name = "feeds.opml"
        await update.message.reply_document(document=file, caption=Messages.EXPORT_CAPTION)

    except Exception as e:
        logger.error(f"Error exporting OPML: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


async def import_opml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /import command to import feeds from OPML."""
    chat_id = update.effective_chat.id

    # Check if user replied to a document
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text(Messages.IMPORT_USAGE)
        return

    document: Document = update.message.reply_to_message.document

    file_name = (document.file_name or "").lower()
    if not file_name.endswith('.opml') and not file_name.endswith('.xml'):
        await update.message.reply_text(Messages.IMPORT_INVALID_FILE)
        return

    try:
        # Download file
        file = await document.get_file()
        content = await file.download_as_bytearray()

        # Parse OPML
        parsed = listparser.parse(bytes(content))

        if not parsed.feeds:
            await update.message.reply_text(Messages.IMPORT_NO_FEEDS)
            return

        # Ensure chat exists
        chat_title = update.effective_chat.title or update.effective_chat.username or str(chat_id)
        store.chats.create(Chat(id=chat_id, title=chat_title))

        status_msg = await update.message.reply_text(Messages.IMPORTING.format(len(parsed.feeds)))

        newly_subscribed = []
        subscribed_count = 0

        # Import feeds
        for item in parsed.feeds:
            url = item.url
            if not url:
                continue

            # Add feed
            store.feeds.create(Feed(url=url, title=item.title or url))

            # Subscribe if not already
            if store.subscriptions.subscribe(chat_id, url):
                subscribed_count += 1
                newly_subscribed.append(url)

        # Initialize check time for newly subscribed feeds
        for url in newly_subscribed:
            try:
                init_feed_check_time(store, url)
            except Exception as e:
                logger.error(f"Error initializing feed {url} during import: {e}")

        # Send result
        await status_msg.edit_text(Messages.IMPORT_SUCCESS.format(subscribed_count))

    except Exception as e:
        logger.error(f"Import error: {e}")
        await update.message.reply_text(Messages.IMPORT_FAILED)
