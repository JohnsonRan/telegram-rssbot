import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Set
from filelock import FileLock
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class DataStore:
    """JSON-based data storage for RSS bot with file locking for concurrency safety."""
    
    def __init__(self, data_file: str = "bot_data.json"):
        self.data_file = Path(data_file)
        self.lock_file = Path(f"{data_file}.lock")
        self._ensure_file_exists()
    
    def _ensure_file_exists(self):
        """Create data file with initial structure if it doesn't exist."""
        if not self.data_file.exists():
            initial_data = {
                "chats": {},
                "feeds": {}
            }
            self.data_file.write_text(json.dumps(initial_data, indent=2, ensure_ascii=False))
            logger.info(f"Created new data file: {self.data_file}")
    
    @contextmanager
    def _lock(self):
        """Context manager for file locking."""
        lock = FileLock(self.lock_file, timeout=10)
        try:
            with lock:
                yield
        except Exception as e:
            logger.error(f"Lock error: {e}")
            raise
    
    def _load(self) -> dict:
        """Load data from JSON file."""
        try:
            return json.loads(self.data_file.read_text(encoding='utf-8'))
        except Exception as e:
            logger.error(f"Error loading data: {e}")
            return {"chats": {}, "feeds": {}}
    
    def _save(self, data: dict):
        """Save data to JSON file."""
        try:
            self.data_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
        except Exception as e:
            logger.error(f"Error saving data: {e}")
            raise
    
    # Chat operations
    def get_chat(self, chat_id: int) -> Optional[dict]:
        """Get chat by ID."""
        with self._lock():
            data = self._load()
            return data["chats"].get(str(chat_id))
    
    def add_chat(self, chat_id: int, title: str) -> dict:
        """Add or update chat."""
        with self._lock():
            data = self._load()
            chat_key = str(chat_id)
            
            if chat_key not in data["chats"]:
                data["chats"][chat_key] = {
                    "id": chat_id,
                    "title": title,
                    "feeds": []
                }
                self._save(data)
                logger.info(f"Added new chat: {chat_id} ({title})")
            else:
                # Update title if changed
                if data["chats"][chat_key]["title"] != title:
                    data["chats"][chat_key]["title"] = title
                    self._save(data)
            
            return data["chats"][chat_key]
    
    def get_all_chats(self) -> Dict[str, dict]:
        """Get all chats."""
        with self._lock():
            data = self._load()
            return data["chats"]
    
    # Feed operations
    def get_feed(self, url: str) -> Optional[dict]:
        """Get feed by URL."""
        with self._lock():
            data = self._load()
            return data["feeds"].get(url)
    
    def add_feed(self, url: str, title: str = None, etag: str = None, modified: str = None) -> dict:
        """Add or update feed."""
        with self._lock():
            data = self._load()
            
            if url not in data["feeds"]:
                data["feeds"][url] = {
                    "url": url,
                    "title": title or url,
                    "last_etag": etag,
                    "last_modified": modified,
                    "last_check_time": None  # Timestamp of last check
                }
                self._save(data)
                logger.info(f"Added new feed: {url}")
            else:
                # Update metadata
                if title:
                    data["feeds"][url]["title"] = title
                if etag is not None:
                    data["feeds"][url]["last_etag"] = etag
                if modified is not None:
                    data["feeds"][url]["last_modified"] = modified
                self._save(data)
            
            return data["feeds"][url]
    
    def update_feed_metadata(self, url: str, etag: str = None, modified: str = None, title: str = None, last_check_time: float = None):
        """Update feed metadata (etag, modified, title, last_check_time)."""
        with self._lock():
            data = self._load()
            if url in data["feeds"]:
                if etag is not None:
                    data["feeds"][url]["last_etag"] = etag
                if modified is not None:
                    data["feeds"][url]["last_modified"] = modified
                if title is not None:
                    data["feeds"][url]["title"] = title
                if last_check_time is not None:
                    data["feeds"][url]["last_check_time"] = last_check_time
                self._save(data)
    
    def get_all_feeds(self) -> Dict[str, dict]:
        """Get all feeds."""
        with self._lock():
            data = self._load()
            return data["feeds"]
    
    # Subscription operations
    def subscribe(self, chat_id: int, feed_url: str) -> bool:
        """Subscribe chat to feed. Returns True if newly subscribed, False if already subscribed."""
        with self._lock():
            data = self._load()
            chat_key = str(chat_id)
            
            if chat_key not in data["chats"]:
                return False
            
            if feed_url not in data["chats"][chat_key]["feeds"]:
                data["chats"][chat_key]["feeds"].append(feed_url)
                self._save(data)
                logger.info(f"Chat {chat_id} subscribed to {feed_url}")
                return True
            
            return False
    
    def unsubscribe(self, chat_id: int, feed_url: str) -> bool:
        """Unsubscribe chat from feed. Returns True if unsubscribed, False if wasn't subscribed."""
        with self._lock():
            data = self._load()
            chat_key = str(chat_id)
            
            if chat_key not in data["chats"]:
                return False
            
            if feed_url in data["chats"][chat_key]["feeds"]:
                data["chats"][chat_key]["feeds"].remove(feed_url)
                self._save(data)
                logger.info(f"Chat {chat_id} unsubscribed from {feed_url}")
                return True
            
            return False
    
    def is_subscribed(self, chat_id: int, feed_url: str) -> bool:
        """Check if chat is subscribed to feed."""
        with self._lock():
            data = self._load()
            chat_key = str(chat_id)
            
            if chat_key not in data["chats"]:
                return False
            
            return feed_url in data["chats"][chat_key].get("feeds", [])
    
    def get_chat_feeds(self, chat_id: int) -> List[dict]:
        """Get all feeds for a chat."""
        with self._lock():
            data = self._load()
            chat_key = str(chat_id)
            
            if chat_key not in data["chats"]:
                return []
            
            feed_urls = data["chats"][chat_key].get("feeds", [])
            return [data["feeds"][url] for url in feed_urls if url in data["feeds"]]
    
    def get_feed_subscribers(self, feed_url: str) -> List[int]:
        """Get all chat IDs subscribed to a feed."""
        with self._lock():
            data = self._load()
            subscribers = []
            
            for chat_key, chat_data in data["chats"].items():
                if feed_url in chat_data.get("feeds", []):
                    subscribers.append(chat_data["id"])
            
            return subscribers
    
    # Last check time operations
    def get_last_check_time(self, feed_url: str) -> Optional[float]:
        """Get the last check time for a feed."""
        with self._lock():
            data = self._load()
            if feed_url not in data["feeds"]:
                return None
            return data["feeds"][feed_url].get("last_check_time")
    
    def update_last_check_time(self, feed_url: str, timestamp: float):
        """Update the last check time for a feed."""
        with self._lock():
            data = self._load()
            if feed_url in data["feeds"]:
                data["feeds"][feed_url]["last_check_time"] = timestamp
                self._save(data)


# Global instance
store = DataStore()
