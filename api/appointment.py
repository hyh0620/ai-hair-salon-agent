"""Appointment API with deterministic salon scheduling rules."""

import logging
from datetime import date, datetime
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from config.trace_context import get_trace_id
from services.availability_service import AvailabilitySearchRequest, AvailabilityService
from services.service_catalog import normalize_specialty
from .auth_dependencies import (
    AuthenticatedPrincipal,
    enforce_csrf,
    get_request_principal,
    resolve_request_identity,
)
from .core.response_models import (
    AppointmentCreateResponse,
    AppointmentCancelRequest,
    AppointmentDetailData,
    AppointmentDetailResponse,
    AppointmentLifecycleItem,
    AppointmentListData,
    AppointmentListResponse,
    AppointmentOperationData,
    AppointmentOperationResponse,
    AppointmentRequest,
    AppointmentResponse,
    AppointmentSelectionResponse,
    AppointmentUpdateRequest,
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
        "登录账户的 owner 来自 JWT；游客必须提交普通 user_id。"
        "LLM、MCP 与 RAG 不参与最终业务裁决。"
    ),
)
async def create_appointment(
    request: AppointmentRequest,
    http_request: Request,
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
):
    """Create an appointment using catalog, schedule and conflict rules."""
    trace_id = get_trace_id()
    identity = resolve_request_identity(principal, request.user_id)
    enforce_csrf(http_request, principal)
    tracking_user_id = identity.owner_id
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
            owner_id=tracking_user_id,
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
        logger.exception(
            "appointment_create_failed trace_id=%s error_type=%s",
            trace_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="创建预约失败")


_LIFECYCLE_ERROR_RESPONSES = {
    400: {"model": AppointmentOperationResponse},
    404: {"model": AppointmentOperationResponse},
    409: {"model": AppointmentOperationResponse},
    422: {"model": AppointmentOperationResponse},
    500: {"model": AppointmentOperationResponse},
}


def _lifecycle_http_status(result_status: str) -> int:
    if result_status == "not_found":
        return status.HTTP_404_NOT_FOUND
    if result_status in {"conflict", "stale_state", "not_modifiable"}:
        return status.HTTP_409_CONFLICT
    if result_status in {"invalid_time", "outside_business_hours", "service_not_supported"}:
        return status.HTTP_400_BAD_REQUEST
    if result_status == "validation_error":
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    if result_status == "persistence_error":
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    return status.HTTP_200_OK


def _operation_response(result, message: str):
    response = AppointmentOperationResponse(
        message=message,
        data=AppointmentOperationData(
            status=result.status,
            appointment=(
                AppointmentLifecycleItem.model_validate(result.appointment)
                if result.appointment
                else None
            ),
            current_version=result.current_version,
            reason=result.reason,
        ),
    )
    http_status = _lifecycle_http_status(result.status)
    if http_status == status.HTTP_200_OK:
        return response
    return JSONResponse(
        status_code=http_status,
        content=jsonable_encoder(response),
    )


@router.get(
    "",
    response_model=AppointmentListResponse,
    responses=_LIFECYCLE_ERROR_RESPONSES,
    summary="查询当前调用者的预约",
    description=(
        "登录请求使用 JWT 身份；游客按 user_id 查询。默认只返回未来、confirmed 状态的预约，"
        "并按开始时间和预约 ID 稳定排序。游客 user_id 不是认证凭据。"
    ),
)
async def list_appointments(
    user_id: Optional[str] = Query(default=None, min_length=1),
    future_only: bool = True,
    target_date: Optional[date] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    appointment_status: Literal["confirmed", "cancelled", "completed", "all"] = "confirmed",
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
):
    identity = resolve_request_identity(principal, user_id)
    appointment_service, _, _ = _build_services()
    statuses = (
        ("confirmed", "cancelled", "completed")
        if appointment_status == "all"
        else (appointment_status,)
    )
    result = appointment_service.list_user_appointments(
        identity.owner_id,
        future_only=future_only,
        target_date=target_date,
        date_from=date_from,
        date_to=date_to,
        statuses=statuses,
        trace_id=get_trace_id(),
    )
    if not result.success:
        return _operation_response(result, "预约查询失败")
    return AppointmentListResponse(
        message="预约查询成功",
        data=AppointmentListData(
            appointments=[
                AppointmentLifecycleItem.model_validate(item)
                for item in result.appointments
            ]
        ),
    )


