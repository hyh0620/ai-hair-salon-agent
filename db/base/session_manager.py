from contextlib import contextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, scoped_session
from ..models import Base


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_database_url(db_path: str | None = None) -> str:
    """Resolve relative SQLite URLs against the project root."""
    load_dotenv(PROJECT_ROOT / ".env")
    database_url = db_path or os.getenv("DATABASE_URL", "sqlite:///data/smart_appointment.db")
    if not database_url.startswith("sqlite:///") or ":memory:" in database_url:
        return database_url

    raw_path = database_url.removeprefix("sqlite:///")
    sqlite_path = Path(raw_path)
    if not sqlite_path.is_absolute():
        sqlite_path = (PROJECT_ROOT / sqlite_path).resolve()
    return f"sqlite:///{sqlite_path}"


class SessionManager:
    """
    数据库会话管理器
    
    职责：
    1. 管理数据库连接和会话
    2. 提供统一的会话上下文管理
    3. 处理事务和异常回滚
    """
    
    def __init__(self, db_path=None):
        """
        初始化会话管理器
        
        Args:
            db_path: 数据库连接路径
        """
        db_path = resolve_database_url(db_path)
        if db_path.startswith("sqlite:///") and ":memory:" not in db_path:
            sqlite_path = db_path.replace("sqlite:///", "", 1)
            directory = os.path.dirname(sqlite_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        engine_options = {}
        if db_path.startswith("sqlite:"):
            engine_options["connect_args"] = {
                "check_same_thread": False,
                "timeout": 30,
            }
        self.engine = create_engine(db_path, **engine_options)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite_connection)
        Base.metadata.create_all(self.engine)
        self._upgrade_sqlite_appointments()
        self._upgrade_sqlite_users()
        self._install_sqlite_schedule_guards()
        self.Session = scoped_session(sessionmaker(bind=self.engine))

    @contextmanager
    def session_scope(self, *, immediate: bool = False):
        """
        提供会话上下文管理
        
        自动处理：
        - 会话创建和关闭
        - 事务提交和回滚
        - 异常处理
        """
        session = self.Session()
        try:
            if immediate and self.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _configure_sqlite_connection(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    def _install_sqlite_schedule_guards(self):
        if self.engine.dialect.name != "sqlite":
            return

        insert_trigger = """
        CREATE TRIGGER IF NOT EXISTS prevent_overlapping_busy_schedule_insert
        BEFORE INSERT ON stylist_schedules
        WHEN NEW.status = 'busy'
        BEGIN
            SELECT RAISE(ABORT, 'schedule_conflict')
            WHERE EXISTS (
                SELECT 1
                FROM stylist_schedules
                WHERE stylist_id = NEW.stylist_id
                  AND status = 'busy'
                  AND start_time < NEW.end_time
                  AND end_time > NEW.start_time
            );
        END
        """
        update_trigger = """
        CREATE TRIGGER IF NOT EXISTS prevent_overlapping_busy_schedule_update
        BEFORE UPDATE OF stylist_id, start_time, end_time, status ON stylist_schedules
        WHEN NEW.status = 'busy'
        BEGIN
            SELECT RAISE(ABORT, 'schedule_conflict')
            WHERE EXISTS (
                SELECT 1
                FROM stylist_schedules
                WHERE id != NEW.id
                  AND stylist_id = NEW.stylist_id
                  AND status = 'busy'
                  AND start_time < NEW.end_time
                  AND end_time > NEW.start_time
            );
        END
        """
        with self.engine.begin() as connection:
            connection.exec_driver_sql(insert_trigger)
            connection.exec_driver_sql(update_trigger)

    def _upgrade_sqlite_appointments(self):
        """Apply the small, repeatable lifecycle upgrade to existing SQLite files."""
        if self.engine.dialect.name != "sqlite":
            return

        with self.engine.begin() as connection:
            table_exists = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appointments'"
            ).scalar()
            if not table_exists:
                return

            columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(appointments)"
                ).fetchall()
            }
            upgrades = {
                "status": "ALTER TABLE appointments ADD COLUMN status VARCHAR NOT NULL DEFAULT 'confirmed'",
                "updated_at": "ALTER TABLE appointments ADD COLUMN updated_at DATETIME",
                "version": "ALTER TABLE appointments ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
            }
            for column_name, statement in upgrades.items():
                if column_name not in columns:
                    connection.exec_driver_sql(statement)

            connection.exec_driver_sql(
                "UPDATE appointments SET status='confirmed' WHERE status IS NULL OR status=''"
            )
            connection.exec_driver_sql(
                "UPDATE appointments SET version=1 WHERE version IS NULL OR version < 1"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_appointments_owner_start "
                "ON appointments(user_id, start_time, id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_appointments_status_start "
                "ON appointments(status, start_time, id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_stylist_schedules_appointment "
                "ON stylist_schedules(appointment_id)"
            )

    def _upgrade_sqlite_users(self):
        """Ensure the account lookup index exists without rebuilding old data."""
        if self.engine.dialect.name != "sqlite":
            return

        with self.engine.begin() as connection:
            table_exists = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).scalar()
            if not table_exists:
                return
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)"
            )

    def close(self):
        """关闭会话管理器"""
        self.Session.remove()
        self.engine.dispose()
