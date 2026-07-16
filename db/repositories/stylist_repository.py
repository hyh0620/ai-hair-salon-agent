from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from ..base.interfaces import BaseStylistRepository, BaseScheduleRepository
from ..base.session_manager import SessionManager
from ..models import Stylist, StylistSchedule


class StylistRepository(BaseStylistRepository, BaseScheduleRepository):
    """
    发型师数据访问对象
    
    职责：
    1. 发型师信息的CRUD操作
    2. 发型师排班的管理
    3. 发型师可用性检查
    """
    
    def __init__(self, session_manager: SessionManager):
        """
        初始化发型师数据仓库
        
        Args:
            session_manager: 会话管理器
        """
        self.session_manager = session_manager

    def add_stylist(self, name: str, gender: Optional[str] = None, specialties: Optional[str] = None) -> int:
        """
        添加新发型师
        
        Args:
            name: 发型师姓名
            gender: 性别
            specialties: 专长
            
        Returns:
            新创建的发型师ID
        """
        with self.session_manager.session_scope() as session:
            stylist = Stylist(name=name, gender=gender, specialties=specialties)
            session.add(stylist)
            session.flush()
            return stylist.id

    def get_stylist_by_id(self, stylist_id: int) -> Optional[Dict[str, Any]]:
        """
        根据ID获取发型师信息
        
        Args:
            stylist_id: 发型师ID
            
        Returns:
            发型师信息字典，如果不存在返回None
        """
        with self.session_manager.session_scope() as session:
            return self.get_stylist_by_id_in_session(session, stylist_id)

    def get_stylist_by_id_in_session(
        self,
        session: Session,
        stylist_id: int,
    ) -> Optional[Dict[str, Any]]:
        stylist = session.query(Stylist).filter(Stylist.id == stylist_id).first()
        return self._stylist_to_dict(stylist) if stylist else None

    def get_stylist_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        根据姓名获取发型师信息
        
        Args:
            name: 发型师姓名
            
        Returns:
            发型师信息字典，如果不存在返回None
        """
        with self.session_manager.session_scope() as session:
            stylist = session.query(Stylist).filter(
                Stylist.name == name
            ).first()
            
            if not stylist:
                return None
                
            return self._stylist_to_dict(stylist)

    def get_all_stylists(self) -> List[Dict[str, Any]]:
        """
        获取所有发型师信息
        
        Returns:
            发型师信息列表
        """
        with self.session_manager.session_scope() as session:
            stylists = session.query(Stylist).all()
            return [self._stylist_to_dict(stylist) for stylist in stylists]

    def get_all_specialties(self) -> List[str]:
        """
        获取所有发型师的专长列表
        
        Returns:
            专长列表（去重后）
        """
        with self.session_manager.session_scope() as session:
            specialties = session.query(Stylist.specialties).distinct().all()
            return [s[0] for s in specialties if s[0] is not None]

    def update_stylist(self, stylist_id: int, **updates) -> bool:
        """
        更新发型师信息
        
        Args:
            stylist_id: 发型师ID
            **updates: 要更新的字段
            
        Returns:
            更新是否成功
        """
        with self.session_manager.session_scope() as session:
            stylist = session.query(Stylist).filter(
                Stylist.id == stylist_id
            ).first()
            
            if not stylist:
                return False
                
            for key, value in updates.items():
                if hasattr(stylist, key):
                    setattr(stylist, key, value)
                    
            return True

    def delete_stylist(self, stylist_id: int) -> bool:
        """
        删除发型师
        
        Args:
            stylist_id: 发型师ID
            
        Returns:
            删除是否成功
        """
        with self.session_manager.session_scope() as session:
            stylist = session.query(Stylist).filter(
                Stylist.id == stylist_id
            ).first()
            
            if not stylist:
                return False
                
            session.delete(stylist)
            return True

    # 排班相关方法
    def add_schedule(self, stylist_id: int, start_time: datetime, end_time: datetime, 
                    status: str, appointment_id: Optional[int] = None) -> int:
        """
        添加发型师排班
        
        Args:
            stylist_id: 发型师ID
            start_time: 开始时间
            end_time: 结束时间
            status: 状态 ('busy' 或 'free')
            appointment_id: 预约ID（如果是忙碌状态）
            
        Returns:
            新创建的排班ID
        """
        with self.session_manager.session_scope() as session:
            return self.add_schedule_in_session(
                session,
                stylist_id=stylist_id,
                start_time=start_time,
                end_time=end_time,
                status=status,
                appointment_id=appointment_id,
            )

    @staticmethod
    def add_schedule_in_session(
        session: Session,
        *,
        stylist_id: int,
        start_time: datetime,
        end_time: datetime,
        status: str,
        appointment_id: Optional[int] = None,
    ) -> int:
        schedule = StylistSchedule(
            stylist_id=stylist_id,
            start_time=start_time,
            end_time=end_time,
            status=status,
            appointment_id=appointment_id,
        )
        session.add(schedule)
        session.flush()
        return int(schedule.id)

    def get_stylist_schedules(self, stylist_id: int, date: datetime) -> List[Dict[str, Any]]:
        """
        获取发型师指定日期的排班
        
        Args:
            stylist_id: 发型师ID
            date: 查询日期
            
        Returns:
            排班信息列表
        """
        with self.session_manager.session_scope() as session:
            start = datetime(date.year, date.month, date.day)
            end = start + timedelta(days=1)
            
            schedules = session.query(StylistSchedule).filter(
                StylistSchedule.stylist_id == stylist_id,
                StylistSchedule.start_time >= start,
                StylistSchedule.end_time < end
            ).all()
            
            return [self._schedule_to_dict(schedule) for schedule in schedules]

    def is_stylist_available(self, stylist_id: int, start_time: datetime, end_time: datetime) -> bool:
        """
        检查发型师在指定时间段是否可用
        
        Args:
            stylist_id: 发型师ID
            start_time: 开始时间
            end_time: 结束时间
            
        Returns:
            是否可用
        """
        with self.session_manager.session_scope() as session:
            return not self.has_schedule_conflict_in_session(
                session,
                stylist_id=stylist_id,
                start_time=start_time,
                end_time=end_time,
            )

    @staticmethod
    def has_schedule_conflict_in_session(
        session: Session,
        *,
        stylist_id: int,
        start_time: datetime,
        end_time: datetime,
    ) -> bool:
        conflict = session.query(StylistSchedule).filter(
            StylistSchedule.stylist_id == stylist_id,
            StylistSchedule.status == "busy",
            StylistSchedule.start_time < end_time,
            StylistSchedule.end_time > start_time,
        ).first()
        return conflict is not None

    def update_schedule_status(self, schedule_id: int, status: str, appointment_id: Optional[int] = None) -> bool:
        """
        更新排班状态
        
        Args:
            schedule_id: 排班ID
            status: 新状态
            appointment_id: 预约ID
            
        Returns:
            更新是否成功
        """
        with self.session_manager.session_scope() as session:
            schedule = session.query(StylistSchedule).filter(
                StylistSchedule.id == schedule_id
            ).first()
            
            if not schedule:
                return False
                
            schedule.status = status
            if appointment_id is not None:
                schedule.appointment_id = appointment_id
                
            return True

    def delete_schedule(self, schedule_id: int) -> bool:
        """
        删除排班
        
        Args:
            schedule_id: 排班ID
            
        Returns:
            删除是否成功
        """
        with self.session_manager.session_scope() as session:
            schedule = session.query(StylistSchedule).filter(
                StylistSchedule.id == schedule_id
            ).first()
            
            if not schedule:
                return False
                
            session.delete(schedule)
            return True

    def get_stylists_by_gender(self, gender: str) -> List[Dict[str, Any]]:
        """
        根据性别获取发型师信息
        
        Args:
            gender: 发型师性别
            
        Returns:
            发型师信息列表
        """
        with self.session_manager.session_scope() as session:
            stylists = session.query(Stylist).filter(
                Stylist.gender == gender
            ).all()
            return [self._stylist_to_dict(stylist) for stylist in stylists]

    def _stylist_to_dict(self, stylist: Stylist) -> Dict[str, Any]:
        """将发型师对象转换为字典"""
        return {
            'id': stylist.id,
            'name': stylist.name,
            'gender': stylist.gender,
            'specialties': stylist.specialties
        }

    def _schedule_to_dict(self, schedule: StylistSchedule) -> Dict[str, Any]:
        """将排班对象转换为字典"""
        return {
            'id': schedule.id,
            'stylist_id': schedule.stylist_id,
            'start_time': schedule.start_time,
            'end_time': schedule.end_time,
            'status': schedule.status,
            'appointment_id': schedule.appointment_id
        }
