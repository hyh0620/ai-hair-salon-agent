"""Database operations for appointments inside caller-owned transactions."""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Appointment


class AppointmentRepository:
    """Write appointment rows without owning commit or rollback boundaries."""

    @staticmethod
    def add_appointment_in_session(
        session: Session,
        *,
        user_id: str,
        session_id: str,
        stylist_id: int,
        service_key: str,
        service_name: str,
        start_time: datetime,
        end_time: datetime,
        duration_minutes: int,
        price: int,
        notes: Optional[str] = None,
    ) -> int:
        appointment = Appointment(
            user_id=user_id,
            session_id=session_id,
            stylist_id=stylist_id,
            service_key=service_key,
            service_name=service_name,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            price=price,
            status="confirmed",
            notes=notes,
        )
        session.add(appointment)
        session.flush()
        return int(appointment.id)
