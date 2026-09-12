"""config_writer edits config.toml in place and never leaves it unparsable."""

from __future__ import annotations

import tomllib

import pytest

from whisperlocal import config as cfg
from whisperlocal import config_writer as cw


# ─── format_value ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-12, "-12"),
        (47311, "47311"),
        (0.3, "0.3"),
        (-45.0, "-45.0"),
        (0.0, "0.0"),
        ("base", '"base"'),
        ("", '""'),
        ('say "hi"\\now', '"say \\"hi\\"\\\\now"'),
        ("tab\there\nline", '"tab\\there\\nline"'),
        (("fn", "f13"), '["fn", "f13"]'),
        (["fn", "f13"], '["fn", "f13"]'),
        ((1.0, 0.58, 0.0), "[1.0, 0.58, 0.0]"),
        ((), "[]"),
        ([], "[]"),
    ],
)
def test_format_value(value, expected):
    assert cw.format_value(value) == expected


@pytest.mark.parametrize("value", [None, {"a": 1}, object()])
def test_format_value_rejects_unsupported(value):
    with pytest.raises(TypeError):
        cw.format_value(value)


def test_format_value_round_trips_through_tomllib():
    cases = {
        "a": True, "b": 3, "c": 0.3, "d": 'q"uo\\te\n', "e": ("x", "y"),
        "f": (1.0, 0.58, 0.0), "g": (), "h": 1e-05, "i": "~/Library/Application Support/x",
    }
    text = "\n".join(f"{k} = {cw.format_value(v)}" for k, v in cases.items())
    parsed = tomllib.loads(text)
    for key, value in cases.items():
        got = parsed[key]
        if isinstance(value, tuple):
            assert got == list(value)
        else:
            assert got == value


# ─── update_config ───────────────────────────────────────────────────────────────

SAMPLE = """\
# My config
# hold_threshold = 9.9   (a commented-out example must be left alone)
trigger_keys = ["fn"]

# the model
model = "base"   # trailing comment
sounds = true
"""


def test_replaces_existing_line_and_keeps_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE)

    text = cw.update_config(path, {"model": "small", "sounds": False})

    assert text == path.read_text()
    lines = text.splitlines()
    assert lines[0] == "# My config"
    assert lines[1].startswith("# hold_threshold = 9.9")
    assert lines[2] == 'trigger_keys = ["fn"]'
    assert lines[4] == "# the model"
    assert lines[5] == 'model = "small"'
    assert lines[6] == "sounds = false"
    assert cw.APPEND_HEADER not in text
    parsed = tomllib.loads(text)
    assert parsed == {"trigger_keys": ["fn"], "model": "small", "sounds": False}


def test_rewrites_legacy_trigger_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# old style\ntrigger_key = "f13"\nmodel = "base"\n')

    text = cw.update_config(path, {"trigger_keys": ("fn", "f14")})

    assert text.splitlines()[1] == 'trigger_keys = ["fn", "f14"]'
    parsed = tomllib.loads(text)
    assert "trigger_key" not in parsed
    assert parsed["trigger_keys"] == ["fn", "f14"]


def test_appends_missing_keys_under_one_header(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE)

    first = cw.update_config(path, {"web_port": 50000})
    assert first.count(cw.APPEND_HEADER) == 1
    assert first.endswith(f"{cw.APPEND_HEADER}\nweb_port = 50000\n")

    second = cw.update_config(path, {"language": "de", "web_port": 50001})
    assert second.count(cw.APPEND_HEADER) == 1
    tail = second.split(cw.APPEND_HEADER)[1].strip().splitlines()
    assert tail == ["web_port = 50001", 'language = "de"']

    parsed = tomllib.loads(second)
    assert parsed["web_port"] == 50001
    assert parsed["language"] == "de"
    assert parsed["model"] == "base"  # untouched


def test_removes_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE + "icon_color = [1.0, 0.58, 0.0]\n")

    text = cw.update_config(path, {"model": "tiny"}, remove=["icon_color", "sounds"])

    parsed = tomllib.loads(text)
    assert "icon_color" not in parsed
    assert "sounds" not in parsed
    assert parsed["model"] == "tiny"
    assert "# the model" in text


