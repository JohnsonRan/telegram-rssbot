import feedparser
import asyncio
import logging
import time
import calendar
import httpx
import random

from data_store import store

logger = logging.getLogger(__name__)

# Configuration
MAX_ENTRIES_PER_UPDATE = 10  # Maximum entries to push per feed per check
FEED_TIMEOUT = 10.0  # Timeout for fetching each feed (seconds)
MAX_CONCURRENT_FEEDS = 10  # Maximum number of feeds to fetch concurrently


async def fetch_single_feed(feed_url: str, feed_data: dict, current_time: float, http_client):
    """
    Fetch and process a single feed.
    Returns a list of (chat_id, message) tuples to send.
    
    Args:
        feed_url: URL of the feed to fetch
        feed_data: Feed metadata from database
        current_time: Current timestamp
        http_client: Shared HTTP client instance
    """
    messages_to_send = []
    
    try:
        # Fetch feed with timeout using shared httpx client
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
        }
        
        # Add etag/modified headers if available
        if feed_data.last_etag:
            headers['If-None-Match'] = feed_data.last_etag
        if feed_data.last_modified:
            headers['If-Modified-Since'] = feed_data.last_modified
        
        # Fetch with timeout using shared client
        try:
            response = await http_client.get(feed_url, headers=headers)
        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching {feed_url}")
            return messages_to_send
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching {feed_url}: {e}")
            return messages_to_send
        
        # Check if feed was modified (304 Not Modified)
        if response.status_code == 304:
            # Not modified, skip
            return messages_to_send
        
        # Check for errors
        if response.status_code >= 400:
            logger.warning(f"HTTP {response.status_code} for {feed_url}")
            return messages_to_send
        
        # Parse feed content
        d = await asyncio.to_thread(feedparser.parse, response.content)
        
        # Update feed metadata
        etag = response.headers.get('ETag')
        modified = response.headers.get('Last-Modified')
        title = d.feed.get('title', None)
        
        store.feeds.update(feed_url, last_etag=etag, last_modified=modified, title=title)
        
        # Get last check time
        last_check_time = feed_data.last_check_time
        
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
        store.feeds.update(feed_url, last_check_time=current_time)
        
        # Prepare messages for subscribers
        if new_entries:
            subscribers = store.subscriptions.get_feed_subscribers(feed_url)
            
            # Limit the number of entries to push to avoid spam
            entries_to_push = new_entries[:MAX_ENTRIES_PER_UPDATE] if len(new_entries) > MAX_ENTRIES_PER_UPDATE else new_entries
            
            # Get feed title
            feed_title = feed_data.title if feed_data.title else feed_url
            
            # Process each subscriber separately for deduplication
            total_filtered = 0
            for chat_id in subscribers:
                # Filter entries for this subscriber (check for duplicates)
                filtered_entries = []
                for entry in reversed(entries_to_push):  # Oldest first
                    title = entry.get('title', 'No Title')
                    
                    # Check if this message was already sent to this chat in the past hour
                    if not store.messages.is_duplicate(title, feed_url, chat_id):
                        filtered_entries.append(entry)
                        # Record this message for deduplication
                        store.messages.record(title, feed_url, chat_id)
                    else:
                        total_filtered += 1
                        logger.debug(f"Skipping duplicate message '{title}' for chat {chat_id}")
                
                # Build and send message if there are non-duplicate entries
                if filtered_entries:
                    message_lines = [f"<b>{feed_title}</b>", ""]
                    
                    for entry in filtered_entries:
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
                    messages_to_send.append((chat_id, combined_message))
            
            if total_filtered > 0:
                logger.info(f"Filtered {total_filtered} duplicate messages from {feed_url}")
            
            if len(new_entries) > MAX_ENTRIES_PER_UPDATE:
                logger.info(f"Found {len(new_entries)} new entries in {feed_url}, pushing {MAX_ENTRIES_PER_UPDATE}, skipping {len(new_entries) - MAX_ENTRIES_PER_UPDATE}")
            else:
                logger.info(f"Found {len(new_entries)} new entries in {feed_url} for {len(subscribers)} subscribers")
    
    except Exception as e:
        logger.error(f"Error processing feed {feed_url}: {e}")
    
    return messages_to_send


async def fetch_and_process_feeds(http_client):
    """
    Fetch all feeds concurrently and check for new entries based on publish time.
    Returns a list of (chat_id, message) tuples to send.
    
    Args:
        http_client: Shared HTTP client instance for fetching feeds
    """
    messages_to_send = []
    
    try:
        feeds = store.feeds.get_all()
        current_time = time.time()
        
        if not feeds:
            return messages_to_send
        
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)
        
        async def limited_fetch(feed_url: str, feed_data: dict):
            """Fetch a single feed with concurrency limit and random delay."""
            # 增加 0 到 5 秒的随机延迟，避免瞬间并发过高导致超时
            await asyncio.sleep(random.uniform(0, 5.0))
            async with semaphore:
                return await fetch_single_feed(feed_url, feed_data, current_time, http_client)
        
        # Create tasks for all feeds
        tasks = [
            limited_fetch(feed_url, feed_data) 
            for feed_url, feed_data in feeds.items()
        ]
        
        logger.info(f"Starting concurrent fetch of {len(tasks)} feeds (max {MAX_CONCURRENT_FEEDS} concurrent)")
        
        # Execute all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect all messages
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Feed fetch error: {result}")
            elif result:
                messages_to_send.extend(result)
        
    except Exception as e:
        logger.error(f"Global feed check error: {e}")
    
    return messages_to_send


async def check_updates(context):
    """
    Async job callback. Fetches feeds and sends messages.
    """
    # Clean up old message history records first
    try:
        store.messages.cleanup_old()
    except Exception as e:
        logger.error(f"Error cleaning up old messages: {e}")
    
    # Get RSS HTTP client from bot_data
    http_client = context.bot_data.get('rss_http_client')
    if not http_client:
        logger.error("RSS HTTP client not initialized in bot_data")
        return
    
    # Fetch feeds asynchronously
    messages = await fetch_and_process_feeds(http_client)
    
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
