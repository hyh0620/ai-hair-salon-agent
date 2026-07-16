"""Database operations for appointments inside caller-owned transactions."""

from datetime import date, datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from ..models import Appointment, Stylist


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
        updated_at: Optional[datetime] = None,
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
            updated_at=updated_at,
            version=1,
        )
        session.add(appointment)
        session.flush()
        return int(appointment.id)

    @staticmethod
    def get_owned_appointment_in_session(
        session: Session,
        *,
        appointment_id: int,
        owner_id: str,
    ):
        """Return an appointment and stylist name using an owner-scoped lookup."""
        return (
            session.query(Appointment, Stylist.name)
            .join(Stylist, Stylist.id == Appointment.stylist_id)
            .filter(
                Appointment.id == appointment_id,
                Appointment.user_id == owner_id,
            )
            .first()
        )

    @staticmethod
    def appointment_owner_in_session(
        session: Session,
        *,
        appointment_id: int,
    ) -> Optional[str]:
        """Return only the owner marker for internal not-found diagnostics."""
        row = (
            session.query(Appointment.user_id)
            .filter(Appointment.id == appointment_id)
            .first()
        )
        return str(row[0]) if row else None

    @staticmethod
    def list_owned_appointments_in_session(
        session: Session,
        *,
        owner_id: str,
        statuses: Sequence[str] = ("confirmed",),
        starts_after: Optional[datetime] = None,
        target_date: Optional[date] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ):
        query = (
            session.query(Appointment, Stylist.name)
            .join(Stylist, Stylist.id == Appointment.stylist_id)
            .filter(Appointment.user_id == owner_id)
        )
        if statuses:
            query = query.filter(Appointment.status.in_(tuple(statuses)))
        if starts_after is not None:
            query = query.filter(Appointment.start_time > starts_after)
        if target_date is not None:
            day_start = datetime.combine(target_date, datetime.min.time())
            query = query.filter(
                Appointment.start_time >= day_start,
                Appointment.start_time < day_start + timedelta(days=1),
            )
        else:
            if date_from is not None:
                query = query.filter(
                    Appointment.start_time >= datetime.combine(date_from, datetime.min.time())
                )
            if date_to is not None:
                query = query.filter(
                    Appointment.start_time
                    < datetime.combine(date_to + timedelta(days=1), datetime.min.time())
                )
        return query.order_by(Appointment.start_time.asc(), Appointment.id.asc()).all()
