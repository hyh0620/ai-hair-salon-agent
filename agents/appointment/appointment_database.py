"""
预约数据库操作器

负责处理预约相关的数据库操作
注意：现在通过Services层访问数据库，符合分层架构
"""

import logging
from typing import Dict, Any
from datetime import datetime
from config.time_config import time_config
from config.constants import busy_periods_dict
from services.appointment_service import AppointmentSaveResult


logger = logging.getLogger(__name__)


class AppointmentDatabase:
    """预约数据库操作器"""
    
    def __init__(self, appointment_service=None, user_behavior_service=None):
        self._appointment_service = appointment_service
        self._user_behavior_service = user_behavior_service
    
    @property
    def appointment_service(self):
        """懒加载预约服务"""
        if self._appointment_service is None:
            from services.appointment_service import AppointmentService
            self._appointment_service = AppointmentService()
        return self._appointment_service
    
    @property 
    def user_behavior_service(self):
        """懒加载用户行为服务"""
        if self._user_behavior_service is None:
            from services.user_behavior_service import UserBehaviorService
            self._user_behavior_service = UserBehaviorService()
        return self._user_behavior_service
    
    def save_appointment(self, stylist_id: str, start_time: datetime, 
                        end_time: datetime, appointment_history: Dict[str, Any], 
                        session_id: str) -> bool:
        """Compatibility wrapper that preserves the historical bool contract."""
        return self.save_appointment_detailed(
            stylist_id,
            start_time,
            end_time,
            appointment_history,
            session_id,
        ).success

    def save_appointment_detailed(
        self,
        stylist_id: str,
        start_time: datetime,
        end_time: datetime,
        appointment_history: Dict[str, Any],
        session_id: str,
    ) -> AppointmentSaveResult:
        """Persist an appointment and retain its database identifiers."""
        try:
            result = self.appointment_service.save_appointment_detailed(
                stylist_id, start_time, end_time, appointment_history, session_id
            )
            if result.success:
                # 记录用户行为
                self._record_user_behavior(start_time, end_time, stylist_id, 
                                         appointment_history, session_id)
            return result
            
        except Exception as e:
            logger.exception("保存预约信息到数据库失败")
            return AppointmentSaveResult(False, reason=type(e).__name__)
    
    def update_memory_schedule(self, stylist_id: str, start_time: datetime, end_time: datetime):
        """更新内存中的发型师忙碌时间段"""
        busy_period = {
            "start": time_config.format_datetime(start_time, "%H:%M"),
            "end": time_config.format_datetime(end_time, "%H:%M")
        }
        busy_periods_dict.setdefault(stylist_id, []).append(busy_period)
    
    def _record_user_behavior(self, start_time: datetime, end_time: datetime,
                            stylist_id: str, appointment_history: Dict[str, Any], 
                            session_id: str):
        """记录用户预约行为"""
        try:
            details = self.appointment_service.build_appointment_details(appointment_history)
            action_data = {
                'start_time': time_config.format_datetime(start_time, "%Y-%m-%d %H:%M:%S"),
                'end_time': time_config.format_datetime(end_time, "%Y-%m-%d %H:%M:%S"),
                'duration': int((end_time - start_time).total_seconds() / 60),
                'project': details.get('project', '剪发'),
                'service_key': details.get('service_key'),
                'price': details.get('price'),
                'preference': appointment_history.get('preference', ''),
                'style_preference': appointment_history.get('style_preference', ''),
                'budget': appointment_history.get('budget', ''),
                'stylist_id': stylist_id
            }

            # Session IDs provide request tracing here; they are not authenticated identities.
            tracking_user_id = str(appointment_history.get("user_id") or session_id)
            # 通过Services层记录用户行为
            self.user_behavior_service.record_behavior(
                user_id=tracking_user_id,
                action_type='appointment',
                action_data=action_data,
                stylist_id=str(stylist_id),
                session_id=session_id
            )
            
        except Exception as behavior_error:
            logger.warning("记录用户行为失败（预约已成功）: %s", type(behavior_error).__name__)
