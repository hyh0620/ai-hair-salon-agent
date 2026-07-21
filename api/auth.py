"""Registration, login, current-user and browser logout endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from api.auth_dependencies import (
    AuthenticatedPrincipal,
    enforce_csrf,
    generate_csrf_token,
    get_required_principal,
)
from api.chat_handler import get_chat_session_registry
from api.core.auth_models import (
    AuthLogoutData,
    AuthLogoutResponse,
    AuthMeData,
    AuthMeResponse,
    AuthRateLimitResponse,
    AuthSessionData,
    AuthSessionResponse,
    LoginRequest,
    RegisterRequest,
    UserPublic,
)
from config.auth_config import AuthConfig
from config.trace_context import get_trace_id
from services.auth_rate_limit_service import (
    LOGIN_CLIENT_ACCOUNT_SCOPE,
    LOGIN_CLIENT_SCOPE,
    REGISTER_CLIENT_SCOPE,
    AuthRateLimiter,
    account_fingerprint,
    client_fingerprint,
    login_pair_fingerprint,
)
from services.auth_service import AuthService, AuthServiceError


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["账户认证"])
RATE_LIMIT_DETAIL = "请求过于频繁，请稍后再试"
RATE_LIMIT_RESPONSES = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": AuthRateLimitResponse,
        "description": "登录或注册请求超过当前进程内的安全频率限制。",
        "headers": {
            "Retry-After": {
                "description": "再次尝试前至少等待的秒数。",
                "schema": {"type": "integer", "minimum": 1},
            }
        },
    }
}


def get_auth_rate_limiter(request: Request) -> AuthRateLimiter:
    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    if not isinstance(limiter, AuthRateLimiter):
        raise RuntimeError("authentication rate limiter is not initialized")
    return limiter


@router.post(
    "/register",
    response_model=AuthSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册本地账户",
    description="创建使用 Argon2 哈希密码的本地账户，并设置 HttpOnly JWT Cookie。",
    responses=RATE_LIMIT_RESPONSES,
)
def register(
    request: Request,
    payload: RegisterRequest,
    response: Response,
    rate_limiter: AuthRateLimiter = Depends(get_auth_rate_limiter),
    x_chat_session_id: Optional[str] = Header(default=None),
):
    _enforce_register_rate_limit(request, rate_limiter)
    service = AuthService()
    try:
        user = service.register_user(
            email=str(payload.email),
            display_name=payload.display_name,
            password=payload.password,
            trace_id=get_trace_id(),
        )
        token, _ = service.create_access_token(user["id"])
        session_id = _rotate_chat_session(x_chat_session_id)
        _set_auth_cookies(response, token, generate_csrf_token(), service.config)
        return _auth_response("账户注册成功", user, token, session_id, service.config)
    except AuthServiceError as exc:
        _raise_auth_error(exc, registering=True)
    finally:
        service.close()


@router.post(
    "/login",
    response_model=AuthSessionResponse,
    summary="登录本地账户",
    description="验证 Argon2 密码并签发带 issuer、audience 和到期时间的访问 Token。",
    responses=RATE_LIMIT_RESPONSES,
)
def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    rate_limiter: AuthRateLimiter = Depends(get_auth_rate_limiter),
    x_chat_session_id: Optional[str] = Header(default=None),
):
    pair_key = _enforce_login_rate_limit(request, payload.email, rate_limiter)
    service = AuthService()
    try:
        user = service.authenticate_user(
            email=str(payload.email),
            password=payload.password,
            trace_id=get_trace_id(),
        )
        token, _ = service.create_access_token(user["id"])
        session_id = _rotate_chat_session(x_chat_session_id)
        _set_auth_cookies(response, token, generate_csrf_token(), service.config)
        if pair_key is not None:
            rate_limiter.reset(LOGIN_CLIENT_ACCOUNT_SCOPE, pair_key)
        return _auth_response("登录成功", user, token, session_id, service.config)
    except AuthServiceError as exc:
        _raise_auth_error(exc, registering=False)
    finally:
        service.close()


@router.get(
    "/me",
    response_model=AuthMeResponse,
    summary="查询当前登录账户",
)
def me(
    principal: AuthenticatedPrincipal = Depends(get_required_principal),
):
    return AuthMeResponse(
        message="已获取当前账户",
        data=AuthMeData(
            user=UserPublic(
                id=principal.user_id,
                email=principal.email,
                display_name=principal.display_name,
                is_active=principal.is_active,
                created_at=principal.created_at,
                updated_at=principal.updated_at,
            ),
            auth_source=principal.auth_source,
        ),
    )


@router.post(
    "/logout",
    response_model=AuthLogoutResponse,
    summary="退出当前浏览器登录",
    description="清除浏览器认证 Cookie；本 MVP 不维护服务端 Token 黑名单。",
)
def logout(
    request: Request,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(get_required_principal),
    x_chat_session_id: Optional[str] = Header(default=None),
):
    enforce_csrf(request, principal)
    config = AuthConfig.from_env()
    response.delete_cookie(
        config.cookie_name,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite=config.cookie_samesite,
    )
    response.delete_cookie(
        config.csrf_cookie_name,
        path="/",
        secure=config.cookie_secure,
        httponly=False,
        samesite=config.cookie_samesite,
    )
    return AuthLogoutResponse(
        message="已退出登录",
        data=AuthLogoutData(session_id=_rotate_chat_session(x_chat_session_id)),
    )


def _auth_response(
    message: str,
    user: dict,
    token: str,
    session_id: str,
    config: AuthConfig,
) -> AuthSessionResponse:
    return AuthSessionResponse(
        message=message,
        data=AuthSessionData(
            user=UserPublic.model_validate(user),
            access_token=token,
            expires_in=config.max_age_seconds,
            session_id=session_id,
        ),
    )


def _set_auth_cookies(
    response: Response,
    token: str,
    csrf_token: str,
    config: AuthConfig,
) -> None:
    response.set_cookie(
        config.cookie_name,
        token,
        max_age=config.max_age_seconds,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite=config.cookie_samesite,
    )
    response.set_cookie(
        config.csrf_cookie_name,
        csrf_token,
        max_age=config.max_age_seconds,
        path="/",
        secure=config.cookie_secure,
        httponly=False,
        samesite=config.cookie_samesite,
    )


def _rotate_chat_session(session_id: Optional[str]) -> str:
    return get_chat_session_registry().reset(session_id)


def _enforce_register_rate_limit(
    request: Request,
    limiter: AuthRateLimiter,
) -> None:
    config = limiter.config
    if not config.enabled:
        return
    decision = limiter.consume(
        REGISTER_CLIENT_SCOPE,
        client_fingerprint(_client_host(request)),
        config.register_client_limit,
        config.register_client_window_seconds,
    )
    if not decision.allowed:
        _raise_rate_limited("register", REGISTER_CLIENT_SCOPE, decision.retry_after_seconds)


def _enforce_login_rate_limit(
    request: Request,
    email: str,
    limiter: AuthRateLimiter,
) -> Optional[str]:
    config = limiter.config
    if not config.enabled:
        return None

    client_key = client_fingerprint(_client_host(request))
    pair_key = login_pair_fingerprint(client_key, account_fingerprint(email))
    decisions = (
        (
            LOGIN_CLIENT_SCOPE,
            limiter.consume(
                LOGIN_CLIENT_SCOPE,
                client_key,
                config.login_client_limit,
                config.login_client_window_seconds,
            ),
        ),
        (
            LOGIN_CLIENT_ACCOUNT_SCOPE,
            limiter.consume(
                LOGIN_CLIENT_ACCOUNT_SCOPE,
                pair_key,
                config.login_client_account_limit,
                config.login_client_account_window_seconds,
            ),
        ),
    )
    blocked = [(scope, decision) for scope, decision in decisions if not decision.allowed]
    if blocked:
        strict_scope, strict_decision = max(
            blocked,
            key=lambda item: item[1].retry_after_seconds,
        )
        _raise_rate_limited(
            "login",
            strict_scope,
            strict_decision.retry_after_seconds,
        )
    return pair_key


def _client_host(request: Request) -> Optional[str]:
    return request.client.host if request.client is not None else None


def _raise_rate_limited(operation: str, scope: str, retry_after_seconds: int) -> None:
    retry_after = max(1, int(retry_after_seconds))
    logger.warning(
        "auth_rate_limit operation=%s status=rate_limited scope=%s "
        "trace_id=%s retry_after_seconds=%s",
        operation,
        scope,
        get_trace_id(),
        retry_after,
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=RATE_LIMIT_DETAIL,
        headers={"Retry-After": str(retry_after)},
    )


def _raise_auth_error(exc: AuthServiceError, *, registering: bool) -> None:
    if exc.code == "auth_not_configured":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="账户认证当前未配置",
        ) from exc
    if registering and exc.code == "email_already_registered":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已经注册",
        ) from exc
    if not registering and exc.code in {"invalid_credentials", "invalid_email"}:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if exc.code in {"invalid_email", "invalid_password", "invalid_display_name"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="注册信息格式无效",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="账户服务暂时不可用",
    ) from exc
