"""Registration, login, refresh, current-user and current-session logout."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from api.auth_dependencies import (
    AuthenticatedPrincipal,
    bearer_scheme,
    enforce_cookie_csrf,
    extract_bearer_token,
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
    AuthRefreshConflictResponse,
    AuthRefreshData,
    AuthRefreshInvalidResponse,
    AuthRefreshResponse,
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
    REFRESH_CLIENT_SCOPE,
    REGISTER_CLIENT_SCOPE,
    AuthRateLimiter,
    account_fingerprint,
    client_fingerprint,
    login_pair_fingerprint,
)
from services.auth_service import AuthService, AuthServiceError, IssuedAuthSession


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["账户认证"])
RATE_LIMIT_DETAIL = "请求过于频繁，请稍后再试"
REFRESH_INVALID_DETAIL = "登录状态已失效，请重新登录"
REFRESH_CONFLICT_DETAIL = "登录状态正在刷新，请重试"
RATE_LIMIT_RESPONSES = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": AuthRateLimitResponse,
        "description": "认证请求超过当前进程内的安全频率限制。",
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
    description="原子创建本地账户与可吊销认证会话，并设置 HttpOnly 认证 Cookie。",
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
        issued = service.register_with_session(
            email=str(payload.email),
            display_name=payload.display_name,
            password=payload.password,
            trace_id=get_trace_id(),
        )
        chat_session_id = _rotate_chat_session(x_chat_session_id)
        _set_auth_cookies(response, issued, generate_csrf_token(), service.config)
        return _auth_response("账户注册成功", issued, chat_session_id)
    except AuthServiceError as exc:
        _raise_auth_error(exc, registering=True)
    finally:
        service.close()


@router.post(
    "/login",
    response_model=AuthSessionResponse,
    summary="登录本地账户",
    description="验证 Argon2 密码，为本次登录创建独立认证会话并签发短期访问令牌。",
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
        issued = service.authenticate_with_session(
            email=str(payload.email),
            password=payload.password,
            trace_id=get_trace_id(),
        )
        chat_session_id = _rotate_chat_session(x_chat_session_id)
        _set_auth_cookies(response, issued, generate_csrf_token(), service.config)
        if pair_key is not None:
            rate_limiter.reset(LOGIN_CLIENT_ACCOUNT_SCOPE, pair_key)
        return _auth_response("登录成功", issued, chat_session_id)
    except AuthServiceError as exc:
        _raise_auth_error(exc, registering=False)
    finally:
        service.close()


@router.post(
    "/refresh",
    response_model=AuthRefreshResponse,
    summary="轮换当前登录凭据",
    description="使用 HttpOnly Cookie 中的一次性刷新令牌原子轮换登录凭据。",
    responses={
        **RATE_LIMIT_RESPONSES,
        status.HTTP_401_UNAUTHORIZED: {
            "model": AuthRefreshInvalidResponse,
            "description": "刷新令牌或服务端认证会话无效。",
        },
        status.HTTP_409_CONFLICT: {
            "model": AuthRefreshConflictResponse,
            "description": "同一个刷新令牌正在被另一个请求轮换。",
            "headers": {
                "Retry-After": {
                    "description": "再次尝试前等待的秒数。",
                    "schema": {"type": "integer", "minimum": 1},
                }
            },
        },
    },
)
def refresh(
    request: Request,
    response: Response,
    rate_limiter: AuthRateLimiter = Depends(get_auth_rate_limiter),
):
    _enforce_refresh_rate_limit(request, rate_limiter)
    config = AuthConfig.from_env()
    raw_refresh_token = request.cookies.get(config.refresh_cookie_name, "")
    if not raw_refresh_token:
        return _refresh_invalid_response(config)
    enforce_cookie_csrf(request)

    service = AuthService(config=config)
    try:
        result = service.refresh_session(
            raw_refresh_token,
            trace_id=get_trace_id(),
        )
        if result.status == "concurrent":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": REFRESH_CONFLICT_DETAIL},
                headers={"Retry-After": "1"},
            )
        if result.status != "success":
            return _refresh_invalid_response(config)
        _set_credential_cookies(
            response,
            access_token=result.access_token,
            refresh_token=result.refresh_token,
            access_expires_at=result.access_expires_at,
            session_expires_at=result.session_expires_at,
            csrf_token=generate_csrf_token(),
            config=config,
        )
        return AuthRefreshResponse(
            message="登录状态已刷新",
            data=AuthRefreshData(
                user=UserPublic.model_validate(result.user),
                access_token=result.access_token,
                expires_in=_seconds_until(result.access_expires_at),
            ),
        )
    except AuthServiceError as exc:
        if exc.code == "auth_not_configured":
            _raise_auth_error(exc, registering=False)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="账户服务暂时不可用",
        ) from exc
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
    summary="退出当前认证会话",
    description="撤销当前服务端认证会话、轮换对话会话并清除认证 Cookie。",
)
def logout(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_chat_session_id: Optional[str] = Header(default=None),
):
    config = AuthConfig.from_env()
    bearer_token = extract_bearer_token(request, credentials)
    access_cookie = request.cookies.get(config.cookie_name, "")
    refresh_cookie = request.cookies.get(config.refresh_cookie_name, "")
    if not bearer_token and (access_cookie or refresh_cookie):
        enforce_cookie_csrf(request)

    if config.is_configured:
        service = AuthService(config=config)
        try:
            revoked = False
            access_token = bearer_token or access_cookie
            if access_token:
                try:
                    claims = service.decode_access_token(access_token)
                    revoked = service.revoke_session(
                        auth_session_id=str(claims["sid"]),
                        user_id=str(claims["sub"]),
                        trace_id=get_trace_id(),
                    )
                except AuthServiceError as exc:
                    if exc.code != "invalid_token":
                        raise
            if not bearer_token and not revoked and refresh_cookie:
                service.revoke_session_by_refresh_token(
                    refresh_cookie,
                    trace_id=get_trace_id(),
                )
        except AuthServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="账户服务暂时不可用",
            ) from exc
        finally:
            service.close()

    _clear_auth_cookies(response, config)
    return AuthLogoutResponse(
        message="已退出登录",
        data=AuthLogoutData(session_id=_rotate_chat_session(x_chat_session_id)),
    )


def _auth_response(
    message: str,
    issued: IssuedAuthSession,
    chat_session_id: str,
) -> AuthSessionResponse:
    return AuthSessionResponse(
        message=message,
        data=AuthSessionData(
            user=UserPublic.model_validate(issued.user),
            access_token=issued.access_token,
            expires_in=_seconds_until(issued.access_expires_at),
            session_id=chat_session_id,
        ),
    )


def _set_auth_cookies(
    response: Response,
    issued: IssuedAuthSession,
    csrf_token: str,
    config: AuthConfig,
) -> None:
    _set_credential_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        access_expires_at=issued.access_expires_at,
        session_expires_at=issued.session_expires_at,
        csrf_token=csrf_token,
        config=config,
    )


def _set_credential_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    access_expires_at: datetime,
    session_expires_at: datetime,
    csrf_token: str,
    config: AuthConfig,
) -> None:
    access_max_age = _seconds_until(access_expires_at)
    refresh_max_age = _seconds_until(session_expires_at)
    response.set_cookie(
        config.cookie_name,
        access_token,
        max_age=access_max_age,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite=config.cookie_samesite,
    )
    response.set_cookie(
        config.refresh_cookie_name,
        refresh_token,
        max_age=refresh_max_age,
        path=config.refresh_cookie_path,
        secure=config.cookie_secure,
        httponly=True,
        samesite=config.cookie_samesite,
    )
    response.set_cookie(
        config.csrf_cookie_name,
        csrf_token,
        max_age=refresh_max_age,
        path="/",
        secure=config.cookie_secure,
        httponly=False,
        samesite=config.cookie_samesite,
    )


def _clear_auth_cookies(response: Response, config: AuthConfig) -> None:
    response.delete_cookie(
        config.cookie_name,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite=config.cookie_samesite,
    )
    response.delete_cookie(
        config.refresh_cookie_name,
        path=config.refresh_cookie_path,
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


def _refresh_invalid_response(config: AuthConfig) -> JSONResponse:
    response = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": REFRESH_INVALID_DETAIL},
    )
    _clear_auth_cookies(response, config)
    return response


def _seconds_until(value: Optional[datetime]) -> int:
    if value is None:
        return 1
    expires_at = value
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))


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


def _enforce_refresh_rate_limit(request: Request, limiter: AuthRateLimiter) -> None:
    config = limiter.config
    if not config.enabled:
        return
    decision = limiter.consume(
        REFRESH_CLIENT_SCOPE,
        client_fingerprint(_client_host(request)),
        config.refresh_client_limit,
        config.refresh_client_window_seconds,
    )
    if not decision.allowed:
        _raise_rate_limited("refresh", REFRESH_CLIENT_SCOPE, decision.retry_after_seconds)


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
