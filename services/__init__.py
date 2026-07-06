"""Service package.

Import concrete services from their modules to keep package import lightweight.
"""

from .service_catalog import HairService, SERVICE_CATALOG, normalize_service

__all__ = [
    "HairService",
    "SERVICE_CATALOG",
    "normalize_service",
]
