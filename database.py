import os
import json
import datetime
from typing import Any
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Boolean, DateTime, ForeignKey, Float, inspect, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import declarative_base, sessionmaker
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
    pool_recycle=1800,
)

_RAW_SESSION_FACTORY = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
BUFFERED_WRITES_FILE = os.path.join(CACHE_DIR, "buffered_writes.json")


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _load_buffered_writes():
    _ensure_cache_dir()
    if not os.path.exists(BUFFERED_WRITES_FILE):
        return []
    try:
        with open(BUFFERED_WRITES_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_buffered_writes(operations):
    _ensure_cache_dir()
    with open(BUFFERED_WRITES_FILE, "w", encoding="utf-8") as handle:
        json.dump(operations, handle, indent=2)


def _serialize_value(value: Any):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    return value


def _restore_value(value: Any):
    if isinstance(value, str):
        try:
            if "T" in value or " " in value:
                return datetime.datetime.fromisoformat(value)
        except ValueError:
            pass
    return value


def _get_model_class(model_name: str):
    for cls in Base.registry._class_registry.values():
        if getattr(cls, "__name__", None) == model_name and hasattr(cls, "__tablename__"):
            return cls
    return None


class BufferedSession:
    def __init__(self, session):
        self._session = session
        self._pending_operations = []

    def add(self, instance):
        self._session.add(instance)

    def delete(self, instance):
        self._session.delete(instance)

    def commit(self):
        operations = _load_buffered_writes()
        pending_operations = []

        for instance in list(self._session.new):
            pending_operations.append(("add", self._serialize_instance(instance)))

        for instance in list(self._session.dirty):
            if instance in self._session.deleted:
                continue
            pending_operations.append(("update", self._serialize_instance(instance)))

        for instance in list(self._session.deleted):
            pending_operations.append(("delete", self._serialize_instance(instance)))

        if pending_operations:
            operations.extend(pending_operations)
            _save_buffered_writes(operations)

        self._session.rollback()
        self._session.expunge_all()

    def rollback(self):
        self._session.rollback()

    def close(self):
        self._session.close()

    def refresh(self, instance):
        return self._session.refresh(instance)

    def query(self, *entities, **kwargs):
        return self._session.query(*entities, **kwargs)

    def execute(self, *args, **kwargs):
        return self._session.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)

    @staticmethod
    def _serialize_instance(instance):
        mapper = inspect(instance)
        values = {}
        pk_values = {}
        for attr in mapper.attrs:
            if attr.key == "_sa_instance_state":
                continue
            try:
                value = getattr(instance, attr.key)
            except Exception:
                continue
            if value is None:
                values[attr.key] = None
                continue
            values[attr.key] = _serialize_value(value)
            if attr.key in {pk.name for pk in mapper.primary_key}:
                pk_values[attr.key] = _serialize_value(value)
        return {
            "class": instance.__class__.__name__,
            "values": values,
            "pk_values": pk_values,
        }


def SessionLocal():
    return BufferedSession(_RAW_SESSION_FACTORY())


def flush_pending_changes_to_db():
    operations = _load_buffered_writes()
    if not operations:
        return 0

    session = _RAW_SESSION_FACTORY()
    try:
        for operation_type, payload in operations:
            model_name = payload.get("class")
            if not model_name:
                continue
            model_class = _get_model_class(model_name)
            if not model_class:
                continue

            table_name = getattr(model_class, "__tablename__", None)
            if table_name and table_name not in inspect(engine).get_table_names():
                continue

            if operation_type == "delete":
                pk_values = payload.get("pk_values", {}) or {}
                if not pk_values:
                    continue
                query = session.query(model_class)
                for key, value in pk_values.items():
                    query = query.filter(getattr(model_class, key) == _restore_value(value))
                query.delete(synchronize_session=False)
                continue

            if operation_type in {"add", "update"}:
                values = payload.get("values", {}) or {}
                instance = model_class()
                for key, value in values.items():
                    if key in {column.name for column in inspect(model_class).mapper.columns}:
                        setattr(instance, key, _restore_value(value))
                session.merge(instance)

        session.commit()
        _save_buffered_writes([])
        return len(operations)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

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


ensure_database_columns()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
