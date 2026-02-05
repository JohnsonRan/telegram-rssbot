import sqlite3
import logging
from pathlib import Path
from typing import Optional, List, Dict
from contextlib import contextmanager
from threading import local

logger = logging.getLogger(__name__)


class SQLiteStore:
    """SQLite-based data storage for RSS bot with better concurrency support."""
    
    def __init__(self, db_file: str = "bot_data.db"):
        self.db_file = Path(db_file)
        self._local = local()  # Thread-local storage for connections
        self._init_database()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn'):
            conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
            conn.row_factory = sqlite3.Row  # Enable dict-like access
            # Enable WAL mode for better concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn
    
    @contextmanager
    def _transaction(self):
        """Context manager for transactions."""
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Transaction error: {e}")
            raise
    
    def _init_database(self):
        """Initialize database schema."""
        conn = self._get_connection()
        
        # Create chats table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create feeds table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feeds (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                last_etag TEXT,
                last_modified TEXT,
                last_check_time REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create subscriptions table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER NOT NULL,
                feed_url TEXT NOT NULL,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, feed_url),
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY (feed_url) REFERENCES feeds(url) ON DELETE CASCADE
            )
        """)
        
        # Create index for reverse lookup
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_feed_url 
            ON subscriptions(feed_url)
        """)
        
        # Create message_history table for deduplication
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                feed_url TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                UNIQUE(title, feed_url, chat_id)
            )
        """)
        
        # Create indexes for efficient querying
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_history_timestamp 
            ON message_history(timestamp)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_history_lookup 
            ON message_history(title, feed_url, chat_id)
        """)
        
        conn.commit()
        logger.info(f"Database initialized: {self.db_file}")
    
    def close(self):
        """Close the current thread's database connection."""
        if hasattr(self._local, 'conn'):
            try:
                self._local.conn.close()
                delattr(self._local, 'conn')
                logger.info("Database connection closed")
            except Exception as e:
                logger.error(f"Error closing connection: {e}")
    
    # Chat operations
    def get_chat(self, chat_id: int) -> Optional[dict]:
        """Get chat by ID."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT id, title FROM chats WHERE id = ?",
            (chat_id,)
        )
        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "title": row["title"]
            }
        return None
    
    def add_chat(self, chat_id: int, title: str) -> dict:
        """Add or update chat."""
        with self._transaction() as conn:
            # Try to insert, on conflict update title
            conn.execute("""
                INSERT INTO chats (id, title) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET title = excluded.title
            """, (chat_id, title))
            
            logger.info(f"Added/updated chat: {chat_id} ({title})")
            
            return {
                "id": chat_id,
                "title": title
            }
    
    def get_all_chats(self) -> Dict[str, dict]:
        """Get all chats."""
        conn = self._get_connection()
        cursor = conn.execute("SELECT id, title FROM chats")
        
        chats = {}
        for row in cursor:
            chat_id = str(row["id"])
            chats[chat_id] = {
                "id": row["id"],
                "title": row["title"],
                "feeds": self._get_chat_feed_urls(row["id"])
            }
        return chats
    
    def _get_chat_feed_urls(self, chat_id: int) -> List[str]:
        """Helper to get feed URLs for a chat."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT feed_url FROM subscriptions WHERE chat_id = ?",
            (chat_id,)
        )
        return [row["feed_url"] for row in cursor]
    
    # Feed operations
    def get_feed(self, url: str) -> Optional[dict]:
        """Get feed by URL."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT url, title, last_etag, last_modified, last_check_time
            FROM feeds WHERE url = ?
        """, (url,))
        
        row = cursor.fetchone()
        if row:
            return {
                "url": row["url"],
                "title": row["title"],
                "last_etag": row["last_etag"],
                "last_modified": row["last_modified"],
                "last_check_time": row["last_check_time"]
            }
        return None
    
    def add_feed(self, url: str, title: str = None, etag: str = None, modified: str = None) -> dict:
        """Add or update feed."""
        with self._transaction() as conn:
            feed_title = title or url
            
            # Check if feed exists
            cursor = conn.execute("SELECT url FROM feeds WHERE url = ?", (url,))
            exists = cursor.fetchone() is not None
            
            if not exists:
                # Insert new feed
                conn.execute("""
                    INSERT INTO feeds (url, title, last_etag, last_modified, last_check_time)
                    VALUES (?, ?, ?, ?, NULL)
                """, (url, feed_title, etag, modified))
                logger.info(f"Added new feed: {url}")
            else:
                # Update existing feed
                updates = []
                params = []
                
                if title:
                    updates.append("title = ?")
                    params.append(title)
                if etag is not None:
                    updates.append("last_etag = ?")
                    params.append(etag)
                if modified is not None:
                    updates.append("last_modified = ?")
                    params.append(modified)
                
                if updates:
                    params.append(url)
                    conn.execute(
                        f"UPDATE feeds SET {', '.join(updates)} WHERE url = ?",
                        params
                    )
            
            return self.get_feed(url)
    
    def update_feed_metadata(self, url: str, etag: str = None, modified: str = None, 
                            title: str = None, last_check_time: float = None):
        """Update feed metadata (etag, modified, title, last_check_time)."""
        with self._transaction() as conn:
            updates = []
            params = []
            
            if etag is not None:
                updates.append("last_etag = ?")
                params.append(etag)
            if modified is not None:
                updates.append("last_modified = ?")
                params.append(modified)
            if title is not None:
                updates.append("title = ?")
                params.append(title)
            if last_check_time is not None:
                updates.append("last_check_time = ?")
                params.append(last_check_time)
            
            if updates:
                params.append(url)
                conn.execute(
                    f"UPDATE feeds SET {', '.join(updates)} WHERE url = ?",
                    params
                )
    
    def get_all_feeds(self) -> Dict[str, dict]:
        """Get all feeds."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT url, title, last_etag, last_modified, last_check_time
            FROM feeds
        """)
        
        feeds = {}
        for row in cursor:
            feeds[row["url"]] = {
                "url": row["url"],
                "title": row["title"],
                "last_etag": row["last_etag"],
                "last_modified": row["last_modified"],
                "last_check_time": row["last_check_time"]
            }
        return feeds
    
    # Subscription operations
    def subscribe(self, chat_id: int, feed_url: str) -> bool:
        """Subscribe chat to feed. Returns True if newly subscribed, False if already subscribed."""
        with self._transaction() as conn:
            # Check if chat exists
            cursor = conn.execute("SELECT id FROM chats WHERE id = ?", (chat_id,))
            if cursor.fetchone() is None:
                return False
            
            # Check if already subscribed
            cursor = conn.execute("""
                SELECT 1 FROM subscriptions WHERE chat_id = ? AND feed_url = ?
            """, (chat_id, feed_url))
            
            if cursor.fetchone() is not None:
                return False
            
            # Add subscription
            conn.execute("""
                INSERT INTO subscriptions (chat_id, feed_url)
                VALUES (?, ?)
            """, (chat_id, feed_url))
            
            logger.info(f"Chat {chat_id} subscribed to {feed_url}")
            return True
    
    def unsubscribe(self, chat_id: int, feed_url: str) -> bool:
        """Unsubscribe chat from feed. Returns True if unsubscribed, False if wasn't subscribed."""
        with self._transaction() as conn:
            # Check if subscribed
            cursor = conn.execute("""
                SELECT 1 FROM subscriptions WHERE chat_id = ? AND feed_url = ?
            """, (chat_id, feed_url))
            
            if cursor.fetchone() is None:
                return False
            
            # Remove subscription
            conn.execute("""
                DELETE FROM subscriptions WHERE chat_id = ? AND feed_url = ?
            """, (chat_id, feed_url))
            
            # Check if feed has any remaining subscribers
            cursor = conn.execute("""
                SELECT 1 FROM subscriptions WHERE feed_url = ? LIMIT 1
            """, (feed_url,))
            
            if cursor.fetchone() is None:
                # No more subscribers, remove the feed
                conn.execute("DELETE FROM feeds WHERE url = ?", (feed_url,))
                logger.info(f"Feed {feed_url} removed (no remaining subscribers)")
            
            logger.info(f"Chat {chat_id} unsubscribed from {feed_url}")
            return True
    
    def is_subscribed(self, chat_id: int, feed_url: str) -> bool:
        """Check if chat is subscribed to feed."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT 1 FROM subscriptions WHERE chat_id = ? AND feed_url = ?
        """, (chat_id, feed_url))
        
        return cursor.fetchone() is not None
    
    def get_chat_feeds(self, chat_id: int) -> List[dict]:
        """Get all feeds for a chat."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT f.url, f.title, f.last_etag, f.last_modified, f.last_check_time
            FROM feeds f
            INNER JOIN subscriptions s ON f.url = s.feed_url
            WHERE s.chat_id = ?
            ORDER BY s.subscribed_at
        """, (chat_id,))
        
        feeds = []
        for row in cursor:
            feeds.append({
                "url": row["url"],
                "title": row["title"],
                "last_etag": row["last_etag"],
                "last_modified": row["last_modified"],
                "last_check_time": row["last_check_time"]
            })
        return feeds
    
    def get_feed_subscribers(self, feed_url: str) -> List[int]:
        """Get all chat IDs subscribed to a feed."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT chat_id FROM subscriptions WHERE feed_url = ?
        """, (feed_url,))
        
        return [row["chat_id"] for row in cursor]
    
    # Last check time operations
    def get_last_check_time(self, feed_url: str) -> Optional[float]:
        """Get the last check time for a feed."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT last_check_time FROM feeds WHERE url = ?",
            (feed_url,)
        )
        
        row = cursor.fetchone()
        if row:
            return row["last_check_time"]
        return None
    
    def update_last_check_time(self, feed_url: str, timestamp: float):
        """Update the last check time for a feed."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE feeds SET last_check_time = ? WHERE url = ?",
                (timestamp, feed_url)
            )
    
    # Message deduplication operations
    def check_message_duplicate(self, title: str, feed_url: str, chat_id: int) -> bool:
        """
        检查消息是否在过去一小时内已推送过。
        
        Args:
            title: 消息标题
            feed_url: RSS feed URL
            chat_id: 聊天 ID
            
        Returns:
            True 表示重复（应该忽略），False 表示不重复（应该推送）
        """
        import time
        conn = self._get_connection()
        one_hour_ago = time.time() - 3600  # 一小时 = 3600 秒
        
        cursor = conn.execute("""
            SELECT 1 FROM message_history 
            WHERE title = ? AND feed_url = ? AND chat_id = ? AND timestamp > ?
            LIMIT 1
        """, (title, feed_url, chat_id, one_hour_ago))
        
        return cursor.fetchone() is not None
    
    def record_message(self, title: str, feed_url: str, chat_id: int):
        """
        记录已推送的消息。
        
        Args:
            title: 消息标题
            feed_url: RSS feed URL
            chat_id: 聊天 ID
        """
        import time
        with self._transaction() as conn:
            # 使用 INSERT OR REPLACE 处理重复
            conn.execute("""
                INSERT OR REPLACE INTO message_history (title, feed_url, chat_id, timestamp)
                VALUES (?, ?, ?, ?)
            """, (title, feed_url, chat_id, time.time()))
    
    def cleanup_old_messages(self):
        """清理超过一小时的消息历史记录，保持数据库轻量。"""
        import time
        with self._transaction() as conn:
            one_hour_ago = time.time() - 3600
            cursor = conn.execute(
                "DELETE FROM message_history WHERE timestamp < ?",
                (one_hour_ago,)
            )
            deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old message records")


# Global instance
store = SQLiteStore()
