"""Build appointment response messages."""

from typing import Any, Dict, List
from datetime import datetime


class MessageBuilder:
    """Response message builder."""

    def __init__(self):
        self.missing_info_prompts = {
            "start_time": "请问您想预约哪一天、几点？",
            "duration": "请问预计需要多长时间？如果不确定，可以告诉我服务项目，我会按门店标准时长安排。",
            "project": "请问您需要什么服务项目？例如男士短发、女士剪发、洗剪吹、染发或烫发。",
            "preference": "您有发型风格或发型师专长偏好吗？",
        }

    def create_appointment_success_message(self, stylist: Dict[str, Any], appointment_history: Dict[str, Any] = None) -> str:
        appointment_history = appointment_history or {}
        project = appointment_history.get("project", "服务")
        duration = appointment_history.get("duration", "")
        price = appointment_history.get("price") or appointment_history.get("standard_price")
        appointment_id = appointment_history.get("appointment_id")
        start_time = self._format_start_time(appointment_history.get("start_time"))
        price_text = f"，{price}元" if price else ""

        if stylist.get("is_recommendation"):
            original = stylist.get("original_stylist", {})
            note = f"（原指定的{original.get('name', '')}时间冲突，已为您推荐相近专长发型师）"
        else:
            note = ""

        return (
            f"\n机器人：预约成功！预约编号：{appointment_id}。"
            f"已为您预约{stylist['name']}，{start_time}，{project}，{duration}{price_text}。{note}\n"
        )

    @staticmethod
    def _format_start_time(value: Any) -> str:
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                return str(value or "预约时间待确认")
        return f"{parsed.year}年{parsed.month}月{parsed.day}日{parsed:%H:%M}"

    def create_stylist_recommendation_message(
        self,
        original_stylist: Dict[str, Any],
        recommended_stylist: Dict[str, Any],
        appointment_history: Dict[str, Any],
        llm=None,
    ) -> str:
        project = appointment_history.get("project", "本次服务")
        start_time = appointment_history.get("start_time", "")

        if llm:
            try:
                prompt = f"""
用户想预约{original_stylist['name']}做{project}，但该发型师在{start_time}不空闲。
可推荐发型师：
- 姓名：{recommended_stylist['name']}
- 专长：{recommended_stylist.get('specialties', '')}
原发型师专长：{original_stylist.get('specialties', '')}

请生成一段80字以内的中文推荐话术，说明原发型师时间冲突、推荐人选适合该项目，并询问是否确认预约。
"""
                response = llm.invoke(prompt)
                if hasattr(response, "content") and response.content.strip():
                    return f"\n机器人：{response.content.strip()}\n"
            except Exception as exc:
                print(f"LLM生成推荐消息失败: {exc}")

        return (
            f"\n机器人：抱歉，{original_stylist['name']}在{start_time}这个时间段已有预约。"
            f"{recommended_stylist['name']}也很适合{project}，这个时间段可以安排，"
            f"请问是否为您预约{recommended_stylist['name']}？\n"
        )

    def create_recommendation_declined_message(self, llm=None) -> str:
        if llm:
            try:
                prompt = "用户拒绝了推荐发型师。请用60字以内中文回复，表达理解，并建议换时间或重新选择。"
                response = llm.invoke(prompt)
                if hasattr(response, "content") and response.content.strip():
                    return f"\n机器人：{response.content.strip()}\n"
            except Exception as exc:
                print(f"LLM生成拒绝消息失败: {exc}")
        return "\n机器人：好的，我理解。您可以换一个时间，或告诉我偏好的风格，我再为您重新推荐发型师。\n"

    def create_appointment_failure_message(self, stylist_name: str) -> str:
        if stylist_name and stylist_name != "未知":
            from services.appointment_service import AppointmentService

            appointment_service = AppointmentService()
            stylist = appointment_service.get_stylist_by_name(stylist_name)
            if stylist:
                return f"\n机器人：抱歉，{stylist_name}在您选择的时间段不空闲。请选择其他时间，或让我为您推荐其他发型师。\n"
            return f"\n机器人：抱歉，没有找到名为'{stylist_name}'的发型师。请确认姓名，或让我按服务项目推荐。\n"
        return "\n机器人：抱歉，该时间段没有合适的发型师空闲，请选择其他时间或调整偏好。\n"

    def create_missing_info_questions(self, missing_info: List[str]) -> str:
        questions = [self.missing_info_prompts.get(field, f"请补充{field}信息") for field in missing_info]
        return "\n" + " ".join(questions) + "\n"

    def create_unrelated_message(self) -> str:
        return "[REPLY][预约机器人]抱歉，我只能处理理发店服务咨询和预约。请问您需要预约剪发、染发、烫发或其他服务吗？\n"

    def create_parse_error_message(self) -> str:
        return "[REPLY][预约机器人]\n机器人：解析失败，请换一种说法再试。\n"

    def create_save_failure_message(self) -> str:
        return "\n机器人：抱歉，预约保存失败，可能是时间冲突或不在营业时间内，请更换时间后重试。\n"

    def create_availability_options_message(
        self,
        options: List[Dict[str, Any]],
        appointment_history: Dict[str, Any],
    ) -> str:
        target_date = appointment_history.get("availability_date", "")
        period = appointment_history.get("availability_period_label") or appointment_history.get("availability_time_text", "")
        service = appointment_history.get("project", "服务")
        specialty = appointment_history.get("specialty")
        duration = appointment_history.get("duration", "")
        price = appointment_history.get("price")
        preference_line = f"\n偏好：{specialty}" if specialty else ""
        lines = [
            "\n机器人：已识别您的需求：",
            f"日期：{target_date}",
            f"时间：{period}",
            f"服务：{service}{preference_line}",
            "\n查询到以下真实可预约选项：",
        ]
        for option in options:
            match = "、".join(option.get("specialty_matches") or []) or "服务匹配"
            start = datetime.fromisoformat(option["start_time"])
            lines.append(
                f"{option['option_id']}. {option['stylist_name']}：{start:%H:%M}"
                f"（专长：{match}；{option['duration_minutes']}分钟；{option['price']}元）"
            )
        lines.extend([
            f"\n{service}标准时长{duration}，价格{price}元。",
            "您可以回复“第一个”“选1”或“发型师姓名+时间”。",
            "",
        ])
        return "\n".join(lines)

    @staticmethod
    def create_availability_confirmation_message(option: Dict[str, Any]) -> str:
        start = datetime.fromisoformat(option["start_time"])
        return (
            f"\n机器人：确认为您预约{option['stylist_name']}，"
            f"{start.year}年{start.month}月{start.day}日{start:%H:%M}，"
            f"{option['service_name']}，{option['duration_minutes']}分钟，{option['price']}元吗？"
            "请回复“确认”或“取消”。\n"
        )

    @staticmethod
    def create_ambiguous_option_message(options: List[Dict[str, Any]]) -> str:
        labels = [
            f"{datetime.fromisoformat(item['start_time']):%H:%M}（选{item['option_id']}）"
            for item in options
        ]
        return f"\n机器人：该发型师有多个可预约时间：{'、'.join(labels)}。请回复具体时间或序号。\n"
