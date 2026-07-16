"""Appointment API with deterministic salon scheduling rules."""

import logging
from datetime import datetime
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status

from config.trace_context import get_trace_id
from services.availability_service import AvailabilitySearchRequest, AvailabilityService
from services.service_catalog import normalize_specialty
from .core.response_models import (
    AppointmentCreateResponse,
    AppointmentRequest,
    AppointmentResponse,
    AppointmentSelectionResponse,
    AvailabilityCandidateResponse,
)

router = APIRouter(prefix="/api/appointment", tags=["预约管理"])
logger = logging.getLogger(__name__)


def _build_services():
    from agents.appointment.stylist_finder import StylistFinder
    from services.appointment_service import AppointmentService
    from services.stylist_service import StylistService

    stylist_service = StylistService()
    stylist_service.initialize_default_stylists()
    appointment_service = AppointmentService()
    finder = StylistFinder(appointment_service)
    availability_service = AvailabilityService(appointment_service)
    return appointment_service, finder, availability_service


def _request_to_history(request: AppointmentRequest, tracking_user_id: str) -> Dict[str, Any]:
    return {
        "user_id": tracking_user_id,
        "project": request.project or request.service,
        "start_time": request.start_time,
        "duration": request.duration,
        "stylist_name": request.stylist_name,
        "gender": request.gender,
        "budget": request.budget,
        "style_preference": request.style_preference,
        "preference": request.preference,
        "notes": request.notes,
    }


def _candidate_response(
    request: AppointmentRequest,
    details: Dict[str, Any],
    start_time: datetime,
    end_time: datetime,
    availability_service: AvailabilityService,
) -> AppointmentCreateResponse:
    specialty = normalize_specialty(request.style_preference or request.preference)
    options = availability_service.search_available_stylists(
        AvailabilitySearchRequest(
            target_date=start_time.date(),
            range_start=start_time.time(),
            range_end=end_time.time(),
            exact_time=start_time.time(),
            service_key=details["service_key"],
            specialty=specialty,
        )
    )
    if not options:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该时间没有支持所选服务的可预约发型师",
        )
    return AppointmentCreateResponse(
        message="请选择发型师后再次提交预约；显式提交所选发型师即表示最终确认",
        data=AppointmentSelectionResponse(
            project=details["project"],
            start_time=start_time.strftime("%Y-%m-%d %H:%M"),
            duration=details["duration"],
            price=details["price"],
            candidates=[
                AvailabilityCandidateResponse.model_validate(option.to_session_dict())
                for option in options
            ],
        ),
    )


def _raise_for_save_failure(reason: str) -> None:
    if reason in {
        "past_appointment",
        "outside_business_hours",
        "unknown_service",
        "invalid_service_duration",
    }:
        detail = {
            "past_appointment": "预约时间已经过去",
            "outside_business_hours": "预约时间不在营业时间内",
            "unknown_service": "服务项目不在理发店服务目录中",
            "invalid_service_duration": "预约时长与服务目录不一致",
        }[reason]
        raise HTTPException(status_code=400, detail=detail)
    if reason == "stylist_not_found":
        raise HTTPException(status_code=404, detail="指定发型师不存在")
    if reason == "stylist_service_unsupported":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="指定发型师不支持所选服务")
    if reason == "schedule_conflict":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="指定发型师该时段已有预约")
    raise HTTPException(status_code=500, detail="预约暂时无法保存")


@router.post(
    "/create",
    response_model=AppointmentCreateResponse,
    summary="创建理发店预约",
    description=(
        "使用服务目录、营业时间、发型师排班和 SQLite 冲突校验创建预约。"
        "未指定发型师时只返回真实候选；显式提交所选发型师后才执行原子写入。"
        "LLM、MCP 与 RAG 不参与最终业务裁决。"
    ),
)
async def create_appointment(request: AppointmentRequest):
    """Create an appointment using catalog, schedule and conflict rules."""
    trace_id = get_trace_id()
    # This fallback is request-scoped tracking only, not an authenticated user identity.
    tracking_user_id = request.user_id or f"api-session-{trace_id or uuid4().hex}"
    logger.info("appointment_route trace_id=%s service=%s stylist_id=%s", trace_id, request.project or request.service, request.stylist_id)
    try:
        appointment_service, finder, availability_service = _build_services()
        details = appointment_service.build_appointment_details(
            _request_to_history(request, tracking_user_id)
        )

        if not details.get("service_key"):
            logger.info("appointment_rejected trace_id=%s reason=unknown_service", trace_id)
            raise HTTPException(status_code=400, detail="服务项目不在理发店服务目录中")

        start_time, end_time, duration_min = finder.parse_time_and_duration(
            details.get("start_time"),
            details.get("duration"),
        )
        if not start_time or not end_time or not duration_min:
            logger.info("appointment_rejected trace_id=%s reason=invalid_time_or_duration", trace_id)
            raise HTTPException(status_code=400, detail="开始时间或服务时长格式不正确")

        if not appointment_service.is_future_start_time(start_time):
            logger.info("appointment_rejected trace_id=%s reason=past_appointment", trace_id)
            raise HTTPException(status_code=400, detail="预约时间已经过去")

        if not appointment_service.is_within_business_hours(start_time, end_time):
            logger.info("appointment_rejected trace_id=%s reason=outside_business_hours", trace_id)
            raise HTTPException(status_code=400, detail="预约时间不在营业时间内")

        stylist = None
        if request.stylist_id is not None:
            stylist = appointment_service.get_stylist_by_id(request.stylist_id)
            if not stylist:
                logger.info("appointment_rejected trace_id=%s reason=stylist_not_found", trace_id)
                raise HTTPException(status_code=404, detail="指定发型师不存在")
        elif request.stylist_name:
            stylist = appointment_service.get_stylist_by_name(request.stylist_name)
            if not stylist:
                logger.info("appointment_rejected trace_id=%s reason=stylist_not_found_by_name", trace_id)
                raise HTTPException(status_code=404, detail="指定发型师不存在")
        else:
            response = _candidate_response(
                request,
                details,
                start_time,
                end_time,
                availability_service,
            )
            logger.info(
                "appointment_selection_required trace_id=%s service=%s candidate_count=%s",
                trace_id,
                details["service_key"],
                len(response.data.candidates),
            )
            return response

        saved = appointment_service.save_appointment_detailed(
            stylist_id=str(stylist["id"]),
            start_time=start_time,
            end_time=end_time,
            appointment_history=details,
            session_id=tracking_user_id,
        )
        if not saved.success:
            logger.info(
                "appointment_rejected trace_id=%s transaction_id=%s reason=%s",
                trace_id,
                saved.transaction_id,
                saved.reason,
            )
            _raise_for_save_failure(saved.reason or "persistence_error")

        logger.info("appointment_response trace_id=%s status=confirmed stylist_id=%s service=%s", trace_id, stylist["id"], details["project"])
        response = AppointmentResponse(
            appointment_id=saved.appointment_id,
            user_id=tracking_user_id,
            project=details["project"],
            start_time=start_time.strftime("%Y-%m-%d %H:%M"),
            end_time=end_time.strftime("%Y-%m-%d %H:%M"),
            duration=details["duration"],
            price=details["price"],
            status="confirmed",
            stylist_id=stylist["id"],
            stylist_name=stylist["name"],
            notes=request.notes,
        )
        return AppointmentCreateResponse(message="预约创建成功", data=response)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建预约失败: {exc}")
