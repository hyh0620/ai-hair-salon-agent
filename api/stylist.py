"""Stylist management API."""

from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


def _service():
    from services.stylist_service import StylistService

    service = StylistService()
    service.initialize_default_stylists()
    return service


@router.get("/schedules/today", summary="获取所有发型师今日排班")
async def get_all_stylists_schedule_today():
    try:
        from config.time_config import time_config

        stylist_service = _service()
        today = time_config.today()
        schedules_data = []
        for stylist in stylist_service.get_all_stylists():
            schedules = stylist_service.get_stylist_schedules(stylist["id"], today)
            busy_periods = [
                {
                    "start": item["start_time"].strftime("%H:%M"),
                    "end": item["end_time"].strftime("%H:%M"),
                    "appointment_id": item.get("appointment_id"),
                }
                for item in schedules
                if item.get("status") == "busy"
            ]
            schedules_data.append({
                "stylist_id": stylist["id"],
                "stylist_name": stylist["name"],
                "busy_periods": busy_periods,
            })
        return schedules_data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取排班信息失败: {exc}")


@router.get("/", response_model=List[StylistResponse], summary="获取所有发型师")
async def get_all_stylists():
    try:
        return [StylistResponse(**item) for item in _service().get_all_stylists()]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"获取发型师信息失败: {exc}")


@router.get("/{stylist_id}/schedule", response_model=List[ScheduleResponse], summary="获取发型师排班")
async def get_stylist_schedule(stylist_id: int):
    try:
        from config.time_config import time_config

        stylist_service = _service()
        stylist = stylist_service.get_stylist_by_id(stylist_id)
        if not stylist:
            raise HTTPException(status_code=404, detail="发型师不存在")
        schedules = stylist_service.get_stylist_schedules(stylist_id, time_config.today())
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