@router.get(
    "/{appointment_id}",
    response_model=AppointmentDetailResponse,
    responses=_LIFECYCLE_ERROR_RESPONSES,
    summary="查询单笔预约",
    description=(
        "使用 appointment_id 与可信登录身份或游客 user_id 联合查询最新数据库事实。"
        "不存在或不属于当前调用者时，"
        "统一返回 not_found，避免泄露其他调用者的预约信息。"
    ),
)
async def get_appointment(
    appointment_id: int,
    user_id: Optional[str] = Query(default=None, min_length=1),
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
):
    identity = resolve_request_identity(principal, user_id)
    appointment_service, _, _ = _build_services()
    result = appointment_service.get_user_appointment(
        appointment_id,
        identity.owner_id,
        trace_id=get_trace_id(),
    )
    if not result.success:
        return _operation_response(result, "未找到预约")
    return AppointmentDetailResponse(
        message="预约查询成功",
        data=AppointmentDetailData(
            appointment=AppointmentLifecycleItem.model_validate(result.appointment)
        ),
    )


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentOperationResponse,
    responses=_LIFECYCLE_ERROR_RESPONSES,
    summary="取消预约",
    description=(
        "在一个 BEGIN IMMEDIATE 事务内校验调用者、状态和 expected_version，"
        "同时将 appointment 与对应 schedule 更新为 cancelled。"
    ),
)
async def cancel_appointment(
    appointment_id: int,
    payload: AppointmentCancelRequest,
    request: Request,
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
):
    identity = resolve_request_identity(principal, payload.user_id)
    enforce_csrf(request, principal)
    appointment_service, _, _ = _build_services()
    result = appointment_service.cancel_appointment(
        appointment_id,
        identity.owner_id,
        payload.expected_version,
        trace_id=get_trace_id(),
    )
    messages = {
        "success": "预约已取消",
        "already_cancelled": "预约已经取消，无需重复操作",
        "not_found": "未找到预约",
        "stale_state": "预约状态已变化，请重新查询后操作",
        "not_modifiable": "该预约当前不能取消",
    }
    return _operation_response(result, messages.get(result.status, "取消预约失败"))


@router.patch(
    "/{appointment_id}",
    response_model=AppointmentOperationResponse,
    responses=_LIFECYCLE_ERROR_RESPONSES,
    summary="修改或改期预约",
    description=(
        "合并未提供字段后，从 SERVICE_CATALOG 重新计算价格、标准时长和结束时间，"
        "在一个 BEGIN IMMEDIATE 事务内完成版本校验、服务能力校验、冲突检查及两表更新。"
    ),
)
async def update_appointment(
    appointment_id: int,
    payload: AppointmentUpdateRequest,
    request: Request,
    principal: Optional[AuthenticatedPrincipal] = Depends(get_request_principal),
):
    identity = resolve_request_identity(principal, payload.user_id)
    enforce_csrf(request, principal)
    appointment_service, _, _ = _build_services()
    result = appointment_service.update_appointment(
        appointment_id,
        identity.owner_id,
        payload.expected_version,
        target_date=payload.target_date,
        target_time=payload.start_time,
        stylist_id=payload.stylist_id,
        stylist_name=payload.stylist_name,
        service_value=payload.project or payload.service,
        trace_id=get_trace_id(),
    )
    messages = {
        "success": "预约修改成功",
        "no_change": "预约信息没有变化",
        "not_found": "未找到预约",
        "stale_state": "预约状态已变化，请重新查询后操作",
        "not_modifiable": "该预约当前不能修改",
        "conflict": "目标档期已被占用",
        "invalid_time": "目标时间已经过去或格式无效",
        "outside_business_hours": "目标时间不在营业时间内",
        "service_not_supported": "目标发型师不支持该服务",
        "validation_error": "修改内容无效",
    }
    message = messages.get(result.status, "修改预约失败")
    if result.reason == "stylist_not_found":
        message = "指定发型师不存在"
    return _operation_response(result, message)
