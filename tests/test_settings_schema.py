"""The Settings page schema must cover every Settings field, exactly once."""

from __future__ import annotations

from dataclasses import fields

from whisperlocal import config as cfg
from whisperlocal.web import settings_schema

TYPES = {"bool", "int", "number", "text", "select", "radio", "multiselect", "triggers", "secret", "color"}
TIERS = {"live", "listeners", "restart"}


def _all_fields() -> list[dict]:
    return [f for group in settings_schema.SCHEMA for f in group["fields"]]


def test_every_settings_field_has_exactly_one_schema_entry():
    names = [f["name"] for f in _all_fields() if not f["virtual"]]
    expected = [f.name for f in fields(cfg.Settings)]
    missing = sorted(set(expected) - set(names))
    assert not missing, f"Settings fields without a UI: {missing}"
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"listed more than once: {duplicates}"


def test_every_non_virtual_field_is_a_real_setting():
    real = {f.name for f in fields(cfg.Settings)}
    bogus = sorted(f["name"] for f in _all_fields() if not f["virtual"] and f["name"] not in real)
    assert not bogus, f"schema fields that are not Settings attributes: {bogus}"


def test_virtual_fields_are_only_the_api_key():
    virtual = [f["name"] for f in _all_fields() if f["virtual"]]
    assert virtual == ["api_key"]
    assert settings_schema.FIELDS["api_key"]["type"] == "secret"
    assert "api_key" not in settings_schema.settings_field_names()


def test_field_shapes():
    for f in _all_fields():
        assert f["type"] in TYPES, f
        assert f["tier"] in TIERS, f
        assert f["label"] and f["help"], f
        if f["type"] in ("select", "radio", "multiselect"):
            assert f["options"], f
            for opt in f["options"]:
                assert set(opt) == {"value", "label"}, opt


def test_types_match_the_settings_defaults():
    defaults = cfg.Settings()
    for f in _all_fields():
        if f["virtual"]:
            continue
        value = getattr(defaults, f["name"])
        kind = f["type"]
        if isinstance(value, bool):
            assert kind == "bool", f["name"]
        elif isinstance(value, int):
            assert kind == "int", f["name"]
        elif isinstance(value, float):
            assert kind == "number", f["name"]
        elif isinstance(value, tuple):
            assert kind in ("triggers", "multiselect", "color"), f["name"]
        else:
            assert kind in ("text", "select", "radio"), f["name"]


def test_select_options_agree_with_config():
    F = settings_schema.FIELDS
    assert [o["value"] for o in F["model"]["options"]] == list(cfg.MODEL_ALIASES) + ["custom"]
    assert [o["value"] for o in F["meeting_prompt"]["options"]] == list(cfg.MEETING_PROMPTS)
    assert [o["value"] for o in F["meeting_apps"]["options"]] == list(cfg.MEETING_APP_KEYS)
    assert [o["value"] for o in F["dictation_backend"]["options"]] == list(cfg.BACKENDS)
    assert [o["value"] for o in F["meeting_backend"]["options"]] == list(cfg.BACKENDS)
    assert [o["value"] for o in F["overlay_anchor"]["options"]] == ["caret", "mouse", "bottom"]
    assert [o["value"] for o in F["paste_mode"]["options"]] == ["paste", "clipboard"]
    langs = [o["value"] for o in F["language"]["options"]]
    assert langs[0] == "auto" and "en" in langs and langs[-1] == "custom"


def test_group_order():
    keys = [g["key"] for g in settings_schema.SCHEMA]
    assert keys == [
        "trigger", "transcription", "meetings", "behaviour", "memory",
        "hallucination", "history", "menubar", "dashboard", "advanced",
    ]
    assert settings_schema.SCHEMA[0]["fields"][0]["name"] == "trigger_keys"


def test_ranges_match_validate():
    F = settings_schema.FIELDS
    assert F["meeting_segment_seconds"]["min"] == 20 and F["meeting_segment_seconds"]["max"] == 300
    assert F["web_port"]["min"] == 1024 and F["web_port"]["max"] == 65535
    assert F["max_repeat_ratio"]["min"] == 0 and F["max_repeat_ratio"]["max"] == 1
    assert F["max_repeat_ratio"]["step"] == 0.05
    assert F["max_word_run"]["min"] == 2
    assert F["icon_point_size"]["min"] == 10 and F["icon_point_size"]["max"] == 22
