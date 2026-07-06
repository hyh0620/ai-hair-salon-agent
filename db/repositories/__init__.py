"""
Repositories Module

数据访问对象模块，包含：
- 发型师数据仓库
- 用户行为数据仓库
"""

from .stylist_repository import StylistRepository
from .user_behavior_repository import UserBehaviorRepository

__all__ = [
    'StylistRepository',
    'UserBehaviorRepository'
]
