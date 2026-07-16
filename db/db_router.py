from .base import SessionManager
from .repositories import AppointmentRepository, StylistRepository, UserBehaviorRepository


class DatabaseRouter:
    """Unified database access entrypoint."""

    def __init__(self, db_path: str = None):
        self.session_manager = SessionManager(db_path)
        self.appointment_repo = AppointmentRepository()
        self.stylist_repo = StylistRepository(self.session_manager)
        self.user_behavior_repo = UserBehaviorRepository(self.session_manager)

    @property
    def stylists(self) -> StylistRepository:
        return self.stylist_repo

    @property
    def appointments(self) -> AppointmentRepository:
        return self.appointment_repo

    @property
    def user_behavior(self) -> UserBehaviorRepository:
        return self.user_behavior_repo

    def close(self):
        self.session_manager.close()
