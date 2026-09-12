"""settings_manager applies, persists and classifies changes to Settings."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import fields

import pytest

from whisperlocal import config as cfg
from whisperlocal import settings_manager as sm
from whisperlocal.config import ConfigError, Settings
from whisperlocal.settings_manager import SettingsManager, Tier

FIELD_NAMES = [f.name for f in fields(Settings)]


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    for name in FIELD_NAMES:
        monkeypatch.delenv(cfg.ENV_PREFIX + name.upper(), raising=False)
    monkeypatch.delenv(cfg.ENV_PREFIX + "TRIGGER_KEY", raising=False)


# ─── Tiers ───────────────────────────────────────────────────────────────────────


def test_tier_of_covers_every_field_exactly():
    missing = set(FIELD_NAMES) - set(sm.TIER_OF)
    extra = set(sm.TIER_OF) - set(FIELD_NAMES)
    assert not missing, f"add a tier for: {sorted(missing)}"
    assert not extra, f"TIER_OF names fields Settings lacks: {sorted(extra)}"
    assert all(isinstance(t, Tier) for t in sm.TIER_OF.values())


def test_tier_assignments():
    assert sm.TIER_OF["trigger_keys"] is Tier.LISTENERS
    assert sm.TIER_OF["web_enabled"] is Tier.RESTART
    assert sm.TIER_OF["web_port"] is Tier.RESTART
    assert sm.TIER_OF["model"] is Tier.LIVE
    assert sm.TIER_OF["hold_threshold"] is Tier.LIVE
    assert Tier.LIVE == "live" and Tier.RESTART.value == "restart"


# ─── diff / classify ─────────────────────────────────────────────────────────────


def test_diff_in_field_order():
    old = Settings()
    new = dataclasses.replace(old, web_port=50000, trigger_keys=("f13",), model="small")
    assert sm.diff(old, new) == ["trigger_keys", "model", "web_port"]
    assert sm.diff(old, old) == []


def test_classify_changes_always_has_all_tiers():
    old = Settings()
    assert sm.classify_changes(old, old) == {
        Tier.LIVE: [], Tier.LISTENERS: [], Tier.RESTART: []
    }
    new = dataclasses.replace(old, web_port=50000, trigger_keys=("f13",), sounds=False)
    assert sm.classify_changes(old, new) == {
        Tier.LIVE: ["sounds"],
        Tier.LISTENERS: ["trigger_keys"],
        Tier.RESTART: ["web_port"],
    }


# ─── helpers ─────────────────────────────────────────────────────────────────────


def test_coerce_types_numeric_tuple_elements():
    assert sm.coerce("icon_color", [1, "0.5", 0]) == (1.0, 0.5, 0.0)
    assert sm.coerce("icon_color", "0.1, 0.2, 0.3") == (0.1, 0.2, 0.3)
    assert sm.coerce("icon_color", []) == ()
    assert sm.coerce("trigger_keys", ["fn", "f13"]) == ("fn", "f13")
    with pytest.raises(ConfigError):
        sm.coerce("icon_color", ["red", "green", "blue"])


def test_field_defaults_and_to_jsonable():
    defaults = sm.field_defaults()
    assert list(defaults) == FIELD_NAMES
    assert defaults["trigger_keys"] == ("fn",)

    data = sm.to_jsonable(Settings(icon_color=(0.1, 0.2, 0.3)))
    assert data["trigger_keys"] == ["fn"]
    assert data["icon_color"] == [0.1, 0.2, 0.3]
    assert data["meeting_apps"] == list(cfg.MEETING_APP_KEYS)
    assert data["sounds"] is True
    assert not any(isinstance(v, tuple) for v in data.values())


# ─── preview ─────────────────────────────────────────────────────────────────────


def test_preview_coerces_json_input(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})
    new, tiers = mgr.preview(
        {"hold_threshold": "0.5", "trigger_keys": ["fn", "f13"], "sounds": False}
    )
    assert new.hold_threshold == 0.5
    assert new.trigger_keys == ("fn", "f13")
    assert new.sounds is False
    assert tiers == {
        Tier.LIVE: ["hold_threshold", "sounds"],
        Tier.LISTENERS: ["trigger_keys"],
        Tier.RESTART: [],
    }
    # Nothing applied, nothing written.
    assert mgr.current == Settings()
    assert not tmp_config.exists()


def test_preview_coerces_ints_and_bool_strings(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})
    new, _ = mgr.preview({"web_port": "50000", "overlay": "off", "icon_color": [1, 0, 0]})
    assert new.web_port == 50000
    assert new.overlay is False
    assert new.icon_color == (1.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "changes",
    [
        {"no_such_setting": 1},
        {"hold_threshold": "fast"},
        {"trigger_keys": ["fn", "banana"]},
        {"trigger_keys": []},
        {"web_port": 80},
        {"paste_mode": "telepathy"},
        {"trigger_keys": ["mouse_left"], "mouse_hold_threshold": 0.1},
    ],
)
def test_preview_rejects_invalid(tmp_config, changes):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})
    with pytest.raises(ConfigError):
        mgr.preview(changes)
    with pytest.raises(ConfigError):
        mgr.apply(changes)
    assert mgr.current == Settings()
    assert not tmp_config.exists()


def test_unknown_setting_message(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})
    with pytest.raises(ConfigError, match="unknown setting 'bogus'"):
        mgr.preview({"bogus": 1})


def test_env_override_rejected_without_writing(tmp_config):
    mgr = SettingsManager(
        Settings(model="small"), config_path=tmp_config, sources={"model": "env"}
    )
    with pytest.raises(sm.EnvOverrideError) as info:
        mgr.apply({"model": "tiny", "sounds": False})
    assert issubclass(sm.EnvOverrideError, ConfigError)
    assert "WHISPERLOCAL_MODEL" in str(info.value)
    assert mgr.current.model == "small"
    assert mgr.current.sounds is True
    assert not tmp_config.exists()

    # Re-sending the value the environment already pins is not a change.
    new, tiers = mgr.preview({"model": "small", "sounds": False})
    assert new.sounds is False
    assert tiers[Tier.LIVE] == ["sounds"]


def test_sources_read_lazily_from_config(tmp_config, monkeypatch):
    mgr = SettingsManager(Settings(), config_path=tmp_config)
    # Set after construction: still seen, because sources() is called on use.
    monkeypatch.setenv("WHISPERLOCAL_LANGUAGE", "de")
    with pytest.raises(sm.EnvOverrideError):
        mgr.preview({"language": "fr"})


def test_config_path_defaults_lazily(tmp_config):
    mgr = SettingsManager(Settings())
    assert mgr.config_path == tmp_config


# ─── apply ───────────────────────────────────────────────────────────────────────


def test_apply_persists_only_changed_keys_and_updates_current(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})

    result = mgr.apply({"hold_threshold": "0.5", "model": "base"})

    assert result.settings.hold_threshold == 0.5
    assert mgr.current is result.settings
    assert result.changed == ["hold_threshold"]
    assert result.applied == {"live": ["hold_threshold"], "listeners": [], "restart": []}
    assert result.restart_required is False
    assert result.warnings == []
    assert result.persisted is True

    text = tmp_config.read_text()
    changed_lines = set(text.splitlines()) ^ set(cfg.TEMPLATE.splitlines())
    assert changed_lines == {"hold_threshold = 0.0", "hold_threshold = 0.5"}


def test_apply_skips_default_values_absent_from_file(tmp_config):
    # Reached via the CLI, say: current differs from default but the file
    # does not mention it. Reverting to the default needs no line.
    mgr = SettingsManager(Settings(language="de"), config_path=tmp_config, sources={})
    result = mgr.apply({"language": "en"})
    assert result.changed == ["language"]
    assert result.persisted is False
    assert not tmp_config.exists()
    assert mgr.current.language == "en"


def test_apply_updates_existing_line_even_to_default(tmp_config):
    tmp_config.parent.mkdir(parents=True)
    tmp_config.write_text('language = "de"\n')
    mgr = SettingsManager(Settings(language="de"), config_path=tmp_config, sources={})
    result = mgr.apply({"language": "en"})
    assert result.persisted is True
    assert tomllib.loads(tmp_config.read_text())["language"] == "en"


def test_apply_notifies_subscribers_with_tiers(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})
    calls: list[tuple] = []

    def boom(old, new, tiers):
        calls.append(("boom",))
        raise RuntimeError("subscriber bug")

    mgr.subscribe(boom)
    mgr.subscribe(lambda old, new, tiers: calls.append((old, new, tiers)))

    result = mgr.apply({"trigger_keys": ["fn", "f13"], "web_port": 50000})

    assert len(calls) == 2
    assert calls[0] == ("boom",)
    old, new, tiers = calls[1]
    assert old == Settings()
    assert new is result.settings
    assert tiers == {
        Tier.LIVE: [], Tier.LISTENERS: ["trigger_keys"], Tier.RESTART: ["web_port"]
    }
    assert result.restart_required is True
    assert result.applied["restart"] == ["web_port"]

    parsed = tomllib.loads(tmp_config.read_text())
    assert parsed["trigger_keys"] == ["fn", "f13"]
    assert parsed["web_port"] == 50000


def test_apply_without_persist(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})
    result = mgr.apply({"sounds": False}, persist=False)
    assert result.persisted is False
    assert mgr.current.sounds is False
    assert not tmp_config.exists()


def test_apply_reports_trigger_warnings(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config, sources={})
    result = mgr.apply({"trigger_keys": ["fn", "cmd_r"]})
    assert result.warnings == cfg.trigger_warnings(result.settings)
    assert any("Right Command" in w for w in result.warnings)


def test_legacy_icon_lines_removed_on_first_save(tmp_config, capsys):
    tmp_config.parent.mkdir(parents=True)
    tmp_config.write_text(
        'model = "small"\n'
        'icon_idle = "mic"\n'
        'icon_waiting = "hourglass"\n'
        'icon_recording = "mic.fill"\n'
        'icon_transcribing = "waveform"\n'
        'icon_disabled = "mic.slash"\n'
        "icon_point_size = 15\n"
        "icon_color = [1.00, 0.58, 0.00]\n"
    )
    initial = cfg.load()
    assert "pre-1.3 defaults" in capsys.readouterr().err
    assert initial.icon_color == ()

    mgr = SettingsManager(initial, config_path=tmp_config, sources={})
    result = mgr.apply({"sounds": False})

    assert result.changed == ["sounds"]
    parsed = tomllib.loads(tmp_config.read_text())
    assert parsed["model"] == "small"
    assert parsed["sounds"] is False
    for key in ("icon_color", *cfg.LEGACY_ICONS):
        assert key not in parsed

    cfg.load()
    assert "pre-1.3" not in capsys.readouterr().err


def test_legacy_icon_color_chosen_again_is_written_as_default(tmp_config):
    tmp_config.parent.mkdir(parents=True)
    tmp_config.write_text("icon_color = [0.2, 0.2, 0.2]\n")
    mgr = SettingsManager(
        Settings(icon_color=(0.2, 0.2, 0.2)), config_path=tmp_config, sources={}
    )
    result = mgr.apply({"icon_color": list(cfg.LEGACY_ICON_COLOR)})
    assert result.settings.icon_color == ()
    assert tomllib.loads(tmp_config.read_text())["icon_color"] == []


def test_legacy_trigger_key_counts_as_present_in_file(tmp_config):
    tmp_config.parent.mkdir(parents=True)
    tmp_config.write_text('trigger_key = "f13"\n')
    mgr = SettingsManager(Settings(trigger_keys=("f13",)), config_path=tmp_config, sources={})
    mgr.apply({"trigger_keys": ["fn"]})  # back to the default
    parsed = tomllib.loads(tmp_config.read_text())
    assert "trigger_key" not in parsed
    assert parsed["trigger_keys"] == ["fn"]


def test_apply_round_trips_through_loader(tmp_config):
    mgr = SettingsManager(Settings(), config_path=tmp_config)
    mgr.apply(
        {
            "trigger_keys": ["fn", "mouse_right"],
            "mouse_hold_threshold": "0.7",
            "web_port": 50123,
            "meeting_apps": ["zoom", "teams"],
        }
    )
    assert cfg.load(strict=True) == mgr.current
