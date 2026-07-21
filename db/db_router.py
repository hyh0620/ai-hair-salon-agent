from .base import SessionManager
from .repositories import (
    AppointmentRepository,
    AuthSessionRepository,
    StylistRepository,
    UserBehaviorRepository,
    UserRepository,
)


class DatabaseRouter:
    """Unified database access entrypoint."""

    def __init__(self, db_path: str = None):
        self.session_manager = SessionManager(db_path)
        self.appointment_repo = AppointmentRepository()
        self.auth_session_repo = AuthSessionRepository()
        self.stylist_repo = StylistRepository(self.session_manager)
        self.user_behavior_repo = UserBehaviorRepository(self.session_manager)
        self.user_repo = UserRepository()

    @property
    def stylists(self) -> StylistRepository:
        return self.stylist_repo

    @property
    def appointments(self) -> AppointmentRepository:
        return self.appointment_repo

    @property
    def auth_sessions(self) -> AuthSessionRepository:
        return self.auth_session_repo

    @property
    def user_behavior(self) -> UserBehaviorRepository:
        return self.user_behavior_repo

    @property
    def users(self) -> UserRepository:
        return self.user_repo

    def close(self):
        self.session_manager.close()
