"""Identity-scoped user behavior analysis and reminder APIs."""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from api.auth_dependencies import (
    AuthenticatedPrincipal,
    RequestIdentity,
    enforce_csrf,
    get_request_identity,
    get_request_principal,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/user-behavior", tags=["用户行为分析"])
router_underscore = APIRouter(prefix="/api/user_behavior", tags=["用户行为分析"])


class UserAnalysisViewer(BaseModel):
    """Safe viewer metadata for the customer-facing analysis page."""

    mode: Literal["account", "anonymous"]
    display_name: str


class UserAnalysisResponse(BaseModel):
    """User analysis response without internal owner identifiers."""

    favorite_stylist_id: Optional[int] = None
    favorite_stylist_name: Optional[str] = None
    favorite_service: Optional[str] = None
    favorite_duration: Optional[int] = None
    total_appointments: int = 0
    days_since_last_appointment: Optional[int] = None
    should_send_reminder: bool = False
    viewer: UserAnalysisViewer


class ReminderResponse(BaseModel):
    """Reminder message response."""

    message: str
    stylist_available_times: list[dict[str, Any]] = Field(default_factory=list)


def _viewer_for(identity: RequestIdentity) -> UserAnalysisViewer:
    if identity.authenticated:
        return UserAnalysisViewer(
            mode="account",
            display_name=identity.display_name or "账户用户",
        )
    return UserAnalysisViewer(mode="anonymous", display_name="游客")


def _empty_analysis(identity: RequestIdentity) -> UserAnalysisResponse:
    return UserAnalysisResponse(viewer=_viewer_for(identity))


def get_user_analysis(identity: RequestIdentity) -> UserAnalysisResponse:
    """Build analysis for one trusted owner selected at the HTTP boundary."""
    try:
        from agents.user_behavior_agent import UserBehaviorAgent

        agent = UserBehaviorAgent()
        analysis = agent.get_user_analysis(identity.owner_id)
        if not analysis:
            return _empty_analysis(identity)

        stylist_name = None
        if analysis.get("favorite_stylist_id"):
            from db import DatabaseRouter

            db = DatabaseRouter()
            stylist_info = db.stylists.get_stylist_by_id(
                analysis["favorite_stylist_id"]
            )
            if stylist_info:
                stylist_name = stylist_info.get("name")

        return UserAnalysisResponse(
            favorite_stylist_id=analysis.get("favorite_stylist_id"),
            favorite_stylist_name=stylist_name,
            favorite_service=analysis.get("favorite_service"),
            favorite_duration=analysis.get("favorite_duration"),
            total_appointments=analysis.get("total_appointments", 0),
            days_since_last_appointment=analysis.get(
                "days_since_last_appointment"
            ),
            should_send_reminder=analysis.get("should_send_reminder", False),
            viewer=_viewer_for(identity),
        )
    except Exception:
        logger.exception("user_analysis_failed")
        return _empty_analysis(identity)


@router.get(
    "/analysis",
    response_model=UserAnalysisResponse,
    summary="获取用户服务偏好分析",
)
async def user_analysis(
    identity: RequestIdentity = Depends(get_request_identity),
):
    return get_user_analysis(identity)


@router.get(
    "/dashboard_data",
    response_model=UserAnalysisResponse,
    summary="获取用户行为仪表板数据",
)
async def dashboard_data(
    identity: RequestIdentity = Depends(get_request_identity),
):
    return get_user_analysis(identity)


@router_underscore.get(
    "/dashboard_data",
    response_model=UserAnalysisResponse,
    include_in_schema=False,
)
async def dashboard_data_underscore(
    identity: RequestIdentity = Depends(get_request_identity),
):
    return get_user_analysis(identity)


@router.post(
    "/send-reminder",
    response_model=ReminderResponse,
    summary="生成当前用户的回访提醒",
)
async def send_reminder(
    request: Request,
    identity: RequestIdentity = Depends(get_request_identity),
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
):
    """Generate a reminder for the trusted request identity."""
    enforce_csrf(request, principal)
    try:
        from agents.user_behavior_agent import UserBehaviorAgent

        agent = UserBehaviorAgent()
        result = await agent.get_reminder_with_schedule(
            identity.owner_id,
            display_name=identity.display_name if identity.authenticated else None,
        )
        return ReminderResponse(
            message=result["message"],
            stylist_available_times=result.get("stylist_available_times", []),
        )
    except Exception:
        logger.exception("user_reminder_failed")
        return ReminderResponse(
            message="您好，系统暂时无法生成回访建议，请稍后再试。",
        )
