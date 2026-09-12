"""The Settings page, described as data.

``SCHEMA`` is a list of groups, each with an ordered list of fields. The
frontend renders it directly, so every ``Settings`` field must appear here
exactly once (``tests/test_settings_schema.py`` enforces that), plus the one
*virtual* field — ``api_key`` — that lives in the Keychain rather than in
config.toml.

Field shape::

    {
        "name":    "hold_threshold",          # Settings attribute (or virtual name)
        "label":   "Hold threshold",
        "type":    "bool" | "int" | "number" | "text" | "select" | "radio"
                   | "multiselect" | "triggers" | "secret" | "color",
        "help":    "One or two sentences.",
        "tier":    "live" | "listeners" | "restart",
        "virtual": False,
        # optional:
        "options": [{"value": "paste", "label": "Paste"}, ...],
        "min": 0, "max": 1, "step": 0.05,
        "placeholder": "...",
        "warning": "...",       # shown prominently next to the control
    }

``tier`` is the static hint for what a change costs; the authoritative answer
for a concrete edit comes back from ``PUT /api/settings`` (``applied`` and
``restart_required``) and ``SettingsManager.preview``.
"""

from __future__ import annotations

from whisperlocal.config import (
    BACKENDS,
    MEETING_APP_KEYS,
    MEETING_PROMPTS,
    MODEL_ALIASES,
)

