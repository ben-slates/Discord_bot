import os
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Boolean, DateTime, ForeignKey, Float, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
import datetime
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    DB_URL = "sqlite:///./rynex.db"
elif DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DB_URL, 
    connect_args={"check_same_thread": False} if "sqlite" in DB_URL else {"connect_timeout": 10},
    pool_pre_ping=True,
    pool_recycle=1800
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class GuildConfig(Base):
    __tablename__ = "guild_config"
    guild_id = Column(String, primary_key=True)
    # Leveling
    leveling_enabled = Column(Boolean, default=False)
    xp_cooldown = Column(Integer, default=60)
    xp_per_message = Column(Integer, default=15)
    daily_xp_limit = Column(Integer, default=100)
    min_message_length = Column(Integer, default=5)
    leveling_channel = Column(String, nullable=True)
    # Attendance
    attendance_enabled = Column(Boolean, default=False)
    attendance_channel = Column(String, nullable=True)
    # Support
    support_enabled = Column(Boolean, default=False)
    support_category = Column(String, nullable=True)
    # Security
    antispam_enabled = Column(Boolean, default=False)
    antispam_max_messages = Column(Integer, default=5)
    antispam_seconds = Column(Integer, default=5)
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
    # Support
    support_feature_enabled = Column(Boolean, default=False)
    support_category = Column(String, nullable=True)

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

class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True)
    guild_id = Column(String, index=True)
    channel_id = Column(String, index=True)
    owner_id = Column(String)
    status = Column(String, default="open") # open, closed
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))))
    closed_at = Column(DateTime, nullable=True)

class CustomLeaderboard(Base):
    __tablename__ = "custom_leaderboards"
    channel_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    guild_id = Column(String, nullable=False)

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
    if "support_feature_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN support_feature_enabled BOOLEAN DEFAULT FALSE"))
    if "support_category" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN support_category VARCHAR"))
    if "leaderboard_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN leaderboard_enabled BOOLEAN DEFAULT FALSE"))
    if "leaderboard_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN leaderboard_channel VARCHAR"))
    if "level_up_announcements_enabled" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN level_up_announcements_enabled BOOLEAN DEFAULT FALSE"))
    if "level_up_announcements_channel" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN level_up_announcements_channel VARCHAR"))


ensure_database_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
