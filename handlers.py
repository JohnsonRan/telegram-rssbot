import logging
import asyncio
import os
import time
from telegram import Update, Document, ChatMember
from telegram.ext import ContextTypes
from sqlalchemy.orm import Session
from database import SessionLocal, Chat, Feed, SeenEntry
from datetime import datetime
import feedparser
import listparser
from io import BytesIO

logger = logging.getLogger(__name__)

def get_whitelist():
    wl = os.getenv("WHITE_LIST", "")
    if not wl: return []
    return [int(x.strip()) for x in wl.split(",") if x.strip().isdigit()]

async def check_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    
    if not user:
        return False

    # Private Chat: Whitelist check
    if chat.type == 'private':
        whitelist = get_whitelist()
        if user.id not in whitelist:
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
            logger.warning(f"Unauthorized access attempt by {user.id} ({user.username}) in private chat.")
            return False
        return True
    
    # Group/Channel: Admin check
    if chat.type in ['group', 'supergroup', 'channel']:
        try:
            member = await chat.get_member(user.id)
            if member.status not in [ChatMember.OWNER, ChatMember.ADMINISTRATOR]:
                await update.message.reply_text("⛔ Only admins can configure the bot.")
                return False
            return True
        except Exception as e:
            logger.error(f"Error checking permissions: {e}")
            # Fail safe
            return False

    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permissions(update, context): return

    chat_id = update.effective_chat.id
    title = update.effective_chat.title or update.effective_chat.username or str(chat_id)
    
    session: Session = SessionLocal()
    try:
        chat = session.query(Chat).filter_by(id=chat_id).first()
        if not chat:
            chat = Chat(id=chat_id, title=title)
            session.add(chat)
            session.commit()
            await update.message.reply_text("Hello! I'm your RSS Bot. Use /add <url> to subscribe to a feed.")
        else:
            await update.message.reply_text("Welcome back! Use /list to see your feeds.")
    except Exception as e:
        logger.error(f"Error in start: {e}")
        await update.message.reply_text("An error occurred.")
    finally:
        session.close()

async def add_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permissions(update, context): return

    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /add <url>")
        return

    url = context.args[0]
    
    # Validate URL check
    msg = await update.message.reply_text(f"Checking {url}...")
    
    # Run feedparser in thread
    try:
        d = await asyncio.to_thread(feedparser.parse, url)
        # Some feeds might not have a title but valid entries, or vice versa. 
        # Check if it has entries or version > 0
        if not d.entries and not d.feed.get('title') and not d.version:
             await msg.edit_text("Invalid RSS feed or could not fetch.")
             return
        
        feed_title = d.feed.get('title', url)
        
        session: Session = SessionLocal()
        try:
            # Check if feed exists
            feed = session.query(Feed).filter_by(url=url).first()
            is_new_feed = feed is None
            
            if not feed:
                feed = Feed(url=url, title=feed_title)
                session.add(feed)
                session.commit()
                session.refresh(feed)  # Get the feed.id
            
            # Check subscription
            chat = session.query(Chat).filter_by(id=chat_id).first()
            if not chat:
                chat = Chat(id=chat_id, title=update.effective_chat.title or str(chat_id))
                session.add(chat)
                session.commit()
            
            if feed not in chat.feeds:
                chat.feeds.append(feed)
                session.commit()
                
                # Mark all existing entries as seen to avoid pushing history
                if d.entries:
                    for entry in d.entries:
                        link = entry.get('link', '')
                        if not link: continue
                        
                        # Check if already in SeenEntry (might exist from other subscriptions)
                        exists = session.query(SeenEntry).filter_by(feed_id=feed.id, entry_link=link).first()
                        if not exists:
                            published = None
                            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                                published = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                            
                            seen = SeenEntry(feed_id=feed.id, entry_link=link, published_date=published)
                            session.add(seen)
                    
                    session.commit()
                    await msg.edit_text(f"Subscribed to <b>{feed_title}</b>! ({len(d.entries)} existing entries marked as read)", parse_mode='HTML')
                else:
                    await msg.edit_text(f"Subscribed to <b>{feed_title}</b>!", parse_mode='HTML')
            else:
                await msg.edit_text("You are already subscribed to this feed.")
                
        finally:
            session.close()
            
    except Exception as e:
        logger.error(f"Error adding feed: {e}")
        await msg.edit_text("Error processing request.")

