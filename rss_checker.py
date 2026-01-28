import feedparser
import asyncio
import logging
import time
import calendar
import httpx

from data_store import store

logger = logging.getLogger(__name__)

# Configuration
MAX_ENTRIES_PER_UPDATE = 10  # Maximum entries to push per feed per check
FEED_TIMEOUT = 10.0  # Timeout for fetching each feed (seconds)


async def fetch_and_process_feeds():
    """
    Fetch all feeds and check for new entries based on publish time.
    Returns a list of (chat_id, message) tuples to send.
    """
    messages_to_send = []
    
    try:
        feeds = store.get_all_feeds()
        current_time = time.time()
        
        for feed_url, feed_data in feeds.items():
            try:
                # Fetch feed with timeout using httpx
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
                }
                
                # Add etag/modified headers if available
                if feed_data.get('last_etag'):
                    headers['If-None-Match'] = feed_data['last_etag']
                if feed_data.get('last_modified'):
                    headers['If-Modified-Since'] = feed_data['last_modified']
                
                # Fetch with timeout
                async with httpx.AsyncClient(timeout=FEED_TIMEOUT, follow_redirects=True) as client:
                    try:
                        response = await client.get(feed_url, headers=headers)
                    except httpx.TimeoutException:
                        logger.warning(f"Timeout fetching {feed_url}")
                        continue
                    except httpx.HTTPError as e:
                        logger.error(f"HTTP error fetching {feed_url}: {e}")
                        continue
                
                # Check if feed was modified (304 Not Modified)
                if response.status_code == 304:
                    # Not modified, skip
                    continue
                
                # Check for errors
                if response.status_code >= 400:
                    logger.warning(f"HTTP {response.status_code} for {feed_url}")
                    continue
                
                # Parse feed content
                d = feedparser.parse(response.content)
                
                # Update feed metadata
                etag = response.headers.get('ETag')
                modified = response.headers.get('Last-Modified')
                title = d.feed.get('title', None)
                
                store.update_feed_metadata(feed_url, etag=etag, modified=modified, title=title)
                
                # Get last check time
                last_check_time = feed_data.get('last_check_time')
                
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
                    
                    # If no time available or this is first check (no last_check_time), add entry
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
                store.update_last_check_time(feed_url, current_time)
                
                # Prepare messages for subscribers
                if new_entries:
                    subscribers = store.get_feed_subscribers(feed_url)
                    
                    # Limit the number of entries to push to avoid spam
                    entries_to_push = new_entries[:MAX_ENTRIES_PER_UPDATE] if len(new_entries) > MAX_ENTRIES_PER_UPDATE else new_entries
                    
                    # Combine all entries into one message
                    if entries_to_push:
                        # Get feed title
                        feed_title = feed_data.get('title', feed_url)
                        
                        # Build message with multiple entries
                        message_lines = [f"<b>{feed_title}</b>", ""]
                        
                        # Add entries in chronological order (oldest first)
                        for entry in reversed(entries_to_push):
                            title = entry.get('title', 'No Title')
                            link = entry.get('link', '')
                            if link:
                                message_lines.append(f"• <b><a href=\"{link}\">{title}</a></b>")
                            else:
                                message_lines.append(f"• <b>{title}</b>")
                            message_lines.append("")  # Empty line between entries
                        
                        # Remove last empty line
                        if message_lines[-1] == "":
                            message_lines.pop()
                        
                        combined_message = "\n".join(message_lines)
                        
                        # Send one message per subscriber
                        for chat_id in subscribers:
                            messages_to_send.append((chat_id, combined_message))
                    
                    if len(new_entries) > MAX_ENTRIES_PER_UPDATE:
                        logger.info(f"Found {len(new_entries)} new entries in {feed_url}, pushing {MAX_ENTRIES_PER_UPDATE}, skipping {len(new_entries) - MAX_ENTRIES_PER_UPDATE}")
                    else:
                        logger.info(f"Found {len(new_entries)} new entries in {feed_url} for {len(subscribers)} subscribers")
            
            except Exception as e:
                logger.error(f"Error processing feed {feed_url}: {e}")
        
    except Exception as e:
        logger.error(f"Global feed check error: {e}")
    
    return messages_to_send


async def check_updates(context):
    """
    Async job callback. Fetches feeds and sends messages.
    """
    # Fetch feeds asynchronously
    messages = await fetch_and_process_feeds()
    
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
