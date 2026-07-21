"""API router registration."""

from .appointment import router as appointment_router
from .auth import router as auth_router
from .consultation import router as consultation_router
from .health import router as health_router
from .knowledge import router as knowledge_router
from .stylist import router as stylist_router
from .task import router as task_router
from .user_behavior_analysis import router as user_behavior_analysis_router
from .user_behavior_analysis import router_underscore as user_behavior_analysis_underscore_router

api_routers = [
    health_router,
    auth_router,
    appointment_router,
    consultation_router,
    task_router,
    knowledge_router,
    stylist_router,
    user_behavior_analysis_router,
    user_behavior_analysis_underscore_router,
]