def test_remove_does_not_beat_values(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE)
    text = cw.update_config(path, {"sounds": False}, remove=["sounds"])
    assert tomllib.loads(text)["sounds"] is False


def test_falls_back_to_template_on_multiline_array(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'trigger_keys = [\n  "fn",\n  "f13",\n]\nmodel = "small"\nsample_rate = 44100\n'
    )

    text = cw.update_config(path, {"trigger_keys": ("fn", "f14")})

    parsed = tomllib.loads(text)
    assert parsed["trigger_keys"] == ["fn", "f14"]
    # Regenerated from the template...
    assert text.startswith("# WhisperLocal configuration")
    # ...with the user's other settings carried over, not reset. sample_rate
    # is not in the template, so it lands in the appended section.
    assert parsed["model"] == "small"
    assert parsed["sample_rate"] == 44100
    assert text.count(cw.APPEND_HEADER) == 1


def test_falls_back_when_value_would_not_read_back(tmp_path):
    # A dotted/quoted key form the line matcher does not recognise as the same
    # key: the plain `model =` line wins, but the quoted duplicate makes the
    # file invalid TOML, so regeneration must kick in.
    path = tmp_path / "config.toml"
    path.write_text('model = "base"\n"model" = "small"\n')
    text = cw.update_config(path, {"model": "tiny"})
    assert tomllib.loads(text)["model"] == "tiny"


def test_creates_file_from_template_when_absent(tmp_path):
    path = tmp_path / "nested" / "dir" / "config.toml"
    assert not path.exists()

    text = cw.update_config(path, {"hold_threshold": 0.5, "trigger_keys": ["fn", "f13"]})

    assert path.exists()
    assert not path.with_name("config.toml.bak").exists()
    assert text.startswith("# WhisperLocal configuration")
    parsed = tomllib.loads(text)
    assert parsed["hold_threshold"] == 0.5
    assert parsed["trigger_keys"] == ["fn", "f13"]
    # Every other template key is still present with its default.
    template = tomllib.loads(cfg.TEMPLATE)
    for key, value in template.items():
        if key not in ("hold_threshold", "trigger_keys"):
            assert parsed[key] == value
    assert cw.APPEND_HEADER not in text


def test_template_parses_and_matches_defaults():
    parsed = tomllib.loads(cfg.TEMPLATE)
    defaults = cfg.Settings()
    for key, value in parsed.items():
        expected = getattr(defaults, key)
        if isinstance(expected, tuple):
            expected = list(expected)
        assert value == expected, key


def test_writes_backup_of_previous_content(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE)

    cw.update_config(path, {"model": "small"})
    bak = path.with_name("config.toml.bak")
    assert bak.read_text() == SAMPLE

    after_first = path.read_text()
    cw.update_config(path, {"model": "tiny"})
    assert bak.read_text() == after_first


def test_no_temp_files_left_behind(tmp_path):
    path = tmp_path / "config.toml"
    cw.update_config(path, {"model": "small"})
    cw.update_config(path, {"model": "tiny"})
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["config.toml", "config.toml.bak"]


def test_write_failure_raises_config_write_error(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    with pytest.raises(cw.ConfigWriteError):
        cw.update_config(blocker / "config.toml", {"model": "small"})


def test_result_round_trips_through_loader(tmp_config, monkeypatch):
    monkeypatch.delenv("WHISPERLOCAL_MODEL", raising=False)
    cw.update_config(
        tmp_config,
        {
            "trigger_keys": ("fn", "mouse_middle"),
            "mouse_hold_threshold": 0.8,
            "icon_color": (0.2, 0.4, 0.6),
            "meeting_dir": "~/Some Dir/with spaces",
            "web_port": 50123,
        },
    )
    settings = cfg.load(strict=True)
    assert settings.trigger_keys == ("fn", "mouse_middle")
    assert settings.mouse_hold_threshold == 0.8
    # Compared as numbers: the loader's element typing is config.py's business.
    assert tuple(float(v) for v in settings.icon_color) == (0.2, 0.4, 0.6)
    assert settings.meeting_dir == "~/Some Dir/with spaces"
    assert settings.web_port == 50123
