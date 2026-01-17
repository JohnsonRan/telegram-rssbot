import feedparser
import time
import asyncio
import logging
from sqlalchemy.orm import Session
from database import SessionLocal, Feed, SeenEntry, Chat, subscription_table
from datetime import datetime

logger = logging.getLogger(__name__)

def fetch_and_process_feeds():
    """
    Synchronous function to fetch feeds and update database.
    Returns a list of (chat_id, message) tuples to send.
    """
    session: Session = SessionLocal()
    messages_to_send = []
    
    try:
        feeds = session.query(Feed).all()
        for feed in feeds:
            try:
                # Fetch feed
                d = feedparser.parse(feed.url, etag=feed.last_etag, modified=feed.last_modified)
                
                # Check status
                if hasattr(d, 'status'):
                    if d.status == 304:
                        continue
                
                # Update feed metadata
                if hasattr(d, 'etag'): feed.last_etag = d.etag
                if hasattr(d, 'modified'): feed.last_modified = d.modified
                if hasattr(d.feed, 'title'): feed.title = d.feed.title
                
                # Check entries
                new_entries = []
                for entry in d.entries:
                    link = entry.get('link', '')
                    if not link: continue
                    
                    # Check duplication
                    if session.query(SeenEntry).filter_by(feed_id=feed.id, entry_link=link).first():
                        continue
                    
                    # New entry
                    published = None
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        published = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                    
                    seen = SeenEntry(feed_id=feed.id, entry_link=link, published_date=published)
                    session.add(seen)
                    new_entries.append(entry)
                
                if new_entries:
                    # Prepare messages
                    chats = feed.chats
                    for entry in reversed(new_entries):
                        title = entry.get('title', 'No Title')
                        link = entry.get('link', '')
                        text = f"<b>{title}</b>\n{link}"
                        
                        for chat in chats:
                            messages_to_send.append((chat.id, text))
                            
            except Exception as e:
                logger.error(f"Error processing feed {feed.url}: {e}")
                
        session.commit()
    except Exception as e:
        logger.error(f"Global feed check error: {e}")
    finally:
        session.close()
        
    return messages_to_send

async def check_updates(context):
    """
    Async job callback. Runs the sync fetcher in a thread and sends messages.
    """
    # Run sync fetch in a separate thread to avoid blocking the asyncio loop
    messages = await asyncio.to_thread(fetch_and_process_feeds)
    
    # Send messages asynchronously
    for chat_id, text in messages:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            # Add small delay to avoid hitting limits if many messages
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
