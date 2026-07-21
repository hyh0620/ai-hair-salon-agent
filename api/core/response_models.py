"""
简化的API响应模型

只保留第一版真正需要的核心功能
"""
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from typing import Annotated, Any, Dict, Literal, Optional, Union
from datetime import date, datetime, time
from config.time_config import time_config


class BaseResponse(BaseModel):
    """基础响应模型"""
    message: str
    timestamp: datetime = Field(default_factory=time_config.now)


class DataResponse(BaseResponse):
    """数据响应模型"""
    data: Any


# 预约相关模型
class AppointmentRequest(BaseModel):
    user_id: Optional[str] = None
    project: Optional[str] = None
    service: Optional[str] = None
    start_time: str
    duration: str
    stylist_id: Optional[int] = None
    stylist_name: Optional[str] = Field(default=None, validation_alias=AliasChoices("stylist_name", "stylist"))
    gender: Optional[str] = None
    budget: Optional[str] = None
    style_preference: Optional[str] = None
    preference: Optional[str] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def require_project_or_service(self):
        if not self.project and not self.service:
            raise ValueError("project 或 service 至少需要提供一个")
        return self


class AppointmentResponse(BaseModel):
    appointment_id: int
    user_id: str
    project: str
    start_time: str
    end_time: str
    duration: str
    price: int
    status: Literal["confirmed"] = "confirmed"
    stylist_id: int
    stylist_name: str
    notes: Optional[str] = None


class AvailabilityCandidateResponse(BaseModel):
    option_id: int
    stylist_id: int
    stylist_name: str
    service_key: str
    service_name: str
    specialty_matches: list[str]
    start_time: str
    end_time: str
    duration_minutes: int
    price: int


class AppointmentSelectionResponse(BaseModel):
    status: Literal["selection_required"] = "selection_required"
    requires_selection: Literal[True] = True
    requires_confirmation: Literal[True] = True
    project: str
    start_time: str
    duration: str
    price: int
    candidates: list[AvailabilityCandidateResponse]


AppointmentCreateData = Annotated[
    Union[AppointmentResponse, AppointmentSelectionResponse],
    Field(discriminator="status"),
]


class AppointmentCreateResponse(BaseResponse):
    data: AppointmentCreateData


class AppointmentLifecycleItem(BaseModel):
    appointment_id: int
    owner_id: str
    stylist_id: int
    stylist_name: str
    service_key: str
    service_name: str
    price: int
    duration_minutes: int
    start_time: datetime
    end_time: datetime
    status: Literal["confirmed", "cancelled", "completed"]
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: Optional[datetime] = None


class AppointmentListData(BaseModel):
    status: Literal["success"] = "success"
    appointments: list[AppointmentLifecycleItem]


class AppointmentListResponse(BaseResponse):
    data: AppointmentListData


class AppointmentDetailData(BaseModel):
    status: Literal["success"] = "success"
    appointment: AppointmentLifecycleItem


class AppointmentDetailResponse(BaseResponse):
    data: AppointmentDetailData


AppointmentLifecycleStatus = Literal[
    "success",
    "confirmation_required",
    "not_found",
    "already_cancelled",
    "not_modifiable",
    "conflict",
    "stale_state",
    "invalid_time",
    "outside_business_hours",
    "service_not_supported",
    "validation_error",
    "no_change",
    "persistence_error",
]


class AppointmentOperationData(BaseModel):
    status: AppointmentLifecycleStatus
    appointment: Optional[AppointmentLifecycleItem] = None
    current_version: Optional[int] = Field(default=None, ge=1)
    reason: Optional[str] = None


class AppointmentOperationResponse(BaseResponse):
    data: AppointmentOperationData


class AppointmentCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Optional[str] = Field(default=None, min_length=1)
    expected_version: int = Field(ge=1)


class AppointmentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Optional[str] = Field(default=None, min_length=1)
    expected_version: int = Field(ge=1)
    target_date: Optional[date] = None
    start_time: Optional[time] = None
    stylist_id: Optional[int] = Field(default=None, ge=1)
    stylist_name: Optional[str] = None
    project: Optional[str] = None
    service: Optional[str] = None

    @model_validator(mode="after")
    def validate_patch(self):
        if self.stylist_id is not None and self.stylist_name:
            raise ValueError("stylist_id 和 stylist_name 只能提供一个")
        if self.project and self.service:
            raise ValueError("project 和 service 只能提供一个")
        if not any((
            self.target_date,
            self.start_time,
            self.stylist_id,
            self.stylist_name,
            self.project,
            self.service,
        )):
            raise ValueError("至少需要提供一个要修改的字段")
        return self


# 咨询相关模型
class ConsultationRequest(BaseModel):
    user_id: str
    question: str
    category: Optional[str] = None


class ConsultationResponse(BaseModel):
    consultation_id: str
    question: str
    answer: str
    category: Optional[str] = None


# 用户行为相关模型
class UserBehaviorRequest(BaseModel):
    user_id: str
    action: str
    context: Optional[Dict[str, Any]] = None


class UserBehaviorResponse(BaseModel):
    user_id: str
    action: str
    timestamp: datetime
    context: Optional[Dict[str, Any]] = None


# 任务分类相关模型
class TaskClassificationRequest(BaseModel):
    text: str
    context: Optional[Dict[str, Any]] = None


class TaskClassificationResponse(BaseModel):
    text: str
    category: str
    confidence: float
    reasoning: Optional[str] = None
