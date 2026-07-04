"""
现代化的 SQLite 数据存储层

采用 dataclass 模型和领域驱动设计，提供类型安全和清晰的 API。
"""

import time
import sqlite3
import logging
from pathlib import Path
from typing import Optional, List, Dict, Set
from dataclasses import dataclass, asdict
from contextlib import contextmanager
from threading import local

logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型定义
# ============================================================================

@dataclass
class Chat:
    """聊天会话模型"""
    id: int
    title: str


@dataclass
class Feed:
    """RSS Feed 模型"""
    url: str
    title: str
    last_etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_check_time: Optional[float] = None


@dataclass
class MessageRecord:
    """消息记录模型（用于去重）"""
    title: str
    feed_url: str
    chat_id: int
    timestamp: float
    id: Optional[int] = None


# ============================================================================
# 基础操作类
# ============================================================================

class _BaseOperations:
    """提供通用的数据库操作方法"""
    
    def __init__(self, store):
        self.store = store
    
    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接"""
        return self.store._get_conn()
    
    def _transaction(self):
        """获取事务上下文"""
        return self.store._transaction()
    
    @staticmethod
    def _row_to_dict(row: sqlite3.Row, fields: List[str]) -> dict:
        """将 Row 对象转换为字典"""
        return {field: row[field] for field in fields} if row else None
    
    def _exists(self, table: str, where: str, params: tuple) -> bool:
        """检查记录是否存在"""
        conn = self._get_conn()
        cursor = conn.execute(
            f"SELECT 1 FROM {table} WHERE {where} LIMIT 1",
            params
        )
        return cursor.fetchone() is not None
    
    def _delete_older_than(self, table: str, ts_column: str, window: int, label: str) -> int:
        """删除时间戳早于 now - window 的记录，返回删除数量"""
        cutoff_time = time.time() - window

        with self._transaction() as conn:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {ts_column} < ?",
                (cutoff_time,)
            )
            deleted_count = cursor.rowcount

            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} {label}")

            return deleted_count

    @staticmethod
    def _build_update(updates: dict) -> tuple:
        """构建 UPDATE SET 子句"""
        if not updates:
            return None, []
        
        set_clause = ", ".join(f"{key} = ?" for key in updates.keys())
        params = list(updates.values())
        return set_clause, params


# ============================================================================
# 聊天操作类
# ============================================================================

class ChatOperations(_BaseOperations):
    """聊天相关的数据库操作"""
    
    FIELDS = ["id", "title"]
    
    def get(self, chat_id: int) -> Optional[Chat]:
        """获取聊天信息"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT id, title FROM chats WHERE id = ?",
            (chat_id,)
        )
        row = cursor.fetchone()
        return Chat(**self._row_to_dict(row, self.FIELDS)) if row else None
    
    def create(self, chat: Chat) -> Chat:
        """创建或更新聊天"""
        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO chats (id, title) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET title = excluded.title
            """, (chat.id, chat.title))
            
            logger.info(f"Added/updated chat: {chat.id} ({chat.title})")
            return chat
    
    def get_all(self) -> List[Chat]:
        """获取所有聊天"""
        conn = self._get_conn()
        cursor = conn.execute("SELECT id, title FROM chats")
        return [Chat(**self._row_to_dict(row, self.FIELDS)) for row in cursor]
    
    def get_feed_urls(self, chat_id: int) -> List[str]:
        """获取聊天订阅的所有 feed URL"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT feed_url FROM subscriptions WHERE chat_id = ?",
            (chat_id,)
        )
        return [row["feed_url"] for row in cursor]


# ============================================================================
# Feed 操作类
# ============================================================================

