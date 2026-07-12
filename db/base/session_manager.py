from contextlib import contextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
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
        self.engine = create_engine(db_path)
        Base.metadata.create_all(self.engine)
        self.Session = scoped_session(sessionmaker(bind=self.engine))

    @contextmanager
    def session_scope(self):
        """
        提供会话上下文管理
        
        自动处理：
        - 会话创建和关闭
        - 事务提交和回滚
        - 异常处理
        """
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self):
        """关闭会话管理器"""
        self.Session.remove()
        self.engine.dispose()
