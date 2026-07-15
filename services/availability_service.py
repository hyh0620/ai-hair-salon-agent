"""Deterministic stylist availability search over persisted salon schedules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from config.time_config import time_config
from services.appointment_service import AppointmentService
from services.service_catalog import SERVICE_CATALOG, normalize_specialty, structured_stylist_profile


SLOT_INTERVAL_MINUTES = 30
MAX_STYLISTS = 3
MAX_SLOTS_PER_STYLIST = 2


@dataclass(frozen=True)
class AvailabilitySearchRequest:
    target_date: date
    range_start: time
    range_end: time
    service_key: str
    specialty: Optional[str] = None
    stylist_name: Optional[str] = None
    exact_time: Optional[time] = None


@dataclass(frozen=True)
class AvailabilityOption:
    option_id: int
    stylist_id: int
    stylist_name: str
    service_key: str
    service_name: str
    specialty_matches: List[str]
    start_time: datetime
    end_time: datetime
    duration_minutes: int
    price: int

    def to_session_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["start_time"] = self.start_time.isoformat()
        value["end_time"] = self.end_time.isoformat()
        return value


class AvailabilityService:
    """Compute stable candidate slots without LLM, MCP, weather, or database writes."""

    def __init__(self, appointment_service: Optional[AppointmentService] = None):
        self.appointment_service = appointment_service or AppointmentService()

    def search_available_stylists(
        self,
        request: AvailabilitySearchRequest,
        now: Optional[datetime] = None,
    ) -> List[AvailabilityOption]:
        service = SERVICE_CATALOG.get(request.service_key)
        if not service:
            return []

        now = now or time_config.now()
        canonical_specialty = normalize_specialty(request.specialty) if request.specialty else None
        if request.specialty and not canonical_specialty:
            return []
        profiles = self.matching_stylists(request)

        ranked: List[tuple[int, int, datetime, Dict[str, Any]]] = []
        for profile in profiles:
            specialty_score = 1 if canonical_specialty in profile.get("specialty_tags", []) else 0
            for start in self._candidate_starts(request, service.standard_duration):
                end = start + timedelta(minutes=service.standard_duration)
                if start <= now or not self.appointment_service.is_within_business_hours(start, end):
                    continue
                if self.appointment_service.is_stylist_available(int(profile["id"]), start, end):
                    ranked.append((-specialty_score, int(profile["id"]), start, profile))

        ranked.sort(key=lambda item: (item[0], item[2], item[1]))
        selected: List[tuple[datetime, Dict[str, Any]]] = []
        per_stylist: Dict[int, int] = {}
        selected_stylists: List[int] = []
        for _, stylist_id, start, profile in ranked:
            if stylist_id not in selected_stylists and len(selected_stylists) >= MAX_STYLISTS:
                continue
            if per_stylist.get(stylist_id, 0) >= MAX_SLOTS_PER_STYLIST:
                continue
            if stylist_id not in selected_stylists:
                selected_stylists.append(stylist_id)
            per_stylist[stylist_id] = per_stylist.get(stylist_id, 0) + 1
            selected.append((start, profile))

        options = []
        for option_id, (start, profile) in enumerate(selected, start=1):
            matches = [canonical_specialty] if canonical_specialty else []
            options.append(AvailabilityOption(
                option_id=option_id,
                stylist_id=int(profile["id"]),
                stylist_name=str(profile["name"]),
                service_key=service.key,
                service_name=service.name,
                specialty_matches=matches,
                start_time=start,
                end_time=start + timedelta(minutes=service.standard_duration),
                duration_minutes=service.standard_duration,
                price=service.standard_price,
            ))
        return options

    def matching_stylists(self, request: AvailabilitySearchRequest) -> List[Dict[str, Any]]:
        service = SERVICE_CATALOG.get(request.service_key)
        if not service:
            return []
        canonical_specialty = normalize_specialty(request.specialty) if request.specialty else None
        if request.specialty and not canonical_specialty:
            return []
        profiles = [structured_stylist_profile(item) for item in self.appointment_service.get_all_stylists()]
        if request.stylist_name:
            profiles = [item for item in profiles if item.get("name") == request.stylist_name]
        profiles = [item for item in profiles if service.key in item.get("supported_services", [])]
        if canonical_specialty:
            profiles = [item for item in profiles if canonical_specialty in item.get("specialty_tags", [])]
        return profiles

    @staticmethod
    def _candidate_starts(request: AvailabilitySearchRequest, duration_minutes: int) -> List[datetime]:
        tz = time_config.BEIJING_TZ
        if request.exact_time:
            return [datetime.combine(request.target_date, request.exact_time, tzinfo=tz)]

        start = datetime.combine(request.target_date, request.range_start, tzinfo=tz)
        latest_end = datetime.combine(request.target_date, request.range_end, tzinfo=tz)
        candidates = []
        while start + timedelta(minutes=duration_minutes) <= latest_end:
            candidates.append(start)
            start += timedelta(minutes=SLOT_INTERVAL_MINUTES)
        return candidates
