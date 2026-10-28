from pipeline.settings import CONFIG


def test_config_has_required_settings():
    assert CONFIG["jsearch"]["queries"]
    assert CONFIG["models"]["scoring"] in CONFIG["pricing"]
    assert 0 <= CONFIG["scoring"]["min_fit_score"] <= 100
    assert CONFIG["models"]["scoring"] and CONFIG["models"]["tailoring"]
    assert CONFIG["limits"]["monthly_budget_usd"] > 0


def test_set_config_value_keeps_comments_and_sections():
    import yaml
    from pipeline.settings import ROOT, set_config_value
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    new = set_config_value(set_config_value(text, "  min_fit_score", 65), "  scoring", "claude-haiku-9")
    data = yaml.safe_load(new)
    assert data["scoring"]["min_fit_score"] == 65            # the section header "scoring:" is untouched
    assert data["models"]["scoring"] == "claude-haiku-9"
    assert "# 0-100" in new                                   # comments kept
    assert new.count("\n") == text.count("\n")
