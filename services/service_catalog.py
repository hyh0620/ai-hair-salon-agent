"""Deterministic hair salon service catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class HairService:
    key: str
    name: str
    standard_duration: int
    standard_price: int
    aliases: List[str]
    recommended_specialties: List[str]


SERVICE_CATALOG: Dict[str, HairService] = {
    "mens_short_cut": HairService(
        key="mens_short_cut",
        name="男士短发",
        standard_duration=45,
        standard_price=88,
        aliases=["男士短发", "男士剪发", "短发", "寸头", "渐变", "油头"],
        recommended_specialties=["男士短发", "渐变推剪", "油头", "寸头", "商务短发"],
    ),
    "womens_cut": HairService(
        key="womens_cut",
        name="女士剪发",
        standard_duration=60,
        standard_price=128,
        aliases=["女士剪发", "女发", "修发尾", "层次剪", "长发修剪"],
        recommended_specialties=["女士层次", "长发修剪", "脸型设计", "发尾修整"],
    ),
    "wash_cut_blow": HairService(
        key="wash_cut_blow",
        name="洗剪吹",
        standard_duration=60,
        standard_price=108,
        aliases=["洗剪吹", "洗头剪发", "剪吹", "洗护剪"],
        recommended_specialties=["基础剪裁", "洗护造型", "日常造型"],
    ),
    "color": HairService(
        key="color",
        name="染发",
        standard_duration=150,
        standard_price=398,
        aliases=["染发", "染色", "补染", "挑染", "发色", "漂染"],
        recommended_specialties=["染发调色", "冷棕色", "显白发色", "挑染", "发色设计", "补染"],
    ),
    "perm": HairService(
        key="perm",
        name="烫发",
        standard_duration=180,
        standard_price=468,
        aliases=["烫发", "卷发", "纹理烫", "蓬松烫", "造型烫"],
        recommended_specialties=["烫发造型", "纹理烫", "卷发设计", "蓬松处理"],
    ),
    "styling": HairService(
        key="styling",
        name="造型",
        standard_duration=40,
        standard_price=98,
        aliases=["造型", "吹造型", "约会造型", "活动造型", "盘发"],
        recommended_specialties=["快速造型", "活动造型", "吹风定型", "盘发"],
    ),
    "scalp_care": HairService(
        key="scalp_care",
        name="头皮护理",
        standard_duration=50,
        standard_price=168,
        aliases=["头皮护理", "头皮清洁", "护理", "洗护", "控油护理"],
        recommended_specialties=["头皮护理", "洗护护理", "控油清洁", "敏感头皮护理"],
    ),
}


SPECIALTY_ALIASES: Dict[str, List[str]] = {
    "冷棕色": ["冷棕色", "冷棕", "冷调棕色"],
    "显白发色": ["显白发色", "显白颜色", "显白染发"],
    "挑染": ["挑染", "线条染"],
    "染发调色": ["染发调色", "调色", "染发配色"],
    "渐变推剪": ["渐变推剪", "渐变", "fade"],
    "男士短发": ["男士短发", "男士剪发"],
    "女士层次": ["女士层次", "层次剪"],
    "纹理烫": ["纹理烫", "纹理卷"],
    "卷发设计": ["卷发设计", "卷发"],
    "活动造型": ["活动造型", "盘发", "约会造型"],
    "头皮护理": ["头皮护理", "头皮清洁"],
}


def all_services() -> List[HairService]:
    return list(SERVICE_CATALOG.values())


def normalize_service(value: Optional[str]) -> Optional[HairService]:
    if not value or value == "未知":
        return None

    query = value.strip().lower()
    for service in SERVICE_CATALOG.values():
        if query == service.key.lower() or query == service.name.lower():
            return service
        if any(alias.lower() in query or query in alias.lower() for alias in service.aliases):
            return service
    return None


def service_names() -> List[str]:
    return [service.name for service in SERVICE_CATALOG.values()]


def parse_budget(value: Optional[str]) -> Optional[int]:
    if not value or value == "未知":
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def parse_duration_minutes(value: Optional[str]) -> Optional[int]:
    if not value or value == "未知":
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    minutes = int(digits)
    return minutes if minutes > 0 else None


def specialties_for(service_value: Optional[str], extra_preferences: Iterable[str] = ()) -> List[str]:
    service = normalize_service(service_value)
    terms: List[str] = []
    if service:
        terms.extend(service.recommended_specialties)
        terms.extend(service.aliases)
        terms.append(service.name)
    for item in extra_preferences:
        if item and item != "未知" and item != "无":
            terms.append(str(item))
    return terms


def normalize_specialty(value: Optional[str]) -> Optional[str]:
    """Normalize a known stylist specialty without inventing unsupported tags."""
    if not value or value == "未知":
        return None
    query = str(value).strip().lower()
    for canonical, aliases in SPECIALTY_ALIASES.items():
        if any(alias.lower() in query or query in alias.lower() for alias in aliases):
            return canonical
    return None


def service_for_specialty(value: Optional[str]) -> Optional[HairService]:
    specialty = normalize_specialty(value)
    if not specialty:
        return None
    for service in SERVICE_CATALOG.values():
        if specialty in service.recommended_specialties:
            return service
    return None


def structured_stylist_profile(stylist: Dict[str, object]) -> Dict[str, object]:
    """Derive stable structured tags from persisted specialty text."""
    specialty_text = str(stylist.get("specialties") or "")
    specialty_tags = [
        canonical
        for canonical, aliases in SPECIALTY_ALIASES.items()
        if any(alias.lower() in specialty_text.lower() for alias in aliases)
    ]
    supported_services = []
    for service in SERVICE_CATALOG.values():
        terms = service.aliases + service.recommended_specialties + [service.name]
        if any(term.lower() in specialty_text.lower() for term in terms):
            supported_services.append(service.key)

    return {
        **stylist,
        "specialty_tags": specialty_tags,
        "supported_services": supported_services,
    }
