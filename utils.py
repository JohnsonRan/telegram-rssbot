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


def get_entry_id(entry):
    """
    Get a unique identifier for an RSS entry.
    
    Priority:
    1. id/guid field (most reliable, e.g., kernel.org uses guid)
    2. link field (traditional approach)
    3. title + published combination (last resort)
    
    Args:
        entry: feedparser entry object
        
    Returns:
        str: Unique identifier for the entry
    """
    # Priority 1: Use id/guid (feedparser normalizes guid to id)
    entry_id = entry.get('id', '').strip()
    if entry_id:
        return entry_id
    
    # Priority 2: Use link (traditional RSS)
    link = entry.get('link', '').strip()
    if link:
        return link
    
    # Priority 3: Fallback to title + published
    title = entry.get('title', 'untitled').strip()
    published = entry.get('published', entry.get('updated', '')).strip()
    
    if published:
        return f"{title}::{published}"
    
    # Last resort: just title (may cause duplicates)
    return title


def init_feed_check_time(store, feed_url: str):
    """
    Initialize the last check time for a newly added feed to current time.
    This prevents pushing historical content.
    
    Args:
        store: DataStore instance
        feed_url: URL of the feed
    """
    import time
    current_time = time.time()
    store.update_last_check_time(feed_url, current_time)
    logger.info(f"Initialized check time for {feed_url}")



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