class FeedOperations(_BaseOperations):
    """Feed 相关的数据库操作"""
    
    FIELDS = ["url", "title", "last_etag", "last_modified", "last_check_time"]
    
    def get(self, url: str) -> Optional[Feed]:
        """获取 Feed 信息"""
        conn = self._get_conn()
        cursor = conn.execute(
            f"SELECT {', '.join(self.FIELDS)} FROM feeds WHERE url = ?",
            (url,)
        )
        row = cursor.fetchone()
        return Feed(**self._row_to_dict(row, self.FIELDS)) if row else None
    
    def create(self, feed: Feed) -> Feed:
        """创建或更新 Feed"""
        with self._transaction() as conn:
            # 检查是否存在
            exists = self._exists("feeds", "url = ?", (feed.url,))
            
            if not exists:
                # 插入新 Feed
                conn.execute("""
                    INSERT INTO feeds (url, title, last_etag, last_modified, last_check_time)
                    VALUES (?, ?, ?, ?, ?)
                """, (feed.url, feed.title, feed.last_etag, feed.last_modified, feed.last_check_time))
                logger.info(f"Added new feed: {feed.url}")
            else:
                # 更新现有 Feed
                updates = {k: v for k, v in asdict(feed).items() 
                          if k != 'url' and v is not None}
                if updates:
                    set_clause, params = self._build_update(updates)
                    params.append(feed.url)
                    conn.execute(f"UPDATE feeds SET {set_clause} WHERE url = ?", params)
            
            return feed
    
    def update(self, url: str, **kwargs) -> None:
        """更新 Feed 元数据"""
        # 过滤 None 值
        updates = {k: v for k, v in kwargs.items() if v is not None}
        
        if not updates:
            return
        
        with self._transaction() as conn:
            set_clause, params = self._build_update(updates)
            params.append(url)
            conn.execute(f"UPDATE feeds SET {set_clause} WHERE url = ?", params)
    
    def get_all(self) -> Dict[str, Feed]:
        """获取所有 Feed，返回 {url: Feed} 字典"""
        conn = self._get_conn()
        cursor = conn.execute(f"SELECT {', '.join(self.FIELDS)} FROM feeds")
        
        return {
            row["url"]: Feed(**self._row_to_dict(row, self.FIELDS))
            for row in cursor
        }
    
    def delete(self, url: str) -> bool:
        """删除 Feed"""
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM feeds WHERE url = ?", (url,))
            return cursor.rowcount > 0


# ============================================================================
# 订阅操作类
# ============================================================================

class SubscriptionOperations(_BaseOperations):
    """订阅相关的数据库操作"""
    
    def subscribe(self, chat_id: int, feed_url: str) -> bool:
        """订阅 Feed，返回 True 表示新订阅，False 表示已存在"""
        with self._transaction() as conn:
            # 检查聊天是否存在
            if not self._exists("chats", "id = ?", (chat_id,)):
                return False
            
            # 检查是否已订阅
            if self._exists("subscriptions", "chat_id = ? AND feed_url = ?", 
                          (chat_id, feed_url)):
                return False
            
            # 添加订阅
            conn.execute("""
                INSERT INTO subscriptions (chat_id, feed_url)
                VALUES (?, ?)
            """, (chat_id, feed_url))
            
            logger.info(f"Chat {chat_id} subscribed to {feed_url}")
            return True
    
    def unsubscribe(self, chat_id: int, feed_url: str) -> bool:
        """取消订阅，返回 True 表示成功，False 表示未订阅"""
        with self._transaction() as conn:
            # 检查是否已订阅
            if not self._exists("subscriptions", "chat_id = ? AND feed_url = ?",
                              (chat_id, feed_url)):
                return False
            
            # 删除订阅
            conn.execute("""
                DELETE FROM subscriptions WHERE chat_id = ? AND feed_url = ?
            """, (chat_id, feed_url))
            
            # 检查 Feed 是否还有其他订阅者
            if not self._exists("subscriptions", "feed_url = ?", (feed_url,)):
                # 没有订阅者了，删除 Feed
                conn.execute("DELETE FROM feeds WHERE url = ?", (feed_url,))
                logger.info(f"Feed {feed_url} removed (no remaining subscribers)")
            
            logger.info(f"Chat {chat_id} unsubscribed from {feed_url}")
            return True
    
    def is_subscribed(self, chat_id: int, feed_url: str) -> bool:
        """检查是否已订阅"""
        return self._exists("subscriptions", "chat_id = ? AND feed_url = ?",
                          (chat_id, feed_url))
    
    def get_chat_feeds(self, chat_id: int) -> List[Feed]:
        """获取聊天订阅的所有 Feed"""
        conn = self._get_conn()
        cursor = conn.execute("""
            SELECT f.url, f.title, f.last_etag, f.last_modified, f.last_check_time
            FROM feeds f
            INNER JOIN subscriptions s ON f.url = s.feed_url
            WHERE s.chat_id = ?
            ORDER BY s.subscribed_at
        """, (chat_id,))
        
        return [Feed(**self._row_to_dict(row, FeedOperations.FIELDS)) 
                for row in cursor]
    
    def get_feed_subscribers(self, feed_url: str) -> List[int]:
        """获取订阅了指定 Feed 的所有聊天 ID"""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT chat_id FROM subscriptions WHERE feed_url = ?",
            (feed_url,)
        )
        return [row["chat_id"] for row in cursor]


