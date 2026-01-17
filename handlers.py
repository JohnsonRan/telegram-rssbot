import logging
import asyncio
import time
from telegram import Update, Document
from telegram.ext import ContextTypes
from datetime import datetime
import feedparser
import listparser
from io import BytesIO

from data_store import store
from utils import require_permission, mark_feed_entries_as_seen, Messages

logger = logging.getLogger(__name__)


@require_permission
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start and /help commands."""
    chat_id = update.effective_chat.id
    title = update.effective_chat.title or update.effective_chat.username or str(chat_id)
    
    try:
        chat = store.get_chat(chat_id)
        if not chat:
            store.add_chat(chat_id, title)
            await update.message.reply_text(Messages.WELCOME_NEW)
        else:
            await update.message.reply_text(Messages.WELCOME_BACK)
    except Exception as e:
        logger.error(f"Error in start: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


@require_permission
async def add_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add command to subscribe to a feed."""
    chat_id = update.effective_chat.id
    
    if not context.args:
        await update.message.reply_text(Messages.USAGE_ADD)
        return
    
    url = context.args[0]
    msg = await update.message.reply_text(Messages.CHECKING_FEED.format(url))
    
    try:
        # Validate feed by parsing it
        d = await asyncio.to_thread(feedparser.parse, url)
        
        # Check if feed is valid
        if not d.entries and not d.feed.get('title') and not d.version:
            await msg.edit_text(Messages.INVALID_FEED)
            return
        
        feed_title = d.feed.get('title', url)
        
        # Ensure chat exists
        chat_title = update.effective_chat.title or update.effective_chat.username or str(chat_id)
        store.add_chat(chat_id, chat_title)
        
        # Check if already subscribed
        if store.is_subscribed(chat_id, url):
            await msg.edit_text(Messages.ALREADY_SUBSCRIBED)
            return
        
        # Add feed to database
        store.add_feed(url, title=feed_title)
        
        # Subscribe chat to feed
        store.subscribe(chat_id, url)
        
        # Mark all existing entries as seen to avoid pushing history
        marked_count = mark_feed_entries_as_seen(store, url, d)
        
        if marked_count > 0:
            await msg.edit_text(
                Messages.SUBSCRIBED_WITH_COUNT.format(feed_title, marked_count),
                parse_mode='HTML'
            )
        else:
            await msg.edit_text(
                Messages.SUBSCRIBED.format(feed_title),
                parse_mode='HTML'
            )
    
    except Exception as e:
        logger.error(f"Error adding feed: {e}")
        await msg.edit_text(Messages.ERROR_PROCESSING)


@require_permission
async def list_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command to show all subscribed feeds."""
    chat_id = update.effective_chat.id
    
    try:
        feeds = store.get_chat_feeds(chat_id)
        
        if not feeds:
            await update.message.reply_text(Messages.NO_SUBSCRIPTIONS)
            return
        
        text = Messages.SUBSCRIBED_FEEDS_HEADER
        for i, feed in enumerate(feeds, 1):
            text += f"{i}. {feed['title']} ({feed['url']})\n"
        
        await update.message.reply_text(text, parse_mode='HTML', disable_web_page_preview=True)
    
    except Exception as e:
        logger.error(f"Error listing feeds: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


@require_permission
async def remove_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove command to unsubscribe from a feed."""
    chat_id = update.effective_chat.id
    
    if not context.args:
        await update.message.reply_text(Messages.USAGE_REMOVE)
        return
    
    url = context.args[0]
    
    try:
        # Check if subscribed
        if not store.is_subscribed(chat_id, url):
            await update.message.reply_text(Messages.NOT_SUBSCRIBED)
            return
        
        # Get feed info before unsubscribing
        feed = store.get_feed(url)
        feed_title = feed['title'] if feed else url
        
        # Unsubscribe
        store.unsubscribe(chat_id, url)
        await update.message.reply_text(Messages.UNSUBSCRIBED.format(feed_title))
    
    except Exception as e:
        logger.error(f"Error removing feed: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


@require_permission
async def export_opml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /export command to export feeds as OPML."""
    chat_id = update.effective_chat.id
    
    try:
        feeds = store.get_chat_feeds(chat_id)
        
        if not feeds:
            await update.message.reply_text(Messages.NOTHING_TO_EXPORT)
            return
        
        # Generate OPML
        opml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<opml version="1.0">\n<head><title>RSS Export</title></head>\n<body>\n'
        
        for feed in feeds:
            title = feed['title'].replace('&', '&amp;').replace('"', '&quot;')
            url = feed['url'].replace('&', '&amp;').replace('"', '&quot;')
            opml_content += f'  <outline text="{title}" title="{title}" type="rss" xmlUrl="{url}"/>\n'
        
        opml_content += '</body>\n</opml>'
        
        # Send as file
        file = BytesIO(opml_content.encode('utf-8'))
        file.name = "feeds.opml"
        await update.message.reply_document(document=file, caption=Messages.EXPORT_CAPTION)
    
    except Exception as e:
        logger.error(f"Error exporting OPML: {e}")
        await update.message.reply_text(Messages.ERROR_OCCURRED)


@require_permission
async def import_opml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /import command to import feeds from OPML."""
    chat_id = update.effective_chat.id
    
    # Check if user replied to a document
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text(Messages.IMPORT_USAGE)
        return
    
    document: Document = update.message.reply_to_message.document
    
    if not document.file_name.endswith('.opml') and not document.file_name.endswith('.xml'):
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
        store.add_chat(chat_id, chat_title)
        
        status_msg = await update.message.reply_text(Messages.IMPORTING.format(len(parsed.feeds)))
        
        newly_subscribed = []
        subscribed_count = 0
        
        # Import feeds
        for item in parsed.feeds:
            url = item.url
            if not url:
                continue
            
            # Add feed
            store.add_feed(url, title=item.title or url)
            
            # Subscribe if not already
            if store.subscribe(chat_id, url):
                subscribed_count += 1
                newly_subscribed.append(url)
        
        # Mark existing entries as seen for newly subscribed feeds
        total_entries_marked = 0
        
        for url in newly_subscribed:
            try:
                d = await asyncio.to_thread(feedparser.parse, url)
                marked_count = mark_feed_entries_as_seen(store, url, d)
                total_entries_marked += marked_count
            except Exception as e:
                logger.error(f"Error fetching feed {url} during import: {e}")
        
        # Send result
        if total_entries_marked > 0:
            await status_msg.edit_text(
                Messages.IMPORT_SUCCESS_WITH_ENTRIES.format(subscribed_count, total_entries_marked)
            )
        else:
            await status_msg.edit_text(Messages.IMPORT_SUCCESS.format(subscribed_count))
    
    except Exception as e:
        logger.error(f"Import error: {e}")
        await update.message.reply_text(Messages.IMPORT_FAILED)
