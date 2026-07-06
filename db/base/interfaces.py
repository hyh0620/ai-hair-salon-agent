from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import datetime


class BaseStylistRepository(ABC):
    """
    发型师数据访问抽象接口
    
    定义发型师相关的所有数据操作方法
    """
    
    @abstractmethod
    def add_stylist(self, name: str, gender: Optional[str] = None, specialties: Optional[str] = None) -> int:
        """添加发型师"""
        pass

    @abstractmethod
    def get_stylist_by_id(self, stylist_id: int) -> Optional[Dict[str, Any]]:
        """根据ID获取发型师信息"""
        pass

    @abstractmethod
    def get_stylist_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """根据姓名获取发型师信息"""
        pass

    @abstractmethod
    def get_all_stylists(self) -> List[Dict[str, Any]]:
        """获取所有发型师"""
        pass

    @abstractmethod
    def get_all_specialties(self) -> List[str]:
        """获取所有发型师的专长"""
        pass

    @abstractmethod
    def update_stylist(self, stylist_id: int, **updates) -> bool:
        """更新发型师信息"""
        pass

    @abstractmethod
    def delete_stylist(self, stylist_id: int) -> bool:
        """删除发型师"""
        pass

    @abstractmethod
    def get_stylists_by_gender(self, gender: str) -> List[Dict[str, Any]]:
        """根据性别获取发型师"""
        pass


class BaseScheduleRepository(ABC):
    """
    排班数据访问抽象接口
    
    定义排班相关的所有数据操作方法
    """
    
    @abstractmethod
    def add_schedule(self, stylist_id: int, start_time: datetime, end_time: datetime, 
                    status: str, appointment_id: Optional[int] = None) -> int:
        """添加排班"""
        pass

    @abstractmethod
    def get_stylist_schedules(self, stylist_id: int, date: datetime) -> List[Dict[str, Any]]:
        """获取发型师指定日期的排班"""
        pass

    @abstractmethod
    def is_stylist_available(self, stylist_id: int, start_time: datetime, end_time: datetime) -> bool:
        """检查发型师时间段是否可用"""
        pass

    @abstractmethod
    def update_schedule_status(self, schedule_id: int, status: str, appointment_id: Optional[int] = None) -> bool:
        """更新排班状态"""
        pass

    @abstractmethod
    def delete_schedule(self, schedule_id: int) -> bool:
        """删除排班"""
        pass


class BaseUserBehaviorRepository(ABC):
    """
    用户行为数据访问抽象接口
    
    定义用户行为分析相关的所有数据操作方法
    """
    
    @abstractmethod
    def record_behavior(self, user_id: str, action_type: str, action_data: Optional[Dict[str, Any]] = None, 
                       stylist_id: Optional[int] = None, session_id: Optional[str] = None) -> int:
        """记录用户行为"""
        pass

    @abstractmethod
    def get_user_behaviors(self, user_id: str, action_type: Optional[str] = None, 
                          days_back: Optional[int] = None) -> List[Dict[str, Any]]:
        """获取用户行为历史"""
        pass

    @abstractmethod
    def update_user_preference(self, user_id: str, preference_type: str, preference_value: str) -> bool:
        """更新用户偏好"""
        pass

    @abstractmethod
    def get_user_preferences(self, user_id: str, preference_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取用户偏好"""
        pass

    @abstractmethod
    def create_recommendation(self, user_id: str, recommendation_type: str, content: str, 
                            stylist_id: Optional[int] = None) -> int:
        """创建推荐"""
        pass

    @abstractmethod
    def get_pending_recommendations(self, user_id: str) -> List[Dict[str, Any]]:
        """获取待发送的推荐"""
        pass

    @abstractmethod
    def mark_recommendation_sent(self, recommendation_id: int) -> bool:
        """标记推荐为已发送"""
        pass

    @abstractmethod
    def get_user_statistics(self, user_id: str, days_back: int = 30) -> Dict[str, Any]:
        """获取用户统计信息"""
        pass
