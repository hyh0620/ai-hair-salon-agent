"""Transaction-aware persistence for revocable authentication sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from db.models import AuthRefreshToken, AuthSession, User


class AuthSessionRepository:
    @staticmethod
    def add_session_in_session(
        session: Session,
        *,
        session_id: str,
        user_id: str,
        now: datetime,
        expires_at: datetime,
        created_by: Optional[str],
    ) -> AuthSession:
        auth_session = AuthSession(
            id=session_id,
            user_id=user_id,
            created_at=now,
            expires_at=expires_at,
            last_used_at=now,
            created_by=created_by,
        )
        session.add(auth_session)
        session.flush()
        return auth_session

    @staticmethod
    def add_refresh_token_in_session(
        session: Session,
        *,
        token_id: str,
        session_id: str,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> AuthRefreshToken:
        refresh_token = AuthRefreshToken(
            id=token_id,
            session_id=session_id,
            token_hash=token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        session.add(refresh_token)
        session.flush()
        return refresh_token

    @staticmethod
    def get_session_context_in_session(
        session: Session,
        session_id: str,
    ) -> Optional[tuple[AuthSession, User]]:
        return (
            session.query(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .filter(AuthSession.id == session_id)
            .one_or_none()
        )

    @staticmethod
    def get_refresh_context_in_session(
        session: Session,
        token_hash: str,
    ) -> Optional[tuple[AuthRefreshToken, AuthSession, User]]:
        return (
            session.query(AuthRefreshToken, AuthSession, User)
            .join(AuthSession, AuthSession.id == AuthRefreshToken.session_id)
            .join(User, User.id == AuthSession.user_id)
            .filter(AuthRefreshToken.token_hash == token_hash)
            .one_or_none()
        )

    @staticmethod
    def revoke_session_in_session(
        session: Session,
        auth_session: AuthSession,
        *,
        now: datetime,
        reason: str,
    ) -> None:
        if auth_session.revoked_at is None:
            auth_session.revoked_at = now
            auth_session.revocation_reason = reason[:64]
        session.query(AuthRefreshToken).filter(
            AuthRefreshToken.session_id == auth_session.id,
            AuthRefreshToken.revoked_at.is_(None),
        ).update(
            {AuthRefreshToken.revoked_at: now},
            synchronize_session=False,
        )
        session.flush()

    @staticmethod
    def cleanup_history_in_session(
        session: Session,
        *,
        now: datetime,
        retention_cutoff: datetime,
        limit: int = 100,
    ) -> tuple[int, int]:
        token_rows = (
            session.query(AuthRefreshToken)
            .filter(
                or_(
                    and_(
                        AuthRefreshToken.expires_at < now,
                        AuthRefreshToken.used_at.is_(None),
                        AuthRefreshToken.revoked_at.is_(None),
                    ),
                    and_(
                        AuthRefreshToken.used_at.is_not(None),
                        AuthRefreshToken.used_at < retention_cutoff,
                    ),
                    and_(
                        AuthRefreshToken.revoked_at.is_not(None),
                        AuthRefreshToken.revoked_at < retention_cutoff,
                    ),
                )
            )
            .order_by(AuthRefreshToken.expires_at, AuthRefreshToken.id)
            .limit(limit)
            .all()
        )
        for row in token_rows:
            session.delete(row)
        session.flush()

        session_rows = (
            session.query(AuthSession)
            .filter(
                or_(
                    AuthSession.expires_at < retention_cutoff,
                    and_(
                        AuthSession.revoked_at.is_not(None),
                        AuthSession.revoked_at < retention_cutoff,
                    ),
                )
            )
            .order_by(AuthSession.expires_at, AuthSession.id)
            .limit(limit)
            .all()
        )
        for row in session_rows:
            session.delete(row)
        session.flush()
        return len(token_rows), len(session_rows)