LANGUAGES: tuple[tuple[str, str], ...] = (
    ("auto", "Auto-detect"),
    ("en", "English"),
    ("de", "German"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("nl", "Dutch"),
    ("hi", "Hindi"),
    ("ja", "Japanese"),
    ("zh", "Chinese"),
    ("ko", "Korean"),
    ("ru", "Russian"),
    ("ar", "Arabic"),
    ("tr", "Turkish"),
    ("pl", "Polish"),
    ("sv", "Swedish"),
)

MEETING_APP_LABELS: dict[str, str] = {
    "zoom": "Zoom",
    "teams": "Microsoft Teams",
    "facetime": "FaceTime",
    "slack": "Slack",
    "webex": "Webex",
    "discord": "Discord",
    "browser": "Browser (Meet, Teams web, ...)",
}

CUSTOM = "custom"


def _opts(pairs) -> list[dict[str, str]]:
    return [{"value": v, "label": l} for v, l in pairs]


def _field(
    name: str,
    label: str,
    type: str,
    help: str,
    *,
    tier: str = "live",
    virtual: bool = False,
    **extra: object,
) -> dict:
    out: dict = {
        "name": name,
        "label": label,
        "type": type,
        "help": help,
        "tier": tier,
        "virtual": virtual,
    }
    out.update(extra)
    return out


def _group(key: str, label: str, help: str, fields: list[dict]) -> dict:
    return {"key": key, "label": label, "help": help, "fields": fields}


SCHEMA: list[dict] = [
    _group(
        "trigger",
        "Trigger",
        "How a dictation starts and stops.",
        [
            _field(
                "trigger_keys",
                "Trigger keys",
                "triggers",
                "Hold any one of these to dictate. Whichever trigger you press "
                "first owns the recording until you let go, so pressing a second "
                "one mid-sentence does not cut you off.",
                tier="listeners",
            ),
            _field(
                "hold_threshold",
                "Hold threshold (seconds)",
                "number",
                "Seconds to hold before recording starts. 0 records the moment "
                "the key goes down; accidental taps are still discarded by the "
                "minimum recording duration.",
                min=0,
                step=0.1,
            ),
            _field(
                "min_recording_duration",
                "Minimum recording (seconds)",
                "number",
                "Recordings shorter than this are thrown away as noise.",
                min=0,
                step=0.1,
            ),
            _field(
                "mouse_hold_threshold",
                "Mouse hold threshold (seconds)",
                "number",
                "Mouse buttons use this instead of the hold threshold, which is "
                "often 0 — fine for a key you never otherwise press, but it would "
                "make every click record. Minimum 0.3 when a mouse button is a trigger.",
                min=0,
                step=0.1,
            ),
            _field(
                "mouse_drag_cancel_px",
                "Drag guard (pixels)",
                "int",
                "A press that moves further than this is a drag, not someone "
                "holding still to talk: the trigger is cancelled and the audio "
                "discarded. Set to 0 to turn the guard off.",
                min=0,
                step=1,
            ),
        ],
    ),
    _group(
        "transcription",
        "Transcription",
        "Which model turns your speech into text, and where it runs.",
        [
            _field(
                "model",
                "Model",
                "select",
                "A short name or any Hugging Face repo id. Bigger is more "
                "accurate and slower.",
                options=_opts([(k, k) for k in MODEL_ALIASES] + [(CUSTOM, "Custom repo id…")]),
            ),
            _field(
                "language",
                "Language",
                "select",
                "Two-letter language code, or auto to detect. Naming the "
                "language is faster and more accurate than autodetection.",
                options=_opts(list(LANGUAGES) + [(CUSTOM, "Other code…")]),
            ),
            _field(
                "fp16",
                "Half precision (fp16)",
                "bool",
                "Off by default because that is the configuration this has been "
                "tuned against; turning it on is usually faster.",
            ),
            _field(
                "dictation_backend",
                "Dictation backend",
                "radio",
                "Local runs the MLX model on this Mac. API sends the audio to an "
                "OpenAI-compatible transcription endpoint — the one thing here "
                "that puts audio on the network.",
                options=_opts([("local", "Local (on this Mac)"), ("api", "API (network)")]),
            ),
            _field(
                "api_base_url",
                "API base URL",
                "text",
                "An OpenAI-compatible endpoint: OpenAI, Groq, or a self-hosted "
                "server. Must start with http:// or https://.",
                placeholder="https://api.openai.com/v1",
            ),
            _field(
                "api_model",
                "API model",
                "text",
                "The model name the endpoint expects, e.g. whisper-1.",
            ),
            _field(
                "api_key",
                "API key",
                "secret",
                "Stored in the macOS Keychain, never in the config file. Leave "
                "blank to keep the current key.",
                virtual=True,
            ),
            _field(
                "api_timeout_seconds",
                "API timeout (seconds)",
                "int",
                "How long to wait for the endpoint before giving up on a dictation.",
                min=1,
                step=1,
            ),
        ],
    ),
    _group(
        "meetings",
        "Meetings",
        "When a meeting app has the microphone open, offer to record the call: "
        "your mic plus the other participants through a system-audio tap, both "
        "transcribed and saved.",
        [
            _field(
                "meeting_enabled",
                "Meeting recording",
                "bool",
                "Watch for meeting apps using the microphone and offer to record.",
                tier="listeners",
            ),
            _field(
                "meeting_auto_record",
                "Record automatically",
                "bool",
                "Start recording as soon as a meeting is detected, without asking.",
            ),
            _field(
                "meeting_prompt",
                "How to ask",
                "select",
                "Notification shows a banner with a Record button; panel opens a "
                "small window; none never asks (pair it with automatic recording "
                "or start from the menu).",
                options=_opts([(p, p.capitalize()) for p in MEETING_PROMPTS]),
            ),
            _field(
                "meeting_apps",
                "Apps to watch",
                "multiselect",
                "Which apps count as a meeting when they hold the microphone.",
                options=_opts([(k, MEETING_APP_LABELS.get(k, k)) for k in MEETING_APP_KEYS]),
                tier="listeners",
            ),
            _field(
                "meeting_backend",
                "Meeting backend",
                "radio",
                "Local transcribes on this Mac; API sends meeting audio to the "
                "configured endpoint.",
                options=_opts([("local", "Local (on this Mac)"), ("api", "API (network)")]),
            ),
            _field(
                "meeting_model",
                "Meeting model",
                "text",
                "A short name or Hugging Face repo id. Blank uses the same model "
                "as dictation.",
                placeholder="same as dictation model",
            ),
            _field(
                "meeting_system_audio",
                "Capture the other participants",
                "bool",
                "Record system audio through a tap so the other side of the call "
                "is transcribed too, not just your microphone.",
                tier="listeners",
            ),
            _field(
                "meeting_system_device",
                "System audio device",
                "text",
                "A BlackHole-style loopback input to use when no system audio tap "
                "is possible. Blank uses the tap.",
                tier="listeners",
            ),
            _field(
                "meeting_transcribe_live",
                "Transcribe while recording",
                "bool",
                "Transcribe each segment as it finishes instead of all at once "
                "when the meeting ends.",
            ),
            _field(
                "meeting_keep_audio",
                "Keep the audio",
                "bool",
                "Keep the recorded audio alongside the transcript. Off deletes it "
                "once the transcript is written.",
            ),
            _field(
                "meeting_audio_format",
                "Audio format",
                "select",
                "FLAC is lossless and roughly half the size of WAV.",
                options=_opts([("flac", "FLAC"), ("wav", "WAV")]),
            ),
            _field(
                "meeting_segment_seconds",
                "Segment length (seconds)",
                "int",
                "Audio is transcribed in chunks of this length. Shorter means "
                "the live transcript keeps up better; longer gives the model more "
                "context. Between 20 and 300.",
                min=20,
                max=300,
                step=5,
            ),
            _field(
                "meeting_silence_db",
                "Silence level (dB)",
                "number",
                "Audio quieter than this counts as silence and is not sent to the "
                "model.",
                max=0,
                step=1,
            ),
            _field(
                "meeting_end_grace_seconds",
                "End grace period (seconds)",
                "int",
                "How long after the meeting app releases the microphone before "
                "the recording is considered over. Covers brief reconnects.",
                min=0,
                step=1,
            ),
            _field(
                "meeting_min_seconds",
                "Minimum meeting length (seconds)",
                "int",
                "Recordings shorter than this are discarded rather than saved.",
                min=0,
                step=1,
            ),
            _field(
                "meeting_transcript_words",
                "Word timestamps",
                "bool",
                "Store a timestamp for every word, not just every segment. Larger "
                "transcripts, more precise search.",
            ),
            _field(
                "meeting_tap_scope",
                "Tap scope",
                "select",
                "System captures everything the Mac plays; app captures only the "
                "meeting app's own output.",
                options=_opts([("system", "Whole system"), ("app", "Meeting app only")]),
                tier="listeners",
            ),
            _field(
                "meeting_dir",
                "Meetings folder",
                "text",
                "Where recordings and transcripts are saved. Application Support "
                "is not iCloud-synced.",
            ),
        ],
    ),
    _group(
        "behaviour",
        "Behaviour",
        "What happens with the text, and what you see while talking.",
        [
            _field(
                "paste_mode",
                "After transcribing",
                "select",
                "Paste copies and presses Cmd+V for you (needs Accessibility "
                "permission). Clipboard only copies; use it in apps that reject "
                "synthetic keystrokes.",
                options=_opts([("paste", "Paste at the cursor"), ("clipboard", "Copy to clipboard only")]),
            ),
            _field(
                "sounds",
                "Sounds",
                "bool",
                "System sounds on start, stop, success and failure.",
            ),
            _field(
                "overlay",
                "Recording dot",
                "bool",
                "The small pulsing dot shown while recording.",
            ),
            _field(
                "overlay_anchor",
                "Dot position",
                "select",
                "Caret puts it beside your text cursor, falling back to the mouse "
                "pointer in apps that do not report their cursor; mouse follows "
                "the pointer; bottom sits above the Dock.",
                options=_opts([("caret", "Beside the text cursor"), ("mouse", "Beside the mouse pointer"), ("bottom", "Above the Dock")]),
            ),
            _field(
                "overlay_offset_x",
                "Dot offset X (px)",
                "int",
                "Nudge the dot sideways so it does not cover your text.",
                step=1,
            ),
            _field(
                "overlay_offset_y",
                "Dot offset Y (px)",
                "int",
                "Nudge the dot up or down from the anchor.",
                step=1,
            ),
        ],
    ),
    _group(
        "memory",
        "Memory",
        "How much the idle app is allowed to hold on to.",
        [
            _field(
                "mlx_cache_mb",
                "MLX buffer cache (MB)",
                "int",
                "MLX keeps freed GPU buffers around to reuse; left alone that "
                "grows to about a gigabyte. 128 MB keeps most of the speed at "
                "less than half the memory. 0 disables the cache; -1 lets MLX do "
                "whatever it likes.",
                min=-1,
                step=16,
                tier="restart",
            ),
            _field(
                "idle_release_seconds",
                "Release cache after (seconds)",
                "int",
                "Seconds without dictation before the cache is dropped "
                "completely, so a burst of dictation stays fast but an idle app "
                "is not holding memory. 0 turns the release off.",
                min=0,
                step=10,
            ),
        ],
    ),
    _group(
        "hallucination",
        "Hallucination guard",
        "Whisper loops on short or noisy audio, emitting one word over and "
        "over. Output failing either test below is discarded instead of pasted.",
        [
            _field(
                "max_word_run",
                "Longest repeated run",
                "int",
                "Longest allowed run of the same word back to back. Must be 2 or more.",
                min=2,
                step=1,
            ),
            _field(
                "max_repeat_ratio",
                "Minimum unique-word ratio",
                "number",
                "Below this ratio of unique words to total words, the output is "
                "a loop. Real speech scores well above 0.3; measured "
                "hallucinations score 0.01-0.29.",
                min=0,
                max=1,
                step=0.05,
            ),
            _field(
                "repeat_min_words",
                "Ratio test from (words)",
                "int",
                "The ratio test needs a few words to mean anything; shorter "
                "output is judged by the run test alone.",
                min=1,
                step=1,
            ),
        ],
    ),
    _group(
        "history",
        "History",
        "Every dictation, successful or not, is appended to a JSONL file. This "
        "is what the statistics read.",
        [
            _field(
                "history_enabled",
                "Keep history",
                "bool",
                "Log every dictation — counts, timings, which app — to the "
                "history file. Failures are logged too; the hallucination rate is "
                "only measurable if they are.",
            ),
            _field(
                "history_text",
                "Store the words",
                "bool",
                "Store the transcribed text itself, not just the statistics.",
                warning=(
                    "With this on, the history file is a permanent, unencrypted, "
                    "plain-text record of everything you dictate. Turn it off to "
                    "keep the statistics but not the words. Deleting the file is "
                    "a complete purge."
                ),
            ),
            _field(
                "history_file",
                "History file",
                "text",
                "Where the log lives. Application Support is not iCloud-synced; "
                "Documents is, and would upload everything you say.",
            ),
        ],
    ),
    _group(
        "menubar",
        "Menu bar",
        "The status icon. SF Symbol names — any name from Apple's SF Symbols app works.",
        [
            _field("icon_idle", "Idle", "text", "SF Symbol name shown when ready."),
            _field("icon_waiting", "Waiting", "text", "SF Symbol name while the hold threshold counts down."),
            _field("icon_recording", "Recording", "text", "SF Symbol name while recording."),
            _field("icon_transcribing", "Transcribing", "text", "SF Symbol name while the model runs."),
            _field("icon_disabled", "Disabled", "text", "SF Symbol name when dictation is paused."),
            _field("icon_meeting", "Meeting recording", "text", "SF Symbol name while a meeting is being recorded."),
            _field("icon_meeting_detected", "Meeting detected", "text", "SF Symbol name when a meeting app has the microphone."),
            _field(
                "icon_point_size",
                "Icon size (pt)",
                "int",
                "Point size of the glyph. 16 matches the system's own icons.",
                min=10,
                max=22,
                step=1,
            ),
            _field(
                "icon_color",
                "Icon colour",
                "color",
                "Leave empty to follow the menu bar: a template image renders "
                "monochrome like the system's own status icons and follows "
                "light and dark mode. A fixed colour does neither.",
            ),
        ],
    ),
    _group(
        "dashboard",
        "Dashboard",
        "This page. Served to your browser from this machine only (127.0.0.1); "
        "nothing is reachable from outside.",
        [
            _field(
                "web_enabled",
                "Dashboard server",
                "bool",
                "Run the local web server for this Settings page and the "
                "analytics dashboard.",
                tier="restart",
            ),
            _field(
                "web_port",
                "Port",
                "int",
                "Port on 127.0.0.1. If it is taken, a free one is used instead.",
                min=1024,
                max=65535,
                step=1,
                tier="restart",
            ),
        ],
    ),
    _group(
        "advanced",
        "Advanced",
        "Fallbacks you should not need to touch.",
        [
            _field(
                "sample_rate",
                "Sample rate (Hz)",
                "int",
                "Fallback only. Recording runs at the input device's native "
                "rate and the audio is downsampled when read; this is used only "
                "if the device rate cannot be determined.",
                min=8000,
                step=1000,
                tier="restart",
            ),
            _field(
                "channels",
                "Channels",
                "int",
                "Input channels to record. Whisper works on mono.",
                min=1,
                max=2,
                step=1,
                tier="restart",
            ),
        ],
    ),
]

FIELDS: dict[str, dict] = {f["name"]: f for g in SCHEMA for f in g["fields"]}
"""Every field by name, virtual ones included."""


def settings_field_names() -> list[str]:
    """Names of the schema fields that are real ``Settings`` attributes."""
    return [name for name, f in FIELDS.items() if not f["virtual"]]


__all__ = ["SCHEMA", "FIELDS", "LANGUAGES", "CUSTOM", "settings_field_names"]
