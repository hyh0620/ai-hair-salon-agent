"""Typed public contracts for the account authentication MVP."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from api.core.response_models import BaseResponse


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("display_name 不能为空")
        return normalized


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str


class UserPublic(BaseModel):
    id: str
    email: EmailStr
    display_name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AuthSessionData(BaseModel):
    status: Literal["authenticated"] = "authenticated"
    user: UserPublic
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(gt=0)
    session_id: str


class AuthSessionResponse(BaseResponse):
    data: AuthSessionData


class AuthMeData(BaseModel):
    status: Literal["authenticated"] = "authenticated"
    user: UserPublic
    auth_source: Literal["bearer", "cookie"]


class AuthMeResponse(BaseResponse):
    data: AuthMeData


class AuthLogoutData(BaseModel):
    status: Literal["logged_out"] = "logged_out"
    session_id: str


class AuthLogoutResponse(BaseResponse):
    data: AuthLogoutData
