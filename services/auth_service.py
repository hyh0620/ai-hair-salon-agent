"""Argon2 accounts, session-bound access JWTs and opaque refresh credentials."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
from typing import Any, Callable, Dict, Optional
from uuid import UUID, uuid4

from email_validator import EmailNotValidError, validate_email
import jwt
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError

from config.auth_config import AuthConfig
from config.time_config import utc_now_naive
from db.db_router import DatabaseRouter
from services.auth_session_service import (
    AuthSessionMaterial,
    AuthSessionService,
    AuthSessionServiceError,
    RefreshRotationResult,
)


logger = logging.getLogger(__name__)


class AuthServiceError(Exception):
    """Stable authentication failure safe for API-level mapping."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedAccessToken:
    user: Dict[str, Any]
    claims: Dict[str, Any]
    auth_session_id: str


@dataclass(frozen=True)
class IssuedAuthSession:
    user: Dict[str, Any]
    auth_session_id: str
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    session_expires_at: datetime

    @property
    def access_expires_in(self) -> int:
        now = datetime.now(timezone.utc)
        expires_at = self.access_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return max(1, int((expires_at - now).total_seconds()))


class AuthService:
    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        config: Optional[AuthConfig] = None,
        clock: Callable[[], datetime] = utc_now_naive,
        refresh_token_factory: Callable[[int], str] = secrets.token_urlsafe,
    ):
        self.config = config or AuthConfig.from_env()
        self.db_router = DatabaseRouter(db_path)
        self.user_repo = self.db_router.users
        self.auth_session_repo = self.db_router.auth_sessions
        self.password_hash = PasswordHash.recommended()
        self._clock = clock
        self.auth_sessions = AuthSessionService(
            self.db_router.session_manager,
            self.auth_session_repo,
            self.config,
            clock=clock,
            token_factory=refresh_token_factory,
        )

    def close(self) -> None:
        self.db_router.close()

    def register_user(
        self,
        *,
        email: str,
        display_name: str,
        password: str,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create only an account; HTTP registration uses register_with_session()."""
        self._require_configured()
        values = self._prepare_registration(email, display_name, password)
        now = self._now()
        try:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                if self.user_repo.get_by_email_in_session(session, values["email"]):
                    raise AuthServiceError("email_already_registered")
                user = self.user_repo.add_user_in_session(
                    session,
                    user_id=str(uuid4()),
                    email=values["email"],
                    display_name=values["display_name"],
                    password_hash=values["password_hash"],
                    now=now,
                )
        except Exception as exc:
            self._raise_registration_error(exc, trace_id, values["email"])
        self._log_auth("register", "success", trace_id=trace_id, user_id=user["id"])
        return self.public_user(user)

    def register_with_session(
        self,
        *,
        email: str,
        display_name: str,
        password: str,
        trace_id: Optional[str] = None,
    ) -> IssuedAuthSession:
        """Atomically create the user, authentication session and refresh hash."""
        self._require_configured()
        values = self._prepare_registration(email, display_name, password)
        now = self._now()
        user_id = str(uuid4())
        material, access_token, access_expires_at = self._prepare_session_issue(
            user_id,
            now,
        )
        try:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                if self.user_repo.get_by_email_in_session(session, values["email"]):
                    raise AuthServiceError("email_already_registered")
                user = self.user_repo.add_user_in_session(
                    session,
                    user_id=user_id,
                    email=values["email"],
                    display_name=values["display_name"],
                    password_hash=values["password_hash"],
                    now=now,
                )
                self.auth_sessions.persist_new_session_in_session(
                    session,
                    user_id=user_id,
                    material=material,
                    created_by="register",
                )
                self.auth_sessions.cleanup_history_in_session(session, now=now)
        except Exception as exc:
            self._raise_registration_error(exc, trace_id, values["email"])
        self._log_auth("register", "success", trace_id=trace_id, user_id=user_id)
        return self._issued_session(
            user,
            material,
            access_token,
            access_expires_at,
        )

    def authenticate_user(
        self,
        *,
        email: str,
        password: str,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_configured()
        user, updated_hash = self._verify_credentials(email, password, trace_id)
        if updated_hash:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                self.user_repo.update_password_hash_in_session(
                    session,
                    user_id=user["id"],
                    password_hash=updated_hash,
                    now=self._now(),
                )
        self._log_auth("login", "success", trace_id=trace_id, user_id=user["id"])
        return self.public_user(user)

    def authenticate_with_session(
        self,
        *,
        email: str,
        password: str,
        trace_id: Optional[str] = None,
    ) -> IssuedAuthSession:
        self._require_configured()
        user, updated_hash = self._verify_credentials(email, password, trace_id)
        now = self._now()
        material, access_token, access_expires_at = self._prepare_session_issue(
            user["id"],
            now,
        )
        try:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                current = self.user_repo.get_by_id_in_session(session, user["id"])
                if not current or not current.get("is_active"):
                    raise AuthServiceError("invalid_credentials")
                if updated_hash:
                    self.user_repo.update_password_hash_in_session(
                        session,
                        user_id=user["id"],
                        password_hash=updated_hash,
                        now=now,
                    )
                self.auth_sessions.persist_new_session_in_session(
                    session,
                    user_id=user["id"],
                    material=material,
                    created_by="login",
                )
                self.auth_sessions.cleanup_history_in_session(session, now=now)
        except AuthServiceError:
            raise
        except Exception as exc:
            logger.exception(
                "auth_event operation=login status=error trace_id=%s error_type=%s",
                trace_id or "unavailable",
                type(exc).__name__,
            )
            raise AuthServiceError("persistence_error") from exc
        self._log_auth("login", "success", trace_id=trace_id, user_id=user["id"])
        return self._issued_session(
            current,
            material,
            access_token,
            access_expires_at,
        )

    def create_session_for_user(
        self,
        user_id: str,
        *,
        created_by: str = "internal",
        now: Optional[datetime] = None,
    ) -> IssuedAuthSession:
        """Issue a persisted session for an already authenticated user."""
        self._require_configured()
        issued_at = self._normalize_naive_utc(now or self._clock())
        material, access_token, access_expires_at = self._prepare_session_issue(
            user_id,
            issued_at,
        )
        try:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                user = self.user_repo.get_by_id_in_session(session, user_id)
                if not user or not user.get("is_active"):
                    raise AuthServiceError("invalid_token")
                self.auth_sessions.persist_new_session_in_session(
                    session,
                    user_id=user_id,
                    material=material,
                    created_by=created_by,
                )
                self.auth_sessions.cleanup_history_in_session(session, now=issued_at)
        except AuthServiceError:
            raise
        except Exception as exc:
            raise AuthServiceError("persistence_error") from exc
        return self._issued_session(
            user,
            material,
            access_token,
            access_expires_at,
        )

    def create_access_token(
        self,
        user_id: str,
        auth_session_id: str,
        *,
        now: Optional[datetime] = None,
        expires_delta: Optional[timedelta] = None,
        absolute_expires_at: Optional[datetime] = None,
    ) -> tuple[str, Dict[str, Any]]:
        self._require_configured()
        if not self._valid_user_id(user_id) or not self._valid_user_id(auth_session_id):
            raise AuthServiceError("invalid_token")
        issued_at = self._normalize_aware_utc(now or datetime.now(timezone.utc))
        expires_at = issued_at + (
            expires_delta or timedelta(minutes=self.config.access_token_minutes)
        )
        if absolute_expires_at is not None:
            expires_at = min(
                expires_at,
                self._normalize_aware_utc(absolute_expires_at),
            )
        if expires_at <= issued_at:
            raise AuthServiceError("invalid_session")
        claims: Dict[str, Any] = {
            "sub": str(user_id),
            "sid": str(auth_session_id),
            "type": "access",
            "iat": issued_at,
            "exp": expires_at,
            "iss": self.config.issuer,
            "aud": self.config.audience,
            "jti": uuid4().hex,
        }
        token = jwt.encode(
            claims,
            self.config.jwt_secret,
            algorithm=self.config.jwt_algorithm,
        )
        return token, claims

    def verify_access_token(self, token: str) -> VerifiedAccessToken:
        claims = self.decode_access_token(token)
        user_id = str(claims["sub"])
        auth_session_id = str(claims["sid"])
        user = self.auth_sessions.validate_session(
            auth_session_id=auth_session_id,
            user_id=user_id,
            now=self._now(),
        )
        if not user:
            raise AuthServiceError("invalid_token")
        return VerifiedAccessToken(user, claims, auth_session_id)

    def decode_access_token(self, token: str) -> Dict[str, Any]:
        """Validate JWT claims without treating the JWT as session authority."""
        self._require_configured()
        if not token or not token.strip():
            raise AuthServiceError("invalid_token")
        try:
            claims = jwt.decode(
                token,
                self.config.jwt_secret,
                algorithms=[self.config.jwt_algorithm],
                audience=self.config.audience,
                issuer=self.config.issuer,
                options={
                    "require": [
                        "sub",
                        "sid",
                        "type",
                        "iat",
                        "exp",
                        "iss",
                        "aud",
                        "jti",
                    ],
                },
            )
        except InvalidTokenError as exc:
            raise AuthServiceError("invalid_token") from exc
        if (
            claims.get("type") != "access"
            or not self._valid_user_id(claims.get("sub"))
            or not self._valid_user_id(claims.get("sid"))
        ):
            raise AuthServiceError("invalid_token")
        return claims

    def refresh_session(
        self,
        raw_refresh_token: str,
        *,
        now: Optional[datetime] = None,
        trace_id: Optional[str] = None,
    ) -> RefreshRotationResult:
        self._require_configured()
        try:
            return self.auth_sessions.rotate_refresh_token(
                raw_refresh_token,
                issue_access_token=self._issue_access_for_session,
                now=now,
                trace_id=trace_id,
            )
        except AuthSessionServiceError as exc:
            raise AuthServiceError(exc.code) from exc

    def revoke_session(
        self,
        *,
        auth_session_id: str,
        user_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> bool:
        self._require_configured()
        try:
            return self.auth_sessions.revoke_session(
                auth_session_id=auth_session_id,
                user_id=user_id,
                trace_id=trace_id,
            )
        except AuthSessionServiceError as exc:
            raise AuthServiceError(exc.code) from exc

    def revoke_session_by_refresh_token(
        self,
        raw_refresh_token: str,
        *,
        trace_id: Optional[str] = None,
    ) -> bool:
        self._require_configured()
        try:
            return self.auth_sessions.revoke_session_by_refresh_token(
                raw_refresh_token,
                trace_id=trace_id,
            )
        except AuthSessionServiceError as exc:
            raise AuthServiceError(exc.code) from exc

    @staticmethod
    def normalize_email(value: str) -> str:
        try:
            normalized = validate_email(
                (value or "").strip(),
                check_deliverability=False,
            ).normalized
        except EmailNotValidError as exc:
            raise AuthServiceError("invalid_email") from exc
        return normalized.lower()

    @staticmethod
    def normalize_display_name(value: str) -> str:
        normalized = " ".join((value or "").strip().split())
        if not 1 <= len(normalized) <= 80:
            raise AuthServiceError("invalid_display_name")
        return normalized

    @staticmethod
    def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "is_active": bool(user["is_active"]),
            "created_at": user["created_at"],
            "updated_at": user["updated_at"],
        }

    def _prepare_registration(
        self,
        email: str,
        display_name: str,
        password: str,
    ) -> Dict[str, str]:
        normalized_email = self.normalize_email(email)
        normalized_name = self.normalize_display_name(display_name)
        self._validate_password(password)
        return {
            "email": normalized_email,
            "display_name": normalized_name,
            "password_hash": self.password_hash.hash(password),
        }

    def _verify_credentials(
        self,
        email: str,
        password: str,
        trace_id: Optional[str],
    ) -> tuple[Dict[str, Any], Optional[str]]:
        normalized_email = self.normalize_email(email)
        if not 8 <= len(password or "") <= 128:
            self._reject_login(trace_id, normalized_email)
        with self.db_router.session_manager.session_scope() as session:
            user = self.user_repo.get_by_email_in_session(session, normalized_email)

        valid = False
        updated_hash = None
        if user and user.get("is_active"):
            try:
                valid, updated_hash = self.password_hash.verify_and_update(
                    password,
                    user["password_hash"],
                )
            except Exception:
                valid = False
        if not valid or not user:
            self._reject_login(trace_id, normalized_email)
        return user, updated_hash

    def _reject_login(self, trace_id: Optional[str], email: str) -> None:
        self._log_auth(
            "login",
            "rejected",
            trace_id=trace_id,
            email=email,
            reason="invalid_credentials",
        )
        raise AuthServiceError("invalid_credentials")

    def _raise_registration_error(
        self,
        exc: Exception,
        trace_id: Optional[str],
        email: str,
    ) -> None:
        if isinstance(exc, AuthServiceError):
            self._log_auth(
                "register",
                "rejected",
                trace_id=trace_id,
                email=email,
                reason=exc.code,
            )
            raise exc
        if isinstance(exc, IntegrityError):
            self._log_auth(
                "register",
                "rejected",
                trace_id=trace_id,
                email=email,
                reason="email_already_registered",
            )
            raise AuthServiceError("email_already_registered") from exc
        logger.exception(
            "auth_event operation=register status=error trace_id=%s error_type=%s",
            trace_id or "unavailable",
            type(exc).__name__,
        )
        raise AuthServiceError("persistence_error") from exc

    def _issue_access_for_session(
        self,
        user_id: str,
        auth_session_id: str,
        issued_at: datetime,
        session_expires_at: datetime,
    ) -> tuple[str, datetime]:
        token, claims = self.create_access_token(
            user_id,
            auth_session_id,
            now=issued_at,
            absolute_expires_at=session_expires_at,
        )
        return token, self._normalize_naive_utc(claims["exp"])

    def _prepare_session_issue(
        self,
        user_id: str,
        issued_at: datetime,
    ) -> tuple[AuthSessionMaterial, str, datetime]:
        try:
            material = self.auth_sessions.create_material(now=issued_at)
            access_token, access_expires_at = self._issue_access_for_session(
                user_id,
                material.auth_session_id,
                issued_at,
                material.session_expires_at,
            )
        except AuthServiceError:
            raise
        except AuthSessionServiceError as exc:
            raise AuthServiceError(exc.code) from exc
        except Exception as exc:
            raise AuthServiceError("credential_issue_error") from exc
        return material, access_token, access_expires_at

    def _issued_session(
        self,
        user: Dict[str, Any],
        material: AuthSessionMaterial,
        access_token: str,
        access_expires_at: datetime,
    ) -> IssuedAuthSession:
        return IssuedAuthSession(
            user=self.public_user(user),
            auth_session_id=material.auth_session_id,
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=material.refresh_token,
            session_expires_at=material.session_expires_at,
        )

    def _require_configured(self) -> None:
        if not self.config.is_configured:
            raise AuthServiceError("auth_not_configured")

    @staticmethod
    def _validate_password(password: str) -> None:
        if not 8 <= len(password or "") <= 128:
            raise AuthServiceError("invalid_password")

    @staticmethod
    def _valid_user_id(value: Any) -> bool:
        if not isinstance(value, str) or not value:
            return False
        try:
            return str(UUID(value)) == value.lower()
        except (TypeError, ValueError):
            return False

    def _now(self) -> datetime:
        return self._normalize_naive_utc(self._clock())

    @staticmethod
    def _normalize_naive_utc(value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _normalize_aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _log_auth(
        operation: str,
        status: str,
        *,
        trace_id: Optional[str],
        user_id: Optional[str] = None,
        email: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        user_marker = "none"
        if user_id:
            user_marker = "user-" + hashlib.sha256(
                user_id.encode("utf-8")
            ).hexdigest()[:12]
        email_marker = "none"
        if email:
            email_marker = "email-" + hashlib.sha256(
                email.encode("utf-8")
            ).hexdigest()[:12]
        logger.info(
            "auth_event operation=%s status=%s trace_id=%s user=%s email=%s reason=%s",
            operation,
            status,
            trace_id or "unavailable",
            user_marker,
            email_marker,
            reason or "none",
        )
