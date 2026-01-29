import logging
import asyncio
import time
from telegram import Update, Document
from telegram.ext import ContextTypes
from datetime import datetime
import feedparser
import listparser
from io import BytesIO
import httpx

from data_store import store
from utils import require_permission, Messages

logger = logging.getLogger(__name__)


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
        # Use custom User-Agent to avoid 403 errors from some RSS sources
        d = await asyncio.to_thread(
            feedparser.parse, 
            url,
            agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
        )
        
        # Check HTTP status if available
        status = getattr(d, 'status', None)
        if status and status >= 400:
            # Delete the "Checking..." message and send http.cat image only
            await msg.delete()
            try:
                await update.message.reply_photo(photo=f"https://http.cat/{status}")
            except Exception:
                # Fallback to simple text if image fails
                await update.message.reply_text(f"HTTP {status} 错误")
            return
        
        # Check if feed is valid
        if not d.entries and not d.feed.get('title') and not d.version:
            error_msg = Messages.INVALID_FEED
            if status:
                error_msg += f" (HTTP {status})"
            # Check if there's a bozo exception (parse error)
            if hasattr(d, 'bozo') and d.bozo and hasattr(d, 'bozo_exception'):
                error_msg += f"\n错误详情: {str(d.bozo_exception)[:100]}"
            await msg.edit_text(error_msg)
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
        
        # Initialize check time to avoid pushing history
        from utils import init_feed_check_time
        init_feed_check_time(store, url)
        
        await msg.edit_text(
            Messages.SUBSCRIBED.format(feed_title),
            parse_mode='HTML'
        )

    
    except Exception as e:
        logger.error(f"Error adding feed: {e}")
        await msg.edit_text(Messages.ERROR_PROCESSING)


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
            text += f"{i}. <a href=\"{feed['url']}\">{feed['title']}</a>\n"
        
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
        
        # Initialize check time for newly subscribed feeds
        from utils import init_feed_check_time
        
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


async def nbnhhsh_translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /nbnhhsh command to translate pinyin abbreviations."""
    text_to_translate = None
    
    # Check if user replied to a message
    if update.message.reply_to_message and update.message.reply_to_message.text:
        text_to_translate = update.message.reply_to_message.text
    # Check if user provided text as argument
    elif context.args:
        text_to_translate = ' '.join(context.args)
    else:
        await update.message.reply_text(Messages.NBNHHSH_USAGE)
        return
    
    try:
        # Get HTTP client from bot_data
        http_client = context.bot_data.get('http_client')
        if not http_client:
            await update.message.reply_text("服务暂时不可用，请稍后再试")
            logger.error("HTTP client not initialized in bot_data")
            return
        
        # Call nbnhhsh API
        response = await http_client.post(
            'https://lab.magiconch.com/api/nbnhhsh/guess',
            json={'text': text_to_translate}
        )
        response.raise_for_status()
        data = response.json()
        
        if not data:
            await update.message.reply_text(Messages.NBNHHSH_NO_RESULT.format(text_to_translate))
            return
        
        # Format results
        result_lines = []
        for item in data:
            name = item.get('name', '')
            trans = item.get('trans')
            inputting = item.get('inputting')
            
            if trans:
                # Has translations
                translations = '、'.join(trans[:5])  # Limit to 5 results
                result_lines.append(f"<b>{name}</b>: {translations}")
            elif inputting:
                # Has possible inputs
                inputs = '、'.join(inputting[:5])
                result_lines.append(f"<b>{name}</b>: 可能是 {inputs}")
            else:
                # No result
                result_lines.append(f"<b>{name}</b>: 未找到释义")
        
        if result_lines:
            result_text = '\n\n'.join(result_lines)
            await update.message.reply_text(result_text, parse_mode='HTML')
        else:
            await update.message.reply_text(Messages.NBNHHSH_NO_RESULT.format(text_to_translate))
    
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in nbnhhsh: {e}")
        await update.message.reply_text(Messages.NBNHHSH_ERROR)
    except Exception as e:
        logger.error(f"Error in nbnhhsh: {e}")
        await update.message.reply_text(Messages.NBNHHSH_ERROR)
