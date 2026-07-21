"""Trusted request identity selection and cookie CSRF enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hmac
import logging
import re
import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.auth_config import AuthConfig
from services.auth_service import AuthService, AuthServiceError


logger = logging.getLogger(__name__)

ANONYMOUS_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ACCOUNT_OWNER_PREFIX = "account:"
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="账户登录后签发的 JWT access token",
)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user_id: str
    email: str
    display_name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    auth_source: str


@dataclass(frozen=True)
class RequestIdentity:
    owner_id: str
    authenticated: bool
    user_id: Optional[str]
    email: Optional[str]
    display_name: Optional[str]
    auth_source: str


def get_request_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[AuthenticatedPrincipal]:
    config = AuthConfig.from_env()
    bearer_token = _bearer_token(request, credentials)
    cookie_token = request.cookies.get(config.cookie_name)
    if not bearer_token and not cookie_token:
        return None
    if not config.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="账户认证当前未配置",
        )

    service = AuthService(config=config)
    try:
        bearer_user = (
            service.verify_access_token(bearer_token).user if bearer_token else None
        )
        cookie_user = (
            service.verify_access_token(cookie_token).user if cookie_token else None
        )
    except AuthServiceError as exc:
        if exc.code == "auth_not_configured":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="账户认证当前未配置",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证凭据无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    finally:
        service.close()

    if bearer_user and cookie_user and bearer_user["id"] != cookie_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer Token 与登录 Cookie 身份不一致",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = bearer_user or cookie_user
    source = "bearer" if bearer_user else "cookie"
    return AuthenticatedPrincipal(
        user_id=user["id"],
        email=user["email"],
        display_name=user["display_name"],
        is_active=user["is_active"],
        created_at=user["created_at"],
        updated_at=user["updated_at"],
        auth_source=source,
    )


def get_required_principal(
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
) -> AuthenticatedPrincipal:
    config = AuthConfig.from_env()
    if not config.is_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="账户认证当前未配置",
        )
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def resolve_request_identity(
    principal: Optional[AuthenticatedPrincipal],
    anonymous_owner_id: Optional[str],
    *,
    legacy_fallback: Optional[str] = None,
) -> RequestIdentity:
    if principal is not None:
        return RequestIdentity(
            owner_id=f"{ACCOUNT_OWNER_PREFIX}{principal.user_id}",
            authenticated=True,
            user_id=principal.user_id,
            email=principal.email,
            display_name=principal.display_name,
            auth_source=principal.auth_source,
        )

    candidate = (anonymous_owner_id or "").strip()
    if not candidate and legacy_fallback:
        candidate = legacy_fallback.strip()
        logger.warning("anonymous_owner_legacy_fallback")
    if not candidate:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="游客预约标识不能为空",
        )
    if candidate.lower().startswith(ACCOUNT_OWNER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="游客不能使用账户预约标识",
        )
    if not ANONYMOUS_OWNER_PATTERN.fullmatch(candidate):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="游客预约标识格式无效",
        )
    return RequestIdentity(
        owner_id=candidate,
        authenticated=False,
        user_id=None,
        email=None,
        display_name=None,
        auth_source="anonymous",
    )


def get_request_identity(
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
    anonymous_owner_id: Optional[str] = Header(
        default=None,
        alias="X-Anonymous-Owner-ID",
    ),
) -> RequestIdentity:
    """Resolve the trusted account or validated browser guest identity."""
    return resolve_request_identity(principal, anonymous_owner_id)


def enforce_csrf(
    request: Request,
    principal: Optional[AuthenticatedPrincipal],
) -> None:
    if principal is None or principal.auth_source != "cookie":
        return
    config = AuthConfig.from_env()
    cookie_token = request.cookies.get(config.csrf_cookie_name, "")
    header_token = request.headers.get("X-CSRF-Token", "")
    if (
        not cookie_token
        or not header_token
        or not hmac.compare_digest(cookie_token, header_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF 校验失败",
        )


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _bearer_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    authorization = request.headers.get("Authorization")
    if not authorization:
        return None
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not credentials.credentials.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization Bearer 格式无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials.strip()
