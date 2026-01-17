import feedparser
import asyncio
import logging

from data_store import store

logger = logging.getLogger(__name__)


def fetch_and_process_feeds():
    """
    Fetch all feeds and check for new entries.
    Returns a list of (chat_id, message) tuples to send.
    """
    messages_to_send = []
    
    try:
        feeds = store.get_all_feeds()
        
        for feed_url, feed_data in feeds.items():
            try:
                # Fetch feed with etag/modified for efficient updates
                d = feedparser.parse(
                    feed_url,
                    etag=feed_data.get('last_etag'),
                    modified=feed_data.get('last_modified')
                )
                
                # Check if feed was modified
                if hasattr(d, 'status') and d.status == 304:
                    # Not modified, skip
                    continue
                
                # Update feed metadata
                etag = getattr(d, 'etag', None)
                modified = getattr(d, 'modified', None)
                title = d.feed.get('title', None)
                
                store.update_feed_metadata(feed_url, etag=etag, modified=modified, title=title)
                
                # Check for new entries
                new_entries = []
                
                for entry in d.entries:
                    link = entry.get('link', '')
                    if not link:
                        continue
                    
                    # Check if we've seen this entry before
                    if store.is_entry_seen(feed_url, link):
                        continue
                    
                    # New entry! Mark as seen
                    store.mark_entry_seen(feed_url, link)
                    new_entries.append(entry)
                
                # Prepare messages for subscribers
                if new_entries:
                    subscribers = store.get_feed_subscribers(feed_url)
                    
                    # Send entries in chronological order (oldest first)
                    for entry in reversed(new_entries):
                        title = entry.get('title', 'No Title')
                        link = entry.get('link', '')
                        text = f"<b>{title}</b>\n{link}"
                        
                        for chat_id in subscribers:
                            messages_to_send.append((chat_id, text))
                    
                    logger.info(f"Found {len(new_entries)} new entries in {feed_url} for {len(subscribers)} subscribers")
            
            except Exception as e:
                logger.error(f"Error processing feed {feed_url}: {e}")
        
    except Exception as e:
        logger.error(f"Global feed check error: {e}")
    
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
            # Add small delay to avoid hitting rate limits
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
    
    if messages:
        logger.info(f"Sent {len(messages)} messages to subscribers")
