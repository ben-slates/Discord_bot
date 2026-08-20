import os
import json
import datetime
import contextlib
import asyncio
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Boolean, DateTime, ForeignKey, Float, Index, event, inspect, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    DB_URL = "sqlite:///./rynex.db"
elif DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine_kwargs = {
    "connect_args": {"check_same_thread": False} if "sqlite" in DB_URL else {"connect_timeout": 10},
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}

# Tune pool sizes for Postgres to avoid blocking when many threads request
# connections concurrently. Leave defaults for SQLite.
if "postgresql" in DB_URL or DB_URL.startswith("postgres://"):
    engine_kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_timeout": 5})

engine = create_engine(DB_URL, **engine_kwargs)


@dataclass
class DatabaseWorkProfile:
    started: float
    checkout_started: float | None = None
    checkout_seconds: float = 0.0
    sql_started: float | None = None
    sql_seconds: float = 0.0
    pre_ping_seconds: float = 0.0
    statement_count: int = 0
    flush_started: float | None = None
    flush_seconds: float = 0.0
    commit_started: float | None = None
    commit_seconds: float = 0.0
    statements: list[tuple[str, float]] = None

    def __post_init__(self):
        if self.statements is None:
            self.statements = []


_db_profile = threading.local()


@contextlib.contextmanager
def profile_database_work():
    profile = DatabaseWorkProfile(started=time.perf_counter(), checkout_started=time.perf_counter())
    _db_profile.current = profile
    try:
        yield profile
    finally:
        _db_profile.current = None


def _active_profile():
    return getattr(_db_profile, "current", None)


@event.listens_for(engine, "checkout")
def _measure_checkout(_dbapi_connection, _connection_record, _connection_proxy):
    profile = _active_profile()
    if profile and profile.checkout_started is not None:
        profile.checkout_seconds += time.perf_counter() - profile.checkout_started
        profile.checkout_started = None


@event.listens_for(engine, "before_cursor_execute")
def _measure_sql_start(_conn, _cursor, _statement, _parameters, _context, _executemany):
    try:
        running_on_event_loop = threading.current_thread() is threading.main_thread() and asyncio.get_running_loop().is_running()
    except RuntimeError:
        running_on_event_loop = False
    if running_on_event_loop:
        # This should never occur during Discord async work.  Capture the
        # caller at the point of violation, not after the loop resumes.
        caller = traceback.extract_stack(limit=10)[-3]
        logging.error(
            "Synchronous SQLAlchemy execute on MainThread: %s:%s in %s; SQL=%s",
            caller.filename, caller.lineno, caller.name, " ".join(_statement.split())[:200],
        )
    profile = _active_profile()
    if profile:
        profile.sql_started = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _measure_sql_end(_conn, _cursor, _statement, _parameters, _context, _executemany):
    profile = _active_profile()
    if profile and profile.sql_started is not None:
        elapsed = time.perf_counter() - profile.sql_started
        profile.sql_seconds += elapsed
        if _statement.strip().upper() == "SELECT 1":
            profile.pre_ping_seconds += time.perf_counter() - profile.sql_started
        profile.statement_count += 1
        profile.statements.append((" ".join(_statement.split())[:500], elapsed))
        profile.sql_started = None


@event.listens_for(Session, "before_flush")
def _measure_flush_start(_session, _flush_context, _instances):
    profile = _active_profile()
    if profile:
        profile.flush_started = time.perf_counter()


@event.listens_for(Session, "after_flush")
def _measure_flush_end(_session, _flush_context):
    profile = _active_profile()
    if profile and profile.flush_started is not None:
        profile.flush_seconds += time.perf_counter() - profile.flush_started
        profile.flush_started = None


@event.listens_for(Session, "before_commit")
def _measure_commit_start(_session):
    profile = _active_profile()
    if profile:
        profile.commit_started = time.perf_counter()


@event.listens_for(Session, "after_commit")
def _measure_commit_end(_session):
    profile = _active_profile()
    if profile and profile.commit_started is not None:
        profile.commit_seconds += time.perf_counter() - profile.commit_started
        profile.commit_started = None

_RAW_SESSION_FACTORY = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def SessionLocal():
    return _RAW_SESSION_FACTORY()

class GuildConfig(Base):
    __tablename__ = "guild_config"
    guild_id = Column(String, primary_key=True)
    # Leveling
    leveling_enabled = Column(Boolean, default=False)
    xp_cooldown = Column(Integer, default=60)
    xp_per_message = Column(Integer, default=15)
    daily_xp_limit = Column(Integer, default=200)
    min_message_length = Column(Integer, default=5)
    leveling_channel = Column(String, nullable=True)
    # Attendance
    attendance_enabled = Column(Boolean, default=False)
    attendance_channel = Column(String, nullable=True)
    # Welcome messages
    welcome_enabled = Column(Boolean, default=False)
    welcome_channel = Column(String, nullable=True)
    # Hall of Fame
    hall_of_fame_enabled = Column(Boolean, default=False)
    hall_of_fame_channel = Column(String, nullable=True)
    hall_of_fame_role_name = Column(String, nullable=True)
    hall_of_fame_announcement_channel = Column(String, nullable=True)
    hall_of_fame_warning_channel = Column(String, nullable=True)
    # Support
    support_enabled = Column(Boolean, default=False)
    support_category = Column(String, nullable=True)
    # Security (antispam columns removed)
    # Notifications
    notification_channel = Column(String, nullable=True)
    daily_summary_time = Column(String, default="00:00")
    # Leaderboards
    main_leaderboard_role_ids = Column(String, nullable=True)
    leaderboard_enabled = Column(Boolean, default=False)
    leaderboard_channel = Column(String, nullable=True)
    # Level-up announcements
    level_up_announcements_enabled = Column(Boolean, default=False)
    level_up_announcements_channel = Column(String, nullable=True)
    # Bot logs
    bot_logs_enabled = Column(Boolean, default=False)
    bot_logs_channel = Column(String, nullable=True)
    # CVE and news
    cve_and_news_enabled = Column(Boolean, default=False)
    cve_and_news_channel = Column(String, nullable=True)
    # Note: legacy `support_feature_enabled` removed; `support_enabled` used instead

