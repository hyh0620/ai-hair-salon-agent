"""
Repositories Module

数据访问对象模块，包含：
- 预约数据仓库
- 发型师数据仓库
- 用户行为数据仓库
"""

from .appointment_repository import AppointmentRepository
from .stylist_repository import StylistRepository
from .user_behavior_repository import UserBehaviorRepository
from .user_repository import UserRepository

__all__ = [
    'AppointmentRepository',
    'StylistRepository',
    'UserBehaviorRepository',
    'UserRepository',
]
