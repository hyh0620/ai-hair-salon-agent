"""
偏好管理器 - 专门负责管理用户的偏好数据

职责：
1. 从预约数据中提取和更新用户偏好
2. 管理发型师偏好、时间偏好、服务偏好等
3. 提供偏好数据的查询和统计
4. 处理偏好的变化和趋势分析
"""

from typing import Dict, Any, Optional
from datetime import datetime
import logging


class PreferenceManager:
    """偏好管理器 - 负责用户偏好的管理和分析"""
    
    def __init__(self, behavior_service = None):
        """
        初始化偏好管理器
        
        Args:
            behavior_service: 用户行为服务实例
        """
        self.behavior_service = behavior_service
        self.logger = logging.getLogger(__name__)
    
    @property
    def behavior_db(self):
        """用户行为仓库入口"""
        if hasattr(self, 'behavior_service') and self.behavior_service:
            return self.behavior_service.user_behavior_repo
        else:
            return None
    
    def update_preferences_from_appointment(self, action_data: Dict[str, Any], stylist_id: int = None):
        """
        从预约数据中更新用户偏好
        
        Args:
            action_data: 预约行为数据
            stylist_id: 发型师ID
        """
        try:
            # 发型师偏好
            if stylist_id:
                self.update_stylist_preference(stylist_id)
            
            # 时间偏好
            if action_data.get('start_time'):
                self.update_time_preference(action_data['start_time'])
            
            # 服务时长偏好
            if action_data.get('duration'):
                self.update_duration_preference(action_data['duration'])
            
            # 服务项目偏好
            if action_data.get('project'):
                self.update_service_preference(action_data['project'])
            
            # 发型师偏好类型（风格、专长等）
            if action_data.get('preference'):
                self.update_stylist_type_preference(action_data['preference'])
                
        except Exception as e:
            self.logger.error(f"更新用户偏好失败: {str(e)}")

    def _update_preference(self, preference_type: str, preference_value: Optional[str]) -> bool:
        if self.behavior_service:
            return self.behavior_service.update_user_preference(
                "default_user",
                preference_type,
                preference_value,
            )
        if self.behavior_db:
            return self.behavior_db.update_user_preference(
                "default_user",
                preference_type,
                preference_value,
            )
        self.logger.error("用户行为服务未初始化")
        return False
    
    def update_stylist_preference(self, stylist_id: int):
        """
        更新发型师偏好
        
        Args:
            stylist_id: 发型师ID
        """
        try:
            self._update_preference('stylist', str(stylist_id))
            self.logger.info(f"更新发型师偏好: {stylist_id}")
        except Exception as e:
            self.logger.error(f"更新发型师偏好失败: {str(e)}")
    
    def update_time_preference(self, start_time: str):
        """
        更新时间偏好
        
        Args:
            start_time: 开始时间字符串
        """
        try:
            start_datetime = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            hour = start_datetime.hour
            
            if 6 <= hour < 12:
                time_period = '上午'
            elif 12 <= hour < 18:
                time_period = '下午'
            else:
                time_period = '晚上'
            
            self._update_preference('time_period', time_period)
            self.logger.info(f"更新时间偏好: {time_period}")
        except Exception as e:
            self.logger.error(f"更新时间偏好失败: {str(e)}")
    
    def update_duration_preference(self, duration: Any):
        """
        更新服务时长偏好
        
        Args:
            duration: 服务时长
        """
        try:
            duration_str = str(duration)
            self._update_preference('duration', duration_str)
            self.logger.info(f"更新时长偏好: {duration_str}")
        except Exception as e:
            self.logger.error(f"更新时长偏好失败: {str(e)}")
    
    def update_service_preference(self, service: str):
        """
        更新服务项目偏好
        
        Args:
            service: 服务项目
        """
        try:
            self._update_preference('service', service)
            self.logger.info(f"更新服务偏好: {service}")
        except Exception as e:
            self.logger.error(f"更新服务偏好失败: {str(e)}")
    
    def update_stylist_type_preference(self, stylist_type: str):
        """
        更新发型师类型偏好
        
        Args:
            stylist_type: 发型师类型偏好（如：渐变推剪、自然层次等）
        """
        try:
            self._update_preference('stylist_type', stylist_type)
            self.logger.info(f"更新发型师类型偏好: {stylist_type}")
        except Exception as e:
            self.logger.error(f"更新发型师类型偏好失败: {str(e)}")
    
    def get_user_preferences(self) -> Dict[str, Any]:
        """
        获取用户所有偏好
        
        Returns:
            Dict: 用户偏好数据
        """
        try:
            if self.behavior_service:
                preferences = self.behavior_service.get_user_preferences("default_user")
            elif self.behavior_db:
                preferences = self.behavior_db.get_user_preferences("default_user")
            else:
                return {}

            if isinstance(preferences, list):
                return {
                    item.get("preference_type"): item.get("preference_value")
                    for item in preferences
                    if item.get("preference_type")
                }
            return preferences or {}
        except Exception as e:
            self.logger.error(f"获取用户偏好失败: {str(e)}")
            return {}
    
    def get_preferred_stylist_id(self) -> Optional[int]:
        """
        获取偏好的发型师ID
        
        Returns:
            int: 发型师ID，如果没有偏好则返回None
        """
        try:
            preferences = self.get_user_preferences()
            stylist_id = preferences.get('stylist')
            return int(stylist_id) if stylist_id else None
        except Exception as e:
            self.logger.error(f"获取偏好发型师ID失败: {str(e)}")
            return None
    
    def get_preferred_time_period(self) -> Optional[str]:
        """
        获取偏好的时间段
        
        Returns:
            str: 时间段偏好
        """
        try:
            preferences = self.get_user_preferences()
            return preferences.get('time_period')
        except Exception as e:
            self.logger.error(f"获取偏好时间段失败: {str(e)}")
            return None
    
    def get_preferred_service(self) -> Optional[str]:
        """
        获取偏好的服务项目
        
        Returns:
            str: 服务项目偏好
        """
        try:
            preferences = self.get_user_preferences()
            return preferences.get('service')
        except Exception as e:
            self.logger.error(f"获取偏好服务失败: {str(e)}")
            return None
    
    def get_preference_summary(self) -> Dict[str, Any]:
        """
        获取偏好摘要信息
        
        Returns:
            Dict: 偏好摘要
        """
        try:
            preferences = self.get_user_preferences()
            
            summary = {
                'has_stylist_preference': bool(preferences.get('stylist')),
                'has_time_preference': bool(preferences.get('time_period')),
                'has_service_preference': bool(preferences.get('service')),
                'has_duration_preference': bool(preferences.get('duration')),
                'preference_count': len([v for v in preferences.values() if v])
            }
            
            # 添加具体偏好内容
            if preferences.get('stylist'):
                summary['preferred_stylist_id'] = int(preferences['stylist'])
            if preferences.get('time_period'):
                summary['preferred_time'] = preferences['time_period']
            if preferences.get('service'):
                summary['preferred_service'] = preferences['service']
            if preferences.get('duration'):
                summary['preferred_duration'] = preferences['duration']
            
            return summary
            
        except Exception as e:
            self.logger.error(f"获取偏好摘要失败: {str(e)}")
            return {}
    
    def clear_preference(self, preference_type: str):
        """
        清除特定类型的偏好
        
        Args:
            preference_type: 偏好类型
        """
        try:
            self._update_preference(preference_type, None)
            self.logger.info(f"清除偏好: {preference_type}")
        except Exception as e:
            self.logger.error(f"清除偏好失败: {str(e)}")
    
    def clear_all_preferences(self):
        """清除所有偏好"""
        try:
            preference_types = ['stylist', 'time_period', 'duration', 'service', 'stylist_type']
            for pref_type in preference_types:
                self.clear_preference(pref_type)
            self.logger.info("清除所有用户偏好")
        except Exception as e:
            self.logger.error(f"清除所有偏好失败: {str(e)}")
