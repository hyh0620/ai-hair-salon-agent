from services.service_catalog import normalize_service, parse_budget, specialties_for


def test_color_and_perm_catalog_rules_are_deterministic():
    color = normalize_service("想染一个显白发色")
    perm = normalize_service("预约纹理烫")

    assert color is not None
    assert color.name == "染发"
    assert color.standard_duration == 150
    assert color.standard_price == 398

    assert perm is not None
    assert perm.name == "烫发"
    assert perm.standard_duration == 180
    assert perm.standard_price == 468


def test_catalog_exposes_specialty_terms_for_recommendation():
    terms = specialties_for("男士短发", ["渐变推剪", "清爽"])

    assert "男士短发" in terms
    assert "渐变推剪" in terms
    assert "清爽" in terms


def test_budget_parser_keeps_optional_budget_as_preference():
    assert parse_budget("300元以内") == 300
    assert parse_budget(None) is None
