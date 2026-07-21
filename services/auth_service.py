"""Argon2 account credentials and strictly validated JWT access tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from email_validator import EmailNotValidError, validate_email
import jwt
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError

from config.auth_config import AuthConfig
from config.time_config import utc_now_naive
from db.db_router import DatabaseRouter


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


class AuthService:
    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        config: Optional[AuthConfig] = None,
    ):
        self.config = config or AuthConfig.from_env()
        self.db_router = DatabaseRouter(db_path)
        self.user_repo = self.db_router.users
        self.password_hash = PasswordHash.recommended()

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
        self._require_configured()
        normalized_email = self.normalize_email(email)
        normalized_name = self.normalize_display_name(display_name)
        self._validate_password(password)
        encoded_password = self.password_hash.hash(password)
        now = utc_now_naive()
        try:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                if self.user_repo.get_by_email_in_session(session, normalized_email):
                    raise AuthServiceError("email_already_registered")
                user = self.user_repo.add_user_in_session(
                    session,
                    user_id=str(uuid4()),
                    email=normalized_email,
                    display_name=normalized_name,
                    password_hash=encoded_password,
                    now=now,
                )
        except AuthServiceError:
            self._log_auth(
                "register",
                "rejected",
                trace_id=trace_id,
                email=normalized_email,
                reason="email_already_registered",
            )
            raise
        except IntegrityError as exc:
            self._log_auth(
                "register",
                "rejected",
                trace_id=trace_id,
                email=normalized_email,
                reason="email_already_registered",
            )
            raise AuthServiceError("email_already_registered") from exc
        except Exception as exc:
            logger.exception(
                "auth_event operation=register status=error trace_id=%s error_type=%s",
                trace_id or "unavailable",
                type(exc).__name__,
            )
            raise AuthServiceError("persistence_error") from exc

        self._log_auth(
            "register",
            "success",
            trace_id=trace_id,
            user_id=user["id"],
        )
        return self.public_user(user)

    def authenticate_user(
        self,
        *,
        email: str,
        password: str,
        trace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_configured()
        normalized_email = self.normalize_email(email)
        if not 8 <= len(password or "") <= 128:
            self._log_auth(
                "login",
                "rejected",
                trace_id=trace_id,
                email=normalized_email,
                reason="invalid_credentials",
            )
            raise AuthServiceError("invalid_credentials")
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
            self._log_auth(
                "login",
                "rejected",
                trace_id=trace_id,
                email=normalized_email,
                reason="invalid_credentials",
            )
            raise AuthServiceError("invalid_credentials")

        if updated_hash:
            with self.db_router.session_manager.session_scope(immediate=True) as session:
                self.user_repo.update_password_hash_in_session(
                    session,
                    user_id=user["id"],
                    password_hash=updated_hash,
                    now=utc_now_naive(),
                )

        self._log_auth(
            "login",
            "success",
            trace_id=trace_id,
            user_id=user["id"],
        )
        return self.public_user(user)

    def create_access_token(
        self,
        user_id: str,
        *,
        now: Optional[datetime] = None,
        expires_delta: Optional[timedelta] = None,
    ) -> tuple[str, Dict[str, Any]]:
        self._require_configured()
        issued_at = now or datetime.now(timezone.utc)
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)
        expires_at = issued_at + (
            expires_delta or timedelta(minutes=self.config.access_token_minutes)
        )
        claims: Dict[str, Any] = {
            "sub": str(user_id),
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
                    "require": ["sub", "type", "iat", "exp", "iss", "aud", "jti"],
                },
            )
        except InvalidTokenError as exc:
            raise AuthServiceError("invalid_token") from exc

        subject = claims.get("sub")
        if claims.get("type") != "access" or not self._valid_user_id(subject):
            raise AuthServiceError("invalid_token")

        with self.db_router.session_manager.session_scope() as session:
            user = self.user_repo.get_by_id_in_session(session, str(subject))
        if not user or not user.get("is_active"):
            raise AuthServiceError("invalid_token")
        return VerifiedAccessToken(self.public_user(user), claims)

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
        email_marker = "none"
        if email:
            digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
            email_marker = f"email-{digest}"
        logger.info(
            "auth_event operation=%s status=%s trace_id=%s user_id=%s email=%s reason=%s",
            operation,
            status,
            trace_id or "unavailable",
            user_id or "none",
            email_marker,
            reason or "none",
        )
