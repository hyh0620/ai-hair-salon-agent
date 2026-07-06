"""Appointment API with deterministic salon scheduling rules."""

import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status

from config.trace_context import get_trace_id
from .core.response_models import AppointmentRequest, AppointmentResponse, DataResponse

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
    return appointment_service, finder


def _request_to_history(request: AppointmentRequest) -> Dict[str, Any]:
    return {
        "user_id": request.user_id,
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


@router.post(
    "/create",
    response_model=DataResponse,
    summary="创建理发店预约",
)
async def create_appointment(request: AppointmentRequest):
    """Create an appointment using catalog, schedule and conflict rules."""
    trace_id = get_trace_id()
    logger.info("appointment_route trace_id=%s service=%s stylist_id=%s", trace_id, request.project or request.service, request.stylist_id)
    try:
        appointment_service, finder = _build_services()
        details = appointment_service.build_appointment_details(_request_to_history(request))

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

        if not appointment_service.is_within_business_hours(start_time, end_time):
            logger.info("appointment_rejected trace_id=%s reason=outside_business_hours", trace_id)
            raise HTTPException(status_code=400, detail="预约时间不在营业时间内")

        stylist = None
        if request.stylist_id is not None:
            stylist = appointment_service.get_stylist_by_id(request.stylist_id)
            if not stylist:
                logger.info("appointment_rejected trace_id=%s reason=stylist_not_found", trace_id)
                raise HTTPException(status_code=404, detail="指定发型师不存在")
            if not appointment_service.is_stylist_available(stylist["id"], start_time, end_time):
                logger.info("appointment_rejected trace_id=%s reason=stylist_conflict stylist_id=%s", trace_id, stylist["id"])
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="指定发型师该时段已有预约")
        elif request.stylist_name:
            stylist = finder.find_specific_stylist(request.stylist_name, start_time, end_time)
            if not stylist:
                logger.info("appointment_rejected trace_id=%s reason=stylist_unavailable_by_name", trace_id)
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="指定发型师不存在或该时段不可预约")
        else:
            stylist = finder.find_available_stylist(details, start_time, end_time)
            if not stylist:
                logger.info("appointment_rejected trace_id=%s reason=no_available_stylist", trace_id)
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="当前条件下没有可预约发型师")

        saved = appointment_service.save_appointment(
            stylist_id=str(stylist["id"]),
            start_time=start_time,
            end_time=end_time,
            appointment_history=details,
            session_id=request.user_id,
        )
        if not saved:
            logger.info("appointment_rejected trace_id=%s reason=save_conflict", trace_id)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="预约保存失败，可能存在时段冲突")

        logger.info("appointment_response trace_id=%s status=confirmed stylist_id=%s service=%s", trace_id, stylist["id"], details["project"])
        response = AppointmentResponse(
            user_id=request.user_id,
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
        return DataResponse(message="预约创建成功", data=response.model_dump())
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建预约失败: {exc}")