class UserData(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True)
    xp = Column(Integer, default=0, nullable=False)
    level = Column(Integer, default=1, nullable=False)
    messages = Column(Integer, default=0, nullable=False)
    voice_minutes = Column(Integer, default=0, nullable=False)
    daily_streak = Column(Integer, default=0, nullable=False)
    reputation = Column(Integer, default=0, nullable=False)
    daily_xp_earned = Column(Integer, default=0, nullable=False)
    daily_text_xp_earned = Column(Integer, default=0, nullable=False)
    daily_voice_xp_earned = Column(Integer, default=0, nullable=False)
    daily_xp_date = Column(String, nullable=True, default=None)
    last_daily = Column(DateTime(timezone=True))
    last_message = Column(String)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))), onupdate=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))))

class AttendanceLog(Base):
    __tablename__ = "attendance_logs"
    id = Column(Integer, primary_key=True)
    guild_id = Column(String, index=True)
    user_id = Column(String, index=True)
    date = Column(String) # YYYY-MM-DD
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))))
    __table_args__ = (
        Index("ix_attendance_logs_guild_date", "guild_id", "date"),
        Index("ix_attendance_logs_guild_user_date", "guild_id", "user_id", "date"),
    )

class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True)
    guild_id = Column(String, index=True)
    channel_id = Column(String, index=True)
    owner_id = Column(String)
    status = Column(String, default="open") # open, closed
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))))
    closed_at = Column(DateTime, nullable=True)
    __table_args__ = (Index("ix_tickets_guild_status", "guild_id", "status"),)

class CustomLeaderboard(Base):
    __tablename__ = "custom_leaderboards"
    channel_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    guild_id = Column(String, nullable=False)
    required_role_id = Column(String, nullable=True)

class HallOfFameEntry(Base):
    __tablename__ = "hall_of_fame_entries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    role_name = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))))

class NewsLog(Base):
    __tablename__ = "news_logs"
    link = Column(String, primary_key=True)
    posted_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))))

Base.metadata.create_all(bind=engine)


def ensure_database_columns():
    inspector = inspect(engine)
    if "guild_config" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("guild_config")}
    if "main_leaderboard_role_ids" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN main_leaderboard_role_ids VARCHAR"))
    if "bot_logs_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN bot_logs_enabled BOOLEAN DEFAULT FALSE"))
    if "bot_logs_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN bot_logs_channel VARCHAR"))
    if "cve_and_news_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN cve_and_news_enabled BOOLEAN DEFAULT FALSE"))
    if "cve_and_news_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN cve_and_news_channel VARCHAR"))
    # legacy support_feature_enabled removed; support_category is already handled above
    if "leaderboard_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN leaderboard_enabled BOOLEAN DEFAULT FALSE"))
    if "leaderboard_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN leaderboard_channel VARCHAR"))
    if "welcome_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN welcome_enabled BOOLEAN DEFAULT FALSE"))
    if "welcome_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN welcome_channel VARCHAR"))
    if "hall_of_fame_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN hall_of_fame_enabled BOOLEAN DEFAULT FALSE"))
    if "hall_of_fame_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN hall_of_fame_channel VARCHAR"))
    if "hall_of_fame_role_name" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN hall_of_fame_role_name VARCHAR"))
    if "hall_of_fame_announcement_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN hall_of_fame_announcement_channel VARCHAR"))
    if "hall_of_fame_warning_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN hall_of_fame_warning_channel VARCHAR"))
    custom_leaderboard_columns = {column["name"] for column in inspector.get_columns("custom_leaderboards")}
    if "required_role_id" not in custom_leaderboard_columns:
        with engine.begin() as conn:
            try:
                conn.execute(text("ALTER TABLE custom_leaderboards ADD COLUMN required_role_id VARCHAR"))
            except ProgrammingError as exc:
                if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower():
                    raise
    if "level_up_announcements_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN level_up_announcements_enabled BOOLEAN DEFAULT FALSE"))
    if "level_up_announcements_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN level_up_announcements_channel VARCHAR"))
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "daily_text_xp_earned" not in user_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN daily_text_xp_earned INTEGER DEFAULT 0"))
    if "daily_voice_xp_earned" not in user_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN daily_voice_xp_earned INTEGER DEFAULT 0"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET daily_text_xp_earned = 0 WHERE daily_text_xp_earned IS NULL"))
        conn.execute(text("UPDATE users SET daily_voice_xp_earned = 0 WHERE daily_voice_xp_earned IS NULL"))
        conn.execute(text("UPDATE guild_config SET daily_xp_limit = 200 WHERE daily_xp_limit IS NULL OR daily_xp_limit != 200"))


ensure_database_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
