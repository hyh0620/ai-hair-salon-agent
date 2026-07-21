"""Registration, login, current-user and browser logout endpoints."""

from __future__ import annotations

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
    AuthSessionData,
    AuthSessionResponse,
    LoginRequest,
    RegisterRequest,
    UserPublic,
)
from config.auth_config import AuthConfig
from config.trace_context import get_trace_id
from services.auth_service import AuthService, AuthServiceError


router = APIRouter(prefix="/api/auth", tags=["账户认证"])


@router.post(
    "/register",
    response_model=AuthSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册本地账户",
    description="创建使用 Argon2 哈希密码的本地账户，并设置 HttpOnly JWT Cookie。",
)
def register(
    payload: RegisterRequest,
    response: Response,
    x_chat_session_id: Optional[str] = Header(default=None),
):
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
)
def login(
    payload: LoginRequest,
    response: Response,
    x_chat_session_id: Optional[str] = Header(default=None),
):
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
