import json

from agents.appointment.appointment_database import AppointmentDatabase
from agents.appointment.appointment_processor import AppointmentProcessor
from agents.appointment.input_parser import InputParser
from agents.appointment.message_builder import MessageBuilder
from agents.appointment.stylist_finder import StylistFinder
from services.appointment_service import AppointmentService
from services.stylist_service import StylistService


def test_mocked_llm_json_boundary_marks_gender_as_optional(tmp_path):
    db_path = f"sqlite:///{tmp_path / 'salon.db'}"
    StylistService(db_path).initialize_default_stylists()
    appointment_service = AppointmentService(db_path)
    parser = InputParser.__new__(InputParser)
    processor = AppointmentProcessor(
        input_parser=parser,
        stylist_finder=StylistFinder(appointment_service),
        message_builder=MessageBuilder(),
        appointment_database=AppointmentDatabase(appointment_service=appointment_service),
        llm=None,
    )
    mocked_llm_output = json.dumps({
        "gender": "未知",
        "start_time": "2026-07-06 14:00",
        "duration": "45分钟",
        "project": "男士短发",
        "preference": "渐变推剪",
        "style_preference": "清爽",
        "budget": "100元",
        "stylist_name": "未知",
        "confirmation": "未知",
        "info_complete": True,
        "unrelated": False,
        "missing_info": [],
    }, ensure_ascii=False)

    data = parser.parse_data(mocked_llm_output)
    history = {
        "gender": None,
        "start_time": None,
        "duration": None,
        "project": None,
        "preference": None,
        "style_preference": None,
        "budget": None,
        "stylist_name": None,
    }

    finished = processor.update_history_from_data(history, data)

    assert finished is True
    assert history["project"] == "男士短发"
    assert history["gender"] is None
    assert history["price"] == 88
