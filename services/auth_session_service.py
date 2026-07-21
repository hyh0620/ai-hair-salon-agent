"""Opaque refresh-token rotation and revocable server authentication sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
import secrets
from typing import Callable, Dict, Optional
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config.auth_config import AuthConfig
from config.time_config import utc_now_naive
from db.base.session_manager import SessionManager
from db.models import AuthSession
from db.repositories.auth_session_repository import AuthSessionRepository


logger = logging.getLogger(__name__)
REFRESH_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,256}$")
MAX_CLEANUP_ROWS = 100


class AuthSessionServiceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AuthSessionMaterial:
    auth_session_id: str
    refresh_token_id: str
    refresh_token: str
    refresh_token_hash: str
    issued_at: datetime
    session_expires_at: datetime


@dataclass(frozen=True)
class RefreshRotationResult:
    status: str
    user: Optional[Dict[str, object]] = None
    auth_session_id: Optional[str] = None
    access_token: Optional[str] = None
    access_expires_at: Optional[datetime] = None
    refresh_token: Optional[str] = None
    session_expires_at: Optional[datetime] = None


AccessTokenIssuer = Callable[
    [str, str, datetime, datetime],
    tuple[str, datetime],
]


class AuthSessionService:
    def __init__(
        self,
        session_manager: SessionManager,
        repository: AuthSessionRepository,
        config: AuthConfig,
        *,
        clock: Callable[[], datetime] = utc_now_naive,
        token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self.session_manager = session_manager
        self.repository = repository
        self.config = config
        self._clock = clock
        self._token_factory = token_factory

    def create_material(self, *, now: Optional[datetime] = None) -> AuthSessionMaterial:
        issued_at = self._normalize_now(now or self._clock())
        raw_token = self._token_factory(48)
        if not self.valid_refresh_token_format(raw_token):
            raise AuthSessionServiceError("token_generation_error")
        return AuthSessionMaterial(
            auth_session_id=str(uuid4()),
            refresh_token_id=str(uuid4()),
            refresh_token=raw_token,
            refresh_token_hash=self.hash_refresh_token(raw_token),
            issued_at=issued_at,
            session_expires_at=issued_at
            + timedelta(days=self.config.refresh_token_days),
        )

    def persist_new_session_in_session(
        self,
        session: Session,
        *,
        user_id: str,
        material: AuthSessionMaterial,
        created_by: str,
    ) -> None:
        self.repository.add_session_in_session(
            session,
            session_id=material.auth_session_id,
            user_id=user_id,
            now=material.issued_at,
            expires_at=material.session_expires_at,
            created_by=created_by,
        )
        self.repository.add_refresh_token_in_session(
            session,
            token_id=material.refresh_token_id,
            session_id=material.auth_session_id,
            token_hash=material.refresh_token_hash,
            issued_at=material.issued_at,
            expires_at=material.session_expires_at,
        )

    def validate_session(
        self,
        *,
        auth_session_id: str,
        user_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, object]]:
        checked_at = self._normalize_now(now or self._clock())
        with self.session_manager.session_scope() as session:
            context = self.repository.get_session_context_in_session(
                session,
                auth_session_id,
            )
            if not context:
                return None
            auth_session, user = context
            if not self._session_is_active(auth_session, checked_at):
                return None
            if auth_session.user_id != user_id or not user.is_active:
                return None
            return self._public_user(user)

    def rotate_refresh_token(
        self,
        raw_token: str,
        *,
        issue_access_token: AccessTokenIssuer,
        now: Optional[datetime] = None,
        trace_id: Optional[str] = None,
    ) -> RefreshRotationResult:
        if not self.valid_refresh_token_format(raw_token):
            return RefreshRotationResult(status="invalid")
        checked_at = self._normalize_now(now or self._clock())
        token_hash = self.hash_refresh_token(raw_token)
        try:
            with self.session_manager.session_scope(immediate=True) as session:
                context = self.repository.get_refresh_context_in_session(
                    session,
                    token_hash,
                )
                if not context:
                    result = RefreshRotationResult(status="invalid")
                else:
                    refresh_token, auth_session, user = context
                    result = self._rotate_context_in_session(
                        session,
                        refresh_token=refresh_token,
                        auth_session=auth_session,
                        user=user,
                        now=checked_at,
                        issue_access_token=issue_access_token,
                    )
                    if result.status in {"success", "replay"}:
                        self.cleanup_history_in_session(session, now=checked_at)
        except AuthSessionServiceError:
            raise
        except SQLAlchemyError as exc:
            self._log("refresh", "rollback", trace_id, "persistence_error")
            raise AuthSessionServiceError("persistence_error") from exc
        except Exception as exc:
            self._log("refresh", "rollback", trace_id, "credential_issue_error")
            raise AuthSessionServiceError("credential_issue_error") from exc

        self._log("refresh", result.status, trace_id, result.status)
        return result

    def revoke_session(
        self,
        *,
        auth_session_id: str,
        user_id: Optional[str] = None,
        reason: str = "logout",
        now: Optional[datetime] = None,
        trace_id: Optional[str] = None,
    ) -> bool:
        checked_at = self._normalize_now(now or self._clock())
        try:
            with self.session_manager.session_scope(immediate=True) as session:
                context = self.repository.get_session_context_in_session(
                    session,
                    auth_session_id,
                )
                if not context:
                    revoked = False
                else:
                    auth_session, _user = context
                    if user_id is not None and auth_session.user_id != user_id:
                        revoked = False
                    else:
                        self.repository.revoke_session_in_session(
                            session,
                            auth_session,
                            now=checked_at,
                            reason=reason,
                        )
                        revoked = True
        except SQLAlchemyError as exc:
            self._log("logout", "rollback", trace_id, "persistence_error")
            raise AuthSessionServiceError("persistence_error") from exc
        self._log("logout", "commit" if revoked else "not_found", trace_id, reason)
        return revoked

    def revoke_session_by_refresh_token(
        self,
        raw_token: str,
        *,
        reason: str = "logout",
        now: Optional[datetime] = None,
        trace_id: Optional[str] = None,
    ) -> bool:
        if not self.valid_refresh_token_format(raw_token):
            return False
        checked_at = self._normalize_now(now or self._clock())
        token_hash = self.hash_refresh_token(raw_token)
        try:
            with self.session_manager.session_scope(immediate=True) as session:
                context = self.repository.get_refresh_context_in_session(
                    session,
                    token_hash,
                )
                if not context:
                    revoked = False
                else:
                    _refresh_token, auth_session, _user = context
                    self.repository.revoke_session_in_session(
                        session,
                        auth_session,
                        now=checked_at,
                        reason=reason,
                    )
                    revoked = True
        except SQLAlchemyError as exc:
            self._log("logout", "rollback", trace_id, "persistence_error")
            raise AuthSessionServiceError("persistence_error") from exc
        self._log("logout", "commit" if revoked else "not_found", trace_id, reason)
        return revoked

    def cleanup_history_in_session(self, session: Session, *, now: datetime) -> None:
        retention_cutoff = now - timedelta(days=self.config.auth_session_retention_days)
        self.repository.cleanup_history_in_session(
            session,
            now=now,
            retention_cutoff=retention_cutoff,
            limit=MAX_CLEANUP_ROWS,
        )

    def _rotate_context_in_session(
        self,
        session: Session,
        *,
        refresh_token,
        auth_session: AuthSession,
        user,
        now: datetime,
        issue_access_token: AccessTokenIssuer,
    ) -> RefreshRotationResult:
        if (
            not self._session_is_active(auth_session, now)
            or not user.is_active
            or refresh_token.revoked_at is not None
            or refresh_token.expires_at <= now
        ):
            return RefreshRotationResult(status="invalid")

        if refresh_token.used_at is not None:
            elapsed = max(0.0, (now - refresh_token.used_at).total_seconds())
            if elapsed <= self.config.refresh_reuse_grace_seconds:
                return RefreshRotationResult(status="concurrent")
            self.repository.revoke_session_in_session(
                session,
                auth_session,
                now=now,
                reason="refresh_replay",
            )
            return RefreshRotationResult(status="replay")

        new_raw_token = self._token_factory(48)
        if not self.valid_refresh_token_format(new_raw_token):
            raise AuthSessionServiceError("token_generation_error")
        new_token_id = str(uuid4())
        new_token_hash = self.hash_refresh_token(new_raw_token)
        access_token, access_expires_at = issue_access_token(
            str(user.id),
            str(auth_session.id),
            now,
            auth_session.expires_at,
        )

        refresh_token.used_at = now
        new_expires_at = min(
            auth_session.expires_at,
            now + timedelta(days=self.config.refresh_token_days),
        )
        self.repository.add_refresh_token_in_session(
            session,
            token_id=new_token_id,
            session_id=auth_session.id,
            token_hash=new_token_hash,
            issued_at=now,
            expires_at=new_expires_at,
        )
        refresh_token.replaced_by_token_id = new_token_id
        auth_session.last_used_at = now
        session.flush()
        return RefreshRotationResult(
            status="success",
            user=self._public_user(user),
            auth_session_id=str(auth_session.id),
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=new_raw_token,
            session_expires_at=auth_session.expires_at,
        )

    @staticmethod
    def hash_refresh_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @staticmethod
    def valid_refresh_token_format(raw_token: object) -> bool:
        return isinstance(raw_token, str) and bool(
            REFRESH_TOKEN_PATTERN.fullmatch(raw_token)
        )

    @staticmethod
    def _session_is_active(auth_session: AuthSession, now: datetime) -> bool:
        return auth_session.revoked_at is None and auth_session.expires_at > now

    @staticmethod
    def _normalize_now(value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _public_user(user) -> Dict[str, object]:
        return {
            "id": str(user.id),
            "email": str(user.email),
            "display_name": str(user.display_name),
            "is_active": bool(user.is_active),
            "created_at": user.created_at,
            "updated_at": user.updated_at,
        }

    @staticmethod
    def _log(
        operation: str,
        status: str,
        trace_id: Optional[str],
        reason: str,
    ) -> None:
        logger.info(
            "auth_session_event operation=%s status=%s trace_id=%s reason=%s",
            operation,
            status,
            trace_id or "unavailable",
            reason,
        )