async def list_feeds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Depending on requirements, maybe everyone can list? 
    # But usually config commands are restricted. Listed is config-related.
    # Let's restrict it too for consistency, or maybe allow viewing.
    # User said "admin operate", usually listing is operation.
    if not await check_permissions(update, context): return

    chat_id = update.effective_chat.id
    session: Session = SessionLocal()
    try:
        chat = session.query(Chat).filter_by(id=chat_id).first()
        if not chat or not chat.feeds:
            await update.message.reply_text("No subscriptions found.")
            return
        
        text = "<b>Subscribed Feeds:</b>\n\n"
        for i, feed in enumerate(chat.feeds, 1):
            text += f"{i}. {feed.title} ({feed.url})\n"
        
        await update.message.reply_text(text, parse_mode='HTML', disable_web_page_preview=True)
    finally:
        session.close()

async def remove_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permissions(update, context): return

    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /remove <url>")
        return
        
    url = context.args[0]
    session: Session = SessionLocal()
    try:
        chat = session.query(Chat).filter_by(id=chat_id).first()
        if not chat:
            await update.message.reply_text("No subscriptions found.")
            return

        feed = session.query(Feed).filter_by(url=url).first()
        if not feed or feed not in chat.feeds:
            await update.message.reply_text("You are not subscribed to this URL.")
            return
            
        chat.feeds.remove(feed)
        session.commit()
        await update.message.reply_text(f"Unsubscribed from {feed.title}")
    finally:
        session.close()

async def export_opml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permissions(update, context): return

    chat_id = update.effective_chat.id
    session: Session = SessionLocal()
    try:
        chat = session.query(Chat).filter_by(id=chat_id).first()
        if not chat or not chat.feeds:
            await update.message.reply_text("Nothing to export.")
            return
        
        opml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<opml version="1.0">\n<head><title>RSS Export</title></head>\n<body>\n'
        for feed in chat.feeds:
            title = feed.title.replace('&', '&amp;').replace('"', '&quot;') if feed.title else "Unknown"
            url = feed.url.replace('&', '&amp;').replace('"', '&quot;')
            opml_content += f'  <outline text="{title}" title="{title}" type="rss" xmlUrl="{url}"/>\n'
        opml_content += '</body>\n</opml>'
        
        file = BytesIO(opml_content.encode('utf-8'))
        file.name = "feeds.opml"
        await update.message.reply_document(document=file, caption="Here is your OPML export.")
        
    finally:
        session.close()

async def import_opml(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permissions(update, context): return

    chat_id = update.effective_chat.id
    
    # Check if user replied to a message with a document
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("Please reply to an OPML file with /import command.")
        return
    
    document: Document = update.message.reply_to_message.document
    
    if not document.file_name.endswith('.opml') and not document.file_name.endswith('.xml'):
        await update.message.reply_text("Please reply to a .opml or .xml file.")
        return
        
    file = await document.get_file()
    content = await file.download_as_bytearray()
    
    try:
        # Convert bytearray to bytes for listparser compatibility
        parsed = listparser.parse(bytes(content))
        if not parsed.feeds:
            await update.message.reply_text("No feeds found in file.")
            return
        
        session: Session = SessionLocal()
        count = 0
        status_msg = await update.message.reply_text(f"Importing {len(parsed.feeds)} feeds...")
        
        chat = session.query(Chat).filter_by(id=chat_id).first()
        if not chat:
            chat = Chat(id=chat_id, title=update.effective_chat.title or "Unknown")
            session.add(chat)
        
        newly_subscribed = []  # Track newly subscribed feeds
        
        for item in parsed.feeds:
            url = item.url
            if not url: continue
            
            feed = session.query(Feed).filter_by(url=url).first()
            if not feed:
                feed = Feed(url=url, title=item.title or url)
                session.add(feed)
            
            if feed not in chat.feeds:
                chat.feeds.append(feed)
                count += 1
                newly_subscribed.append((feed, url))
        
        session.commit()
        
        # Mark existing entries as seen for newly subscribed feeds
        entries_marked = 0
        for feed, url in newly_subscribed:
            try:
                d = await asyncio.to_thread(feedparser.parse, url)
                if d.entries:
                    for entry in d.entries:
                        link = entry.get('link', '')
                        if not link: continue
                        
                        exists = session.query(SeenEntry).filter_by(feed_id=feed.id, entry_link=link).first()
                        if not exists:
                            published = None
                            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                                published = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                            
                            seen = SeenEntry(feed_id=feed.id, entry_link=link, published_date=published)
                            session.add(seen)
                            entries_marked += 1
            except Exception as e:
                logger.error(f"Error fetching feed {url} during import: {e}")
        
        session.commit()
        session.close()
        
        if entries_marked > 0:
            await status_msg.edit_text(f"Imported {count} new feeds ({entries_marked} existing entries marked as read).")
        else:
            await status_msg.edit_text(f"Imported {count} new feeds.")
        
    except Exception as e:
        logger.error(f"Import error: {e}")
        await update.message.reply_text("Failed to process OPML file.")

