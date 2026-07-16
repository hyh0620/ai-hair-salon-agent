from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import declarative_base, relationship
from config.time_config import utc_now_naive

Base = declarative_base()


class Stylist(Base):
    __tablename__ = 'stylists'

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)
    gender = Column(String, nullable=True)
    specialties = Column(String, nullable=True)
    schedules = relationship("StylistSchedule", back_populates="stylist", cascade="all, delete-orphan")


class Appointment(Base):
    """Persisted booking record created atomically with its busy schedule."""

    __tablename__ = 'appointments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False, default='default_user')
    session_id = Column(String, nullable=True)
    stylist_id = Column(Integer, ForeignKey('stylists.id'), nullable=False)
    service_key = Column(String, nullable=False)
    service_name = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default='confirmed')
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now_naive, nullable=False)
    updated_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)


class StylistSchedule(Base):
    __tablename__ = 'stylist_schedules'

    id = Column(Integer, primary_key=True)
    stylist_id = Column(Integer, ForeignKey('stylists.id'))
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(String, nullable=False)  # 'busy' or 'free'
    appointment_id = Column(Integer, nullable=True)
    stylist = relationship("Stylist", back_populates="schedules")


class UserBehavior(Base):
    __tablename__ = 'user_behaviors'

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, default='default_user')  # 单用户场景使用默认用户ID
    action_type = Column(String, nullable=False)  # 'appointment', 'consultation', 'inquiry'
    action_data = Column(JSON, nullable=True)  # 存储行为相关的详细数据
    stylist_id = Column(Integer, ForeignKey('stylists.id'), nullable=True)
    session_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utc_now_naive)
    stylist = relationship("Stylist")


class UserPreference(Base):
    __tablename__ = 'user_preferences'

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, default='default_user')
    preference_type = Column(String, nullable=False)  # 'stylist', 'time', 'service', 'duration'
    preference_value = Column(String, nullable=False)
    confidence_score = Column(Integer, default=1)  # 偏好的置信度（出现次数）
    last_updated = Column(DateTime, default=utc_now_naive, onupdate=utc_now_naive)


class UserRecommendation(Base):
    __tablename__ = 'user_recommendations'

    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, default='default_user')
    recommendation_type = Column(String, nullable=False)  # 'stylist_available', 'return_reminder', 'service_suggestion'
    content = Column(Text, nullable=False)
    stylist_id = Column(Integer, ForeignKey('stylists.id'), nullable=True)
    is_sent = Column(Integer, default=0)  # 是否已发送
    created_at = Column(DateTime, default=utc_now_naive)
    sent_at = Column(DateTime, nullable=True)
    stylist = relationship("Stylist")
