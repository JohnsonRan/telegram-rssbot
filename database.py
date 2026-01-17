import os
import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Table
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import func

Base = declarative_base()

# Many-to-Many relationship between Chat and Feed
subscription_table = Table('subscription', Base.metadata,
    Column('chat_id', Integer, ForeignKey('chats.id'), primary_key=True),
    Column('feed_id', Integer, ForeignKey('feeds.id'), primary_key=True)
)

class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, primary_key=True)  # Telegram Chat ID
    title = Column(String)
    created_at = Column(DateTime, default=func.now())
    
    feeds = relationship('Feed', secondary=subscription_table, back_populates='chats')

    def __repr__(self):
        return f"<Chat(id={self.id}, title='{self.title}')>"

class Feed(Base):
    __tablename__ = 'feeds'
    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String, unique=True, nullable=False)
    title = Column(String)
    last_etag = Column(String, nullable=True)
    last_modified = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    
    chats = relationship('Chat', secondary=subscription_table, back_populates='feeds')
    seen_entries = relationship('SeenEntry', back_populates='feed', cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Feed(id={self.id}, url='{self.url}')>"

class SeenEntry(Base):
    __tablename__ = 'seen_entries'
    id = Column(Integer, primary_key=True, autoincrement=True)
    feed_id = Column(Integer, ForeignKey('feeds.id'), nullable=False)
    entry_link = Column(String, nullable=False)
    published_date = Column(DateTime, nullable=True)
    create_at = Column(DateTime, default=func.now())

    feed = relationship('Feed', back_populates='seen_entries')

# Database Setup
DATABASE_URL = "sqlite:///bot.db"

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
    print("Database initialized.")
