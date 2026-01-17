import os
import time
import logging
import asyncio
from functools import wraps
from datetime import datetime
from telegram import Update, ChatMember
from telegram.ext import ContextTypes
import feedparser

logger = logging.getLogger(__name__)


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


def mark_feed_entries_as_seen(store, feed_url: str, feed_data: dict):
    """
    Mark all entries in a feed as seen.
    
    Args:
        store: DataStore instance
        feed_url: URL of the feed
        feed_data: Parsed feedparser data
    """
    if not feed_data.entries:
        return 0
    
    entry_links = []
    for entry in feed_data.entries:
        link = entry.get('link', '')
        if link and not store.is_entry_seen(feed_url, link):
            entry_links.append(link)
    
    if entry_links:
        store.mark_entries_seen(feed_url, entry_links)
        logger.info(f"Marked {len(entry_links)} entries as seen for {feed_url}")
    
    return len(entry_links)


class Messages:
    """Centralized message text constants."""
    
    # Permission messages
    UNAUTHORIZED = "⛔ You are not authorized to use this bot."
    ADMIN_ONLY = "⛔ Only admins can configure the bot."
    
    # Start/Help messages
    WELCOME_NEW = "Hello! I'm your RSS Bot. Use /add <url> to subscribe to a feed."
    WELCOME_BACK = "Welcome back! Use /list to see your feeds."
    
    # Add feed messages
    USAGE_ADD = "Usage: /add <url>"
    CHECKING_FEED = "Checking {}..."
    INVALID_FEED = "Invalid RSS feed or could not fetch."
    ALREADY_SUBSCRIBED = "You are already subscribed to this feed."
    SUBSCRIBED = "Subscribed to <b>{}</b>!"
    SUBSCRIBED_WITH_COUNT = "Subscribed to <b>{}</b>! ({} existing entries marked as read)"
    
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
    IMPORT_SUCCESS_WITH_ENTRIES = "Imported {} new feeds ({} existing entries marked as read)."
    IMPORT_FAILED = "Failed to process OPML file."
    
    # Error messages
    ERROR_OCCURRED = "An error occurred."
    ERROR_PROCESSING = "Error processing request."
