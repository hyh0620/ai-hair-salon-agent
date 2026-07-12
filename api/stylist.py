"""Stylist management and persisted schedule APIs."""

from datetime import date
from typing import List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config.time_config import time_config

router = APIRouter(prefix="/api/stylists", tags=["发型师管理"])


class StylistResponse(BaseModel):
    id: int
    name: str
    gender: str | None = None
    specialties: str | None = None


class ScheduleResponse(BaseModel):
    id: int
    stylist_id: int
    start_time: str
    end_time: str
    status: str
    appointment_id: int | None = None


class BusyPeriodResponse(BaseModel):
    schedule_id: int
    appointment_id: int | None = None
    start: str
    end: str
    status: str


class StylistDayScheduleResponse(BaseModel):
    stylist_id: int
    stylist_name: str
    busy_periods: List[BusyPeriodResponse]


class BusinessHoursResponse(BaseModel):
    start: str
    end: str


class AllStylistSchedulesResponse(BaseModel):
    date: str
    business_hours: BusinessHoursResponse
    stylists: List[StylistDayScheduleResponse]


def _service():
    from services.stylist_service import StylistService

    service = StylistService()
    service.initialize_default_stylists()
    return service


def build_stylist_schedules(selected_date: date) -> AllStylistSchedulesResponse:
    """Read one day's schedule directly from the configured SQLite database."""
    stylist_service = _service()
    stylists = []
    for stylist in stylist_service.get_all_stylists():
        schedules = stylist_service.get_stylist_schedules(stylist["id"], selected_date)
        busy_periods = [
            BusyPeriodResponse(
                schedule_id=item["id"],
                appointment_id=item.get("appointment_id"),
                start=item["start_time"].strftime("%H:%M"),
                end=item["end_time"].strftime("%H:%M"),
                status=item["status"],
            )
            for item in schedules
            if item.get("status") == "busy"
        ]
        stylists.append(
            StylistDayScheduleResponse(
                stylist_id=stylist["id"],
                stylist_name=stylist["name"],
                busy_periods=busy_periods,
            )
        )
    start_hour, end_hour = time_config.get_business_hours()
    return AllStylistSchedulesResponse(
        date=selected_date.isoformat(),
        business_hours=BusinessHoursResponse(start=f"{start_hour:02d}:00", end=f"{end_hour:02d}:00"),
        stylists=stylists,
    )


@router.get(
    "/schedules",
    response_model=AllStylistSchedulesResponse,
    summary="按日期查询全部发型师排班",
    description="从 SQLite 查询指定日期的忙碌时段；该接口不读取聊天进程中的内存排班。",
)
async def get_all_stylists_schedules(
    selected_date: date = Query(alias="date", description="查询日期，格式 YYYY-MM-DD"),
):
    try:
        return build_stylist_schedules(selected_date)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取排班信息失败: {exc}")


@router.get("/schedules/today", summary="获取所有发型师今日排班")
async def get_all_stylists_schedule_today():
    try:
        response = build_stylist_schedules(time_config.today().date())
        return [
            {
                "stylist_id": item.stylist_id,
                "stylist_name": item.stylist_name,
                "busy_periods": [
                    {
                        "start": period.start,
                        "end": period.end,
                        "appointment_id": period.appointment_id,
                    }
                    for period in item.busy_periods
                ],
            }
            for item in response.stylists
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取排班信息失败: {exc}")


@router.get(
    "/",
    response_model=List[StylistResponse],
    summary="获取全部发型师",
    description="返回发型师基础信息和美发专长。",
)
async def get_all_stylists():
    try:
        return [StylistResponse(**item) for item in _service().get_all_stylists()]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取发型师信息失败: {exc}")


@router.get(
    "/{stylist_id}/schedule",
    response_model=List[ScheduleResponse],
    summary="查询单个发型师排班",
    description="按可选日期读取 SQLite 排班；不传 date 时保持兼容，查询北京时间今天。",
)
async def get_stylist_schedule(
    stylist_id: int,
    selected_date: date | None = Query(default=None, alias="date", description="查询日期，格式 YYYY-MM-DD"),
):
    try:
        stylist_service = _service()
        stylist = stylist_service.get_stylist_by_id(stylist_id)
        if not stylist:
            raise HTTPException(status_code=404, detail="发型师不存在")
        target_date = selected_date or time_config.today().date()
        schedules = stylist_service.get_stylist_schedules(stylist_id, target_date)
        return [
            ScheduleResponse(
                id=item["id"],
                stylist_id=item["stylist_id"],
                start_time=item["start_time"].strftime("%H:%M"),
                end_time=item["end_time"].strftime("%H:%M"),
                status=item["status"],
                appointment_id=item.get("appointment_id"),
            )
            for item in schedules
        ]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取排班信息失败: {exc}")


@router.get("/{stylist_id}", response_model=StylistResponse, summary="获取单个发型师信息")
async def get_stylist(stylist_id: int):
    try:
        stylist = _service().get_stylist_by_id(stylist_id)
        if not stylist:
            raise HTTPException(status_code=404, detail="发型师不存在")
        return StylistResponse(**stylist)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取发型师信息失败: {exc}")
