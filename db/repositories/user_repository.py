"""Transaction-aware persistence helpers for local user accounts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from db.models import User


class UserRepository:
    @staticmethod
    def add_user_in_session(
        session: Session,
        *,
        user_id: str,
        email: str,
        display_name: str,
        password_hash: str,
        now: datetime,
    ) -> Dict[str, Any]:
        user = User(
            id=user_id,
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush()
        return UserRepository.to_dict(user)

    @staticmethod
    def get_by_email_in_session(
        session: Session,
        email: str,
    ) -> Optional[Dict[str, Any]]:
        user = session.query(User).filter(User.email == email).first()
        return UserRepository.to_dict(user) if user else None

    @staticmethod
    def get_by_id_in_session(
        session: Session,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        user = session.query(User).filter(User.id == user_id).first()
        return UserRepository.to_dict(user) if user else None

    @staticmethod
    def update_password_hash_in_session(
        session: Session,
        *,
        user_id: str,
        password_hash: str,
        now: datetime,
    ) -> bool:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        user.password_hash = password_hash
        user.updated_at = now
        session.flush()
        return True

    @staticmethod
    def to_dict(user: User) -> Dict[str, Any]:
        return {
            "id": str(user.id),
            "email": str(user.email),
            "display_name": str(user.display_name),
            "password_hash": str(user.password_hash),
            "is_active": bool(user.is_active),
            "created_at": user.created_at,
            "updated_at": user.updated_at,
        }
