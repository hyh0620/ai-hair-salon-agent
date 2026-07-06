from typing import Any, Dict, List
import logging

from db.db_router import DatabaseRouter

logger = logging.getLogger(__name__)


class StylistService:
    """Manage stylist seed data and read operations."""

    def __init__(self, db_path: str = None):
        self.db = DatabaseRouter(db_path)
        self.default_stylists = [
            {
                "name": "林浩",
                "gender": "男",
                "specialties": "男士短发、渐变推剪、商务短发、油头造型，线条干净利落"
            },
            {
                "name": "陈宇",
                "gender": "男",
                "specialties": "男士短发、寸头、渐变推剪、纹理烫，适合清爽通勤风格"
            },
            {
                "name": "许然",
                "gender": "女",
                "specialties": "女士层次剪、长发修剪、脸型设计、发尾修整，风格自然柔和"
            },
            {
                "name": "周晴",
                "gender": "女",
                "specialties": "染发调色、冷棕色、挑染、补染、发色设计，擅长显白发色"
            },
            {
                "name": "赵一鸣",
                "gender": "男",
                "specialties": "烫发造型、纹理烫、蓬松处理、男士卷发，适合发量偏少客户"
            },
            {
                "name": "孙悦",
                "gender": "女",
                "specialties": "女士烫发、卷发设计、法式刘海、活动造型，注重整体气质"
            },
            {
                "name": "吴彤",
                "gender": "女",
                "specialties": "头皮护理、洗护护理、控油清洁、敏感头皮护理，适合头皮敏感客户"
            },
            {
                "name": "郑凯",
                "gender": "男",
                "specialties": "洗剪吹、基础剪裁、快速造型、学生短发，速度稳定"
            },
            {
                "name": "何岚",
                "gender": "女",
                "specialties": "活动造型、盘发、吹风定型、约会造型，适合重要场合"
            },
            {
                "name": "马骏",
                "gender": "男",
                "specialties": "儿童剪发、家庭剪发、基础修剪、亲和沟通，适合亲子预约"
            },
        ]

    def initialize_default_stylists(self) -> bool:
        try:
            existing_stylists = self.db.stylists.get_all_stylists()
            if existing_stylists:
                logger.info("数据库中已有 %s 位发型师，跳过初始化", len(existing_stylists))
                return True

            logger.info("数据库中无发型师数据，开始初始化默认发型师")
            for item in self.default_stylists:
                self.db.stylists.add_stylist(
                    name=item["name"],
                    gender=item["gender"],
                    specialties=item["specialties"],
                )

            final_count = len(self.db.stylists.get_all_stylists())
            logger.info("发型师初始化完成，共添加 %s 位发型师", final_count)
            return True
        except Exception as exc:
            logger.error("发型师初始化失败: %s", exc)
            return False

    def get_all_stylists(self) -> List[Dict[str, Any]]:
        return self.db.stylists.get_all_stylists()

    def get_stylist_by_name(self, name: str) -> Dict[str, Any]:
        return self.db.stylists.get_stylist_by_name(name)

    def get_stylist_by_id(self, stylist_id: int) -> Dict[str, Any]:
        return self.db.stylists.get_stylist_by_id(stylist_id)

    def get_stylist_schedules(self, stylist_id: int, date) -> List[Dict[str, Any]]:
        return self.db.stylists.get_stylist_schedules(stylist_id, date)

    def is_stylist_available(self, stylist_id: int, start_time, end_time) -> bool:
        return self.db.stylists.is_stylist_available(stylist_id, start_time, end_time)

    def add_stylist(self, name: str, gender: str = None, specialties: str = None) -> int:
        return self.db.stylists.add_stylist(name, gender, specialties)

    def get_stylists_count(self) -> int:
        return len(self.db.stylists.get_all_stylists())