# ============================================================================
# 消息历史操作类
# ============================================================================

class MessageHistoryOperations(_BaseOperations):
    """消息去重相关的数据库操作"""
    
    # 去重时间窗口（秒）
    DEDUP_WINDOW = 5400  # 1.5 小时
    
    def is_duplicate(self, title: str, feed_url: str, chat_id: int) -> bool:
        """检查消息是否为重复（去重时间窗口内已推送过）"""
        cutoff_time = time.time() - self.DEDUP_WINDOW
        
        return self._exists(
            "message_history",
            "title = ? AND feed_url = ? AND chat_id = ? AND timestamp > ?",
            (title, feed_url, chat_id, cutoff_time)
        )
    
    def record(self, title: str, feed_url: str, chat_id: int) -> None:
        """记录已推送的消息"""
        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO message_history (title, feed_url, chat_id, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(title, feed_url, chat_id)
                DO UPDATE SET timestamp = excluded.timestamp
            """, (title, feed_url, chat_id, time.time()))
    
    def filter_and_record_batch(self, feed_url: str, chat_id: int, titles: List[str]) -> Set[str]:
        """批量过滤重复消息并记录新消息，返回本次允许推送的标题集合"""
        if not titles:
            return set()
        
        # 在单次批处理中去重，避免同标题重复推送
        unique_titles = list(dict.fromkeys(titles))
        cutoff_time = time.time() - self.DEDUP_WINDOW
        
        placeholders = ", ".join("?" for _ in unique_titles)
        query = f"""
            SELECT title FROM message_history
            WHERE feed_url = ? AND chat_id = ? AND timestamp > ?
              AND title IN ({placeholders})
        """
        params = [feed_url, chat_id, cutoff_time, *unique_titles]
        conn = self._get_conn()
        existing_titles = {row["title"] for row in conn.execute(query, params)}
        
        new_titles = [title for title in unique_titles if title not in existing_titles]
        if not new_titles:
            return set()
        
        now = time.time()
        with self._transaction() as conn:
            conn.executemany("""
                INSERT INTO message_history (title, feed_url, chat_id, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(title, feed_url, chat_id)
                DO UPDATE SET timestamp = excluded.timestamp
            """, [(title, feed_url, chat_id, now) for title in new_titles])
        
        return set(new_titles)
    
    def cleanup_old(self, window_seconds: Optional[int] = None) -> int:
        """清理过期的消息记录，返回删除的记录数"""
        return self._delete_older_than(
            "message_history", "timestamp",
            window_seconds or self.DEDUP_WINDOW,
            "old message records",
        )


# ============================================================================
# Feed 条目追踪操作类（用于判定新条目，支持无时间戳的 Feed）
# ============================================================================

class FeedEntryOperations(_BaseOperations):
    """持久化记录每个 Feed 已见过的条目 key，用于可靠地判定新条目。

    与基于发布时间的判定不同，这里以 build_entry_key 生成的稳定指纹为准，
    因此即使条目缺少 published/updated 时间也能正确去重并推送。
    """

    # 条目 key 保留期（秒）。按 last_seen 修剪：仅清理早已离开 Feed 的条目。
    RETENTION = 7_776_000  # 90 天

    def register_and_get_new(self, feed_url: str, keys: List[str]) -> Set[str]:
        """记录本次见到的所有 key（刷新 last_seen），返回其中此前未见过的 key 集合。

        该 Feed 尚无任何记录时（首次检查，或迁移到 key 追踪的既有 Feed），
        仅记录 key 并返回空集合，确保历史条目不会被当作新条目推送。
        """
        if not keys:
            return set()

        unique_keys = list(dict.fromkeys(keys))
        had_prior_keys = self._exists("feed_entries", "feed_url = ?", (feed_url,))

        existing: Set[str] = set()
        if had_prior_keys:
            placeholders = ", ".join("?" for _ in unique_keys)
            conn = self._get_conn()
            existing = {
                row["entry_key"]
                for row in conn.execute(
                    f"SELECT entry_key FROM feed_entries "
                    f"WHERE feed_url = ? AND entry_key IN ({placeholders})",
                    [feed_url, *unique_keys],
                )
            }

        now = time.time()
        with self._transaction() as conn:
            conn.executemany("""
                INSERT INTO feed_entries (feed_url, entry_key, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(feed_url, entry_key)
                DO UPDATE SET last_seen = excluded.last_seen
            """, [(feed_url, key, now, now) for key in unique_keys])

        if not had_prior_keys:
            return set()

        return {key for key in unique_keys if key not in existing}

    def cleanup_old(self, window_seconds: Optional[int] = None) -> int:
        """清理长期未再出现的条目 key，返回删除的记录数。"""
        return self._delete_older_than(
            "feed_entries", "last_seen",
            window_seconds or self.RETENTION,
            "stale feed entry keys",
        )


# ============================================================================
# 主存储类
# ============================================================================

class SQLiteStore:
    """
    现代化的 SQLite 数据存储
    
    使用领域驱动设计，按业务功能分组操作：
    - store.chats: 聊天相关操作
    - store.feeds: Feed 相关操作
    - store.subscriptions: 订阅相关操作
    - store.messages: 消息去重相关操作
    """
    
    def __init__(self, db_file: str = "bot_data.db"):
        self.db_file = Path(db_file)
        self._local = local()  # 线程本地存储
        self._init_database()
        
        # 初始化各业务操作组
        self.chats = ChatOperations(self)
        self.feeds = FeedOperations(self)
        self.subscriptions = SubscriptionOperations(self)
        self.messages = MessageHistoryOperations(self)
        self.feed_entries = FeedEntryOperations(self)
    
    def _get_conn(self) -> sqlite3.Connection:
        """获取线程本地数据库连接"""
        if not hasattr(self._local, 'conn'):
            conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
            conn.row_factory = sqlite3.Row  # 启用字典式访问
            conn.execute("PRAGMA journal_mode=WAL")  # 启用 WAL 模式提高并发
            conn.execute("PRAGMA foreign_keys=ON")   # 启用外键约束
            self._local.conn = conn
        return self._local.conn
    
    @contextmanager
    def _transaction(self):
        """事务上下文管理器"""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Transaction error: {e}")
            raise
    
    def _init_database(self):
        """初始化数据库表结构"""
        conn = self._get_conn()
        
        # 聊天表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Feed 表
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
        
        # 订阅关系表
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
        
        # 订阅表索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscriptions_feed_url 
            ON subscriptions(feed_url)
        """)
        
        # 消息历史表（用于去重）
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
        
        # 消息历史表索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_history_timestamp 
            ON message_history(timestamp)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_message_history_lookup
            ON message_history(title, feed_url, chat_id)
        """)

        # Feed 条目追踪表（用于判定新条目）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feed_entries (
                feed_url TEXT NOT NULL,
                entry_key TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                PRIMARY KEY (feed_url, entry_key),
                FOREIGN KEY (feed_url) REFERENCES feeds(url) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feed_entries_last_seen
            ON feed_entries(last_seen)
        """)

        conn.commit()
        logger.info(f"Database initialized: {self.db_file}")
    
    def close(self):
        """关闭当前线程的数据库连接"""
        if hasattr(self._local, 'conn'):
            try:
                self._local.conn.close()
                delattr(self._local, 'conn')
                logger.info("Database connection closed")
            except Exception as e:
                logger.error(f"Error closing connection: {e}")


# 全局实例
store = SQLiteStore()

